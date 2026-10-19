# 管理内置与本地冻结权重快照。
"""Validated immutable weight snapshots and an offline-first local provider.

Network acquisition is intentionally separate from activation. No downloaded
code, remote Python expressions or host-global template mutation is involved.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from types import MappingProxyType

from echo_workshop.resources.weight_control import WeightControl, WeightConflictError, actual
from echo_workshop.core.cancellation import checkpoint
from echo_workshop.core.scoring import ALGORITHM_VERSION as ALGORITHM_VERSION

from echo_workshop.resources.snapshot import CONDITIONS, SOURCE_COMMIT, SOURCE_REPOSITORY, TEMPLATES


SCHEMA_VERSION = 1

_CHAR = re.compile(r'(?:default|[0-9]{1,8})\Z')
_VARIANT = re.compile(r'(?:default|[a-z0-9]+(?:-[a-z0-9]+)*)\Z')
_KEY = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    )


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _freeze(value, depth=0):
    if depth > 20:
        raise ValueError('Weight data nesting is too deep')
    if isinstance(value, Mapping):
        if len(value) > 2048 or not all(isinstance(key, str) for key in value):
            raise ValueError('Invalid weight object')
        return MappingProxyType({key: _freeze(item, depth + 1) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        if len(value) > 2048:
            raise ValueError('Weight array is too large')
        return tuple(_freeze(item, depth + 1) for item in value)
    if value is None or type(value) in (bool, str, int, float):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Non-finite weight')
        return value
    raise ValueError('Only declarative JSON data is accepted')


def _number(value, positive=False):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value > 1e12
        or (value < 1e-9 if positive else value < 0)
    ):
        raise ValueError('Invalid numeric weight')


def _condition(rule, variants, *, root=False, depth=0):
    if depth > 12 or not isinstance(rule, Mapping):
        raise ValueError('Invalid modal condition')
    allowed = {'choose', 'op', 'sub', 'key', 'value'}
    if set(rule) - allowed:
        raise ValueError('Unsupported condition fields')
    if root:
        choose = rule.get('choose', '')
        if not isinstance(choose, str):
            raise ValueError('Condition target must be a filename')
        variant = (
            'default'
            if choose == 'calc.json'
            else choose.removeprefix('calc-').removesuffix('.json')
        )
        if (
            choose != ('calc.json' if variant == 'default' else f'calc-{variant}.json')
            or variant not in variants
        ):
            raise ValueError('Condition references an absent modal template')
    elif 'choose' in rule:
        raise ValueError('Nested condition cannot choose a template')
    op = rule.get('op')
    if op in ('&&', '||'):
        sub = rule.get('sub')
        if (
            not isinstance(sub, (list, tuple))
            or not 1 <= len(sub) <= 64
            or 'key' in rule
            or 'value' in rule
        ):
            raise ValueError('Invalid boolean condition')
        for child in sub:
            _condition(child, variants, depth=depth + 1)
    else:
        if op not in ('=', '!=', 'in', '>', '>=', '<', '<=') or 'sub' in rule:
            raise ValueError('Unsupported condition operator')
        if (
            not isinstance(rule.get('key'), str)
            or not _KEY.fullmatch(rule['key'])
            or 'value' not in rule
        ):
            raise ValueError('Invalid condition key')
        value = rule['value']
        if op == 'in':
            if not isinstance(value, (list, tuple)) or not all(
                type(item) in (str, int, float, bool) for item in value
            ):
                raise ValueError('Membership condition needs a literal array')
        elif type(value) not in (str, int, float, bool):
            raise ValueError('Condition needs a literal scalar')
        if op in ('>', '>=', '<', '<=') and type(value) not in (int, float):
            raise ValueError('Ordering condition needs a numeric literal')


def _validate(templates, conditions):
    if not isinstance(templates, Mapping) or not templates or not isinstance(conditions, Mapping):
        raise ValueError('Invalid weight bundle')
    if not isinstance(templates.get('default'), Mapping) or 'default' not in templates['default']:
        raise ValueError('Generic fallback template is required')
    for char_id, variants in templates.items():
        if not _CHAR.fullmatch(char_id) or not isinstance(variants, Mapping) or not variants:
            raise ValueError('Invalid character ID or modal collection')
        for variant, data in variants.items():
            if (
                not _VARIANT.fullmatch(variant)
                or not isinstance(data, Mapping)
                or not isinstance(data.get('name'), str)
                or not data['name'].strip()
            ):
                raise ValueError('Invalid modal ID or name')
            main = data.get('main_props')
            sub = data.get('sub_props')
            if not isinstance(main, Mapping) or not isinstance(sub, Mapping):
                raise ValueError('Missing main or sub weights')
            for cost in ('1', '3', '4'):
                weights = main.get(cost)
                if not isinstance(weights, Mapping) or not weights:
                    raise ValueError('Missing COST weights')
                for value in weights.values():
                    _number(value)
            for value in sub.values():
                _number(value)
            for name, length, positive in (('skill_weight', 4, False), ('score_max', 3, True)):
                values = data.get(name)
                if not isinstance(values, (list, tuple)) or len(values) != length:
                    raise ValueError(f'Invalid {name} length')
                for value in values:
                    _number(value, positive)
            from echo_workshop.core.rating import validate_rating_tables

            validate_rating_tables(data)
    for char_id, rules in conditions.items():
        if char_id not in templates or not isinstance(rules, (list, tuple)):
            raise ValueError('Condition character is absent')
        for rule in rules:
            _condition(rule, templates[char_id], root=True)


@dataclass(frozen=True)
class CharacterObservation:
    """A name observed in-game, not a guess at a scoring modal or avatar."""

    raw_name: str
    name: str = ''
    character_ids: tuple = ()
    state: str = 'unknown'

    @property
    def character_id(self):
        return self.character_ids[0] if self.state == 'equipped' else ''


class CharacterNameIndex:
    """Small derived view of validated weights; no files/network/avatar data."""

    def __init__(self, templates):
        by_name, names = {}, {}
        for char_id, variants in templates.items():
            if char_id == 'default':
                continue
            bases = {self._base(data['name']) for data in variants.values()}
            bases.discard('')
            # A changed/inconsistent naming convention must not silently remap
            # game ownership. Keep the template usable for explicit scoring.
            if len(bases) != 1:
                continue
            name = next(iter(bases))
            names[char_id] = name
            by_name.setdefault(name, set()).add(char_id)
        traveler = set().union(
            *(by_name.get(name, set()) for name in ('雷主', '风主', '光主', '暗主'))
        )
        if traveler:
            by_name.setdefault('漂泊者', set()).update(traveler)
        self.names = MappingProxyType(names)
        self.by_name = MappingProxyType({name: tuple(sorted(ids)) for name, ids in by_name.items()})

    @staticmethod
    def _base(name):
        return name.split('-', 1)[0].split('－', 1)[0].strip()

    def resolve(self, raw_name):
        from echo_workshop.core.text import simplify_echo_text

        if not isinstance(raw_name, str):
            raise ValueError('Observed character name must be text')
        normalized = re.sub(r'\s+', '', simplify_echo_text(raw_name))
        normalized = normalized.removesuffix('装配中')
        if not normalized:
            return CharacterObservation(raw_name, state='unobserved')
        matches = [
            (name, ids)
            for name, ids in self.by_name.items()
            if normalized == name
            or normalized.startswith(name + '·')
            or normalized.startswith(name + '・')
        ]
        if not matches:
            return CharacterObservation(raw_name)
        longest = max(len(name) for name, _ in matches)
        chosen = [(name, ids) for name, ids in matches if len(name) == longest]
        ids = tuple(sorted({char_id for _, values in chosen for char_id in values}))
        return CharacterObservation(
            raw_name, chosen[0][0], ids, 'equipped' if len(ids) == 1 else 'ambiguous'
        )

    def in_display_text(self, text):
        from echo_workshop.core.text import simplify_echo_text

        compact = re.sub(r'\s+', '', simplify_echo_text(text))
        found = []
        for name in self.by_name:
            pattern = re.escape(name) + r'(?:[·・][^\n]{1,24}?)?装配中'
            found.extend(self.resolve(match[0]) for match in re.finditer(pattern, compact))
        if not found:
            return CharacterObservation('', state='unobserved')
        longest = max(len(entry.name) for entry in found)
        chosen = [entry for entry in found if len(entry.name) == longest]
        ids = tuple(sorted({char_id for entry in chosen for char_id in entry.character_ids}))
        return CharacterObservation(
            chosen[0].raw_name, chosen[0].name, ids, 'equipped' if len(ids) == 1 else 'ambiguous'
        )


@dataclass(frozen=True)
class WeightSnapshot:
    templates: Mapping
    conditions: Mapping
    source: str = SOURCE_REPOSITORY
    source_revision: str = ''
    fetched_at: str = ''
    local_state: Mapping | None = field(default=None, repr=False)
    character_elements: Mapping = field(default_factory=dict)
    version: str = field(init=False)
    options: Mapping = field(init=False, repr=False)
    characters: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        templates, conditions = _freeze(self.templates), _freeze(self.conditions)
        _validate(templates, conditions)
        for name in ('source', 'source_revision', 'fetched_at'):
            if not isinstance(getattr(self, name), str):
                raise ValueError('Snapshot provenance must be text')
        from echo_workshop.core.scoring import ELEMENT_NAMES

        elements = _freeze(self.character_elements)
        if (
            not isinstance(elements, Mapping)
            or len(elements) > 2048
            or any(
                not isinstance(cid, str)
                or not re.fullmatch(r'[0-9]{1,8}', cid)
                or element not in ELEMENT_NAMES
                for cid, element in elements.items()
            )
        ):
            raise ValueError('角色属性资料格式错误')
        identity = {'templates': _thaw(templates), 'conditions': _thaw(conditions)}
        # Absent metadata preserves historical dev87 snapshot hashes/proofs.
        if elements:
            identity['character_elements'] = _thaw(elements)
        object.__setattr__(self, 'character_elements', elements)
        local_state = None
        if self.local_state is not None:
            base, pins, expected = validate_local_state(self.local_state)
            expected_identity = {'templates': _thaw(expected[0]), 'conditions': _thaw(expected[1])}
            if base.character_elements:
                expected_identity['character_elements'] = _thaw(base.character_elements)
            if identity != expected_identity:
                raise ValueError('自定义权重与保存的角色设置不一致')
            local_state = _freeze(self.local_state)
            # A role saved without numeric changes must still remember its update
            # protection. Upstream provenance/time does not affect score identity.
            identity['custom'] = {'upstream_version': base.version, 'characters': pins}
        object.__setattr__(self, 'local_state', local_state)
        canonical = _json(identity)
        version = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        rows = [
            (char_id, variant, data)
            for char_id, variants in templates.items()
            for variant, data in variants.items()
        ]
        labels = {
            (cid, variant): (
                base.templates.get(cid, {}).get(variant, data)['name']
                if local_state is not None
                else data['name']
            )
            for cid, variant, data in rows
        }
        counts = Counter(labels.values())
        options = {
            labels[(char_id, variant)]
            if counts[labels[(char_id, variant)]] == 1
            else f'{labels[(char_id, variant)]}（{char_id}/{variant}）': (char_id, variant, data)
            for char_id, variant, data in rows
        }
        object.__setattr__(self, 'templates', templates)
        object.__setattr__(self, 'conditions', conditions)
        object.__setattr__(self, 'version', version)
        object.__setattr__(self, 'options', MappingProxyType(options))
        # Frozen custom numeric templates do not freeze role identity metadata.
        # Removed customized roles still retain their last explicit name.
        name_templates = dict(templates)
        if local_state is not None:
            name_templates.update(base.templates)
        object.__setattr__(self, 'characters', CharacterNameIndex(name_templates))

    @classmethod
    def builtin(cls):
        return cls(TEMPLATES, CONDITIONS, SOURCE_REPOSITORY, SOURCE_COMMIT)

    def template(self, char_id, variant='default'):
        # Stable IDs are strict: deleted selections must not silently migrate.
        try:
            return self.templates[char_id][variant]
        except KeyError as error:
            raise ValueError(f'Template is no longer available: {char_id}/{variant}') from error

    def scoring_template(self, char_id, variant='default'):
        # Raw upstream/custom fields stay unchanged and editable. Scoring gets
        # the immutable metadata belonging to this exact snapshot.
        data = dict(self.template(char_id, variant))
        data['_character_element'] = self.character_elements.get(char_id, '')
        data['_require_character_element'] = bool(self.character_elements) and char_id != 'default'
        from echo_workshop.core.rating import project_grade_fields

        upstream = (
            self.local_state['upstream']['templates'].get(char_id, {}).get(variant)
            if self.local_state is not None
            else None
        )
        return project_grade_fields(data, upstream)

    def compatibility_rule(self, char_id, rule=None):
        from echo_workshop.core.compatibility import CompatibilityRule

        rule = rule or CompatibilityRule()
        return CompatibilityRule(
            self.character_elements.get(char_id, '') or rule.element, rule.sonatas
        )

    def resolve_selection(self, label):
        """Translate saved display text to IDs; unresolved selections stay explicit."""
        if label == '通用':
            return ('default', 'default')
        if label in self.options:
            return self.options[label][:2]
        matches = [
            (char_id, variant)
            for char_id, variant, template in self.options.values()
            if template['name'].startswith(f'{label}-')
        ]
        default_matches = [entry for entry in matches if entry[1] == 'default']
        if len(default_matches) == 1:
            return default_matches[0]
        if len(matches) == 1:
            return matches[0]
        raise ValueError('Saved template needs an explicit replacement')

    def to_dict(self):
        data = {
            'schema_version': SCHEMA_VERSION,
            'version': self.version,
            'source': self.source,
            'source_revision': self.source_revision,
            'fetched_at': self.fetched_at,
            'templates': _thaw(self.templates),
            'conditions': _thaw(self.conditions),
        }
        if self.character_elements:
            data['character_elements'] = _thaw(self.character_elements)
        if self.local_state is not None:
            data['local_state'] = _thaw(self.local_state)
        return data

    @classmethod
    def from_dict(cls, data):
        if (
            not isinstance(data, dict)
            or type(data.get('schema_version')) is not int
            or data['schema_version'] != SCHEMA_VERSION
        ):
            raise ValueError('Unsupported snapshot schema')
        try:
            snapshot = cls(
                data['templates'],
                data['conditions'],
                data['source'],
                data.get('source_revision', ''),
                data.get('fetched_at', ''),
                data.get('local_state'),
                data.get('character_elements', {}),
            )
            if snapshot.version != data['version']:
                raise ValueError('Snapshot hash mismatch')
            return snapshot
        except (KeyError, TypeError) as error:
            raise ValueError('Malformed snapshot') from error


def validate_local_state(state):
    """Pure, bounded, declarative customization data inside the atomic snapshot."""
    if (
        not isinstance(state, Mapping)
        or set(state) != {'schema_version', 'upstream', 'characters'}
        or type(state['schema_version']) is not int
        or state['schema_version'] != 1
        or not isinstance(state['upstream'], Mapping)
        or 'local_state' in state['upstream']
        or not isinstance(state['characters'], Mapping)
        or not 1 <= len(state['characters']) <= 2048
    ):
        raise ValueError('自定义权重保存格式错误')
    base = WeightSnapshot.from_dict(_thaw(state['upstream']))
    pins = _thaw(_freeze(state['characters']))
    templates, conditions = _thaw(base.templates), _thaw(base.conditions)
    for char_id, pin in pins.items():
        if (
            not _CHAR.fullmatch(char_id)
            or not isinstance(pin, dict)
            or set(pin) != {'templates', 'conditions'}
        ):
            raise ValueError('自定义角色权重格式错误')
        templates[char_id] = pin['templates']
        conditions.pop(char_id, None)
        if pin['conditions']:
            conditions[char_id] = pin['conditions']
        elif not isinstance(pin['conditions'], list):
            raise ValueError('自定义角色条件须为列表')
    _validate(templates, conditions)
    return base, pins, (templates, conditions)


def customized_snapshot(base, pins):
    if not pins:
        return base
    state = {'schema_version': 1, 'upstream': base.to_dict(), 'characters': _thaw(pins)}
    _, _, (templates, conditions) = validate_local_state(state)
    return WeightSnapshot(
        templates,
        conditions,
        base.source,
        base.source_revision,
        base.fetched_at,
        state,
        base.character_elements,
    )


def condition_matches(rule, context):
    """Interpret a previously validated rule. Missing context never matches."""
    op = rule['op']
    if op == '&&':
        return all(condition_matches(child, context) for child in rule['sub'])
    if op == '||':
        return any(condition_matches(child, context) for child in rule['sub'])
    if rule['key'] not in context:
        return False
    actual, expected = context[rule['key']], rule['value']
    if op == '=':
        return actual == expected
    if op == '!=':
        return actual != expected
    if op == 'in':
        return actual in expected
    if type(actual) not in (int, float) or type(expected) not in (int, float):
        return False
    return {
        '>': lambda: actual > expected,
        '>=': lambda: actual >= expected,
        '<': lambda: actual < expected,
        '<=': lambda: actual <= expected,
    }[op]()


class WeightProvider:
    def __init__(self, data_dir, *, custom_evaluator=None):
        self.custom_evaluator = custom_evaluator
        self.cache_dir = actual(Path(data_dir).absolute() / 'weights')
        self.control = WeightControl(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.last_error = ''
        self._active = WeightSnapshot.builtin()
        self.generation = 0
        try:
            self._expected_head = self.control.fingerprint()
        except (OSError, ValueError):
            self._expected_head = 'unreadable'
        try:
            self._active = self._load('current.json')
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as error:
            self.last_error = str(error)

    @property
    def active(self):
        with self._lock:
            return self._active

    @property
    def upstream(self):
        snapshot = self.active
        return (
            WeightSnapshot.from_dict(_thaw(snapshot.local_state['upstream']))
            if snapshot.local_state
            else snapshot
        )

    @property
    def customized_characters(self):
        snapshot = self.active
        return (
            frozenset(snapshot.local_state['characters']) if snapshot.local_state else frozenset()
        )

    def set_custom_values(
        self, char_id, variant, main, sub, *, expected_version=None, cancel_event=None
    ):
        return self._set_custom(
            char_id,
            variant,
            main=main,
            sub=sub,
            expected_version=expected_version,
            cancel_event=cancel_event,
        )

    def _set_custom(
        self,
        char_id,
        variant,
        *,
        template=None,
        main=None,
        sub=None,
        expected_version=None,
        cancel_event=None,
    ):
        if self.custom_evaluator is None:
            raise ValueError('自定义权重保存需要显式的 Rust 计算服务')

        with self.operation():
            self.assert_current()
            active = self.active
            if expected_version is not None and active.version != expected_version:
                raise WeightConflictError('权重已变化，请先点击“放弃未保存修改”，再重新编辑和保存')
            current = active.template(char_id, variant)
            arguments = dict(
                identity=(char_id, variant),
                weight_version=active.version,
                cancel_event=cancel_event,
            )
            template = (
                self.custom_evaluator.validate(current, template, **arguments)
                if template is not None
                else self.custom_evaluator.build(current, main, sub, **arguments)
            )
            checkpoint(cancel_event)
            pins = _thaw(active.local_state['characters']) if active.local_state else {}
            if char_id not in pins:
                # First edit freezes every modal of the character, not just the
                # currently selected one. Other characters still follow updates.
                pins[char_id] = {
                    'templates': _thaw(active.templates[char_id]),
                    'conditions': _thaw(active.conditions.get(char_id, ())),
                }
            pins[char_id]['templates'][variant] = _thaw(template)
            return self._publish(
                customized_snapshot(self.upstream, pins), cancel_event=cancel_event
            )

    def reset_custom_character(self, char_id, *, expected_version=None, cancel_event=None):
        with self.operation():
            self.assert_current()
            active = self.active
            if expected_version is not None and active.version != expected_version:
                raise WeightConflictError('权重已变化，请重新选择角色后再操作')
            pins = _thaw(active.local_state['characters']) if active.local_state else {}
            if char_id not in pins:
                return False
            if char_id not in self.upstream.templates:
                raise ValueError('更新来源已移除此角色；请保留自定义权重')
            del pins[char_id]
            return self._publish(
                customized_snapshot(self.upstream, pins), cancel_event=cancel_event
            )

    def operation(self):
        return self.control.lease()

    def assert_current(self):
        with self._lock:
            try:
                self.control.record()
            except FileNotFoundError:
                pass
            if self.control.fingerprint() != self._expected_head:
                raise WeightConflictError(
                    '本机权重已被其他实例修改；请先后台重新加载本机已保存权重，再检查'
                )

    def _load_version(self, version):
        path = actual(self.cache_dir / (version + '.json'))
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Snapshot exceeds cache size limit')
        with path.open('rb') as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Snapshot exceeds cache size limit')
        snapshot = WeightSnapshot.from_dict(json.loads(raw))
        if snapshot.version != version:
            raise ValueError('Cache pointer mismatch')
        return snapshot

    def _load(self, pointer):
        return self._load_version(self.control.record(pointer)['version'])

    def reload(self, *, cancel_event=None):
        with self.operation():
            checkpoint(cancel_event)
            before = self.control.fingerprint()
            try:
                snapshot = self._load('current.json')
            except FileNotFoundError:
                snapshot = WeightSnapshot.builtin()
            checkpoint(cancel_event)
            if self.control.fingerprint() != before:
                raise WeightConflictError('加载期间权重指针已变化；当前快照保留')
            with self._lock:
                changed = snapshot.version != self._active.version
                self._active = snapshot
                self._expected_head = before
                if changed:
                    self.generation += 1
                self.last_error = ''
                return changed

    def _store_snapshot(self, snapshot):
        try:
            existing = self._load_version(snapshot.version)
            if existing.version == snapshot.version:
                return
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        self._atomic_write(snapshot.version + '.json', snapshot.to_dict())

    def _atomic_write(self, name, data, *, cancel_event=None):
        actual(self.cache_dir / name)
        fd, temp = tempfile.mkstemp(prefix='.weights-', dir=self.cache_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(_json(data) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            if name == 'current.json':
                self.assert_current()
            # The last cancellation boundary is after fsync, before replace.
            # Never turn a successful replacement into a cancelled result.
            checkpoint(cancel_event)
            os.replace(temp, self.cache_dir / name)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def activate(self, snapshot, *, cancel_event=None):
        if not isinstance(snapshot, WeightSnapshot):
            raise ValueError('Expected a validated snapshot')
        with self.operation():
            if self.active.local_state and snapshot.local_state is None:
                snapshot = customized_snapshot(snapshot, self.active.local_state['characters'])
            return self._publish(snapshot, cancel_event=cancel_event)

    def _publish(self, snapshot, *, cancel_event=None):
        if not isinstance(snapshot, WeightSnapshot):
            raise ValueError('Expected a validated snapshot')
        with self.operation():
            checkpoint(cancel_event)
            self.assert_current()
            if snapshot.version == self.active.version:
                return False
            try:
                head = self.control.record()
                revision = head.get('revision', 0)
            except FileNotFoundError:
                head = {}
                revision = 0
            if revision >= 2**63 - 1:
                raise ValueError('权重提交代次达到上限；保留原指针')
            # Operation ownership serializes publishers, not readers of active.
            # Large payload IO/validation must not hold the active-reference lock.
            self._store_snapshot(snapshot)
            checkpoint(cancel_event)
            self.assert_current()
            data = dict(version=snapshot.version, pointer_schema_version=3, revision=revision + 1)
            if cancel_event is None:
                self._atomic_write('current.json', data)
            else:
                self._atomic_write('current.json', data, cancel_event=cancel_event)
            with self._lock:
                self._expected_head = hashlib.sha256(
                    (_json(data) + '\n').encode('utf-8')
                ).hexdigest()
                self._active = snapshot
                self.generation += 1
                self.last_error = ''
            return True
