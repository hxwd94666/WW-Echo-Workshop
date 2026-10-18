# 按冻结快照执行多角色声骸评分作业。
"""Snapshot-pinned comparison service; UI and OCR never enter this module."""

from dataclasses import dataclass
from collections import OrderedDict
from concurrent.futures import CancelledError
import threading

from echo_workshop.core.scoring import ELEMENT_NAMES, EchoScoreResult
from echo_workshop.core.scoring import ALGORITHM_VERSION
from echo_workshop.resources.weights import WeightSnapshot
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.compute_bridge import (
    compact_score,
    plain,
    prepare_rating_fields,
    record_payload,
    require_background,
)
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import ComparisonCancelled

# 紧凑匹配单元里的枚举序号，与 Rust matching.rs 的编码一一对应。
_COMPATIBILITY = ('element_match', 'element_mismatch', 'unknown')
_SONATA = ('match', 'mismatch', 'unknown')
_RECOMMENDATION = ('match', 'mismatch', 'unknown', 'excluded')
_REASON = ('', 'invalid', 'compatibility', 'slot')


def _code(table, value, message):
    if type(value) is not int or not 0 <= value < len(table):
        raise ComputeFailure('protocol', message)
    return table[value]


def _decode_score(data, standards, prepared=None):
    """Rebuild the shared score value from a compact cell and its standard table."""
    return compact_score(data, standards, prepared)


@dataclass(frozen=True, slots=True)
class ComparisonScore:
    item_id: str
    character_id: str
    modal_id: str
    weight_version: str
    algorithm_version: str
    inventory_revision: int
    score: EchoScoreResult | None
    compatibility: str
    issues: tuple[str, ...]
    # Existing scoring normalizes main stats to +25; do not imply live stats.
    main_stat_basis: str = 'level_25'
    sonata_compatibility: str = 'unknown'
    recommendation: str = 'unknown'
    explicit_rule: CompatibilityRule = CompatibilityRule()
    # 原生可见性结论：invalid / compatibility / slot / 空串（可见）。
    exclusion: str = ''


class ScoreService:
    def __init__(self, provider, *, character_elements=None, cache_entries=50000, compute=None):
        self.provider = provider
        # Supplied explicit rules, never inferred from labels or scores.
        self.character_elements = dict(character_elements or {})
        if any(value not in ELEMENT_NAMES for value in self.character_elements.values()):
            raise ValueError('Unknown character element')
        if type(cache_entries) is not int or not 0 <= cache_entries <= 50000:
            raise ValueError('Expected 0..50000 cached comparison cells')
        self.compute = compute if compute is not None else ComputeJobs()
        self._owns_compute = compute is None
        self._cache_limit = cache_entries
        self._cache = OrderedDict()
        self._cache_revision = None
        self._lock = threading.RLock()

    def compare(
        self,
        records,
        selections,
        *,
        inventory_revision,
        rules=None,
        requirements=None,
        cancel_event=None,
        snapshot=None,
    ):
        require_background()
        # Pin before waiting for another batch; a cancelled waiter never starts a stale full calculation.
        snapshot = snapshot or self.provider.active
        diagnostic = begin_job(
            'comparison',
            metadata={
                'inventory_revision': inventory_revision,
                'weight_version': getattr(snapshot, 'version', ''),
            },
        )
        acquired = False
        try:
            diagnostic.stage('waiting')
            while not self._lock.acquire(timeout=0.05):
                self._checkpoint(cancel_event)
            acquired = True
            self._checkpoint(cancel_event)
            diagnostic.stage('scoring')
            result = self._compare(
                records,
                selections,
                inventory_revision=inventory_revision,
                rules=rules,
                requirements=requirements,
                cancel_event=cancel_event,
                snapshot=snapshot,
                diagnostic=diagnostic,
            )
            diagnostic.finish('completed', 'comparison_completed', counters={'cells': len(result)})
            return result
        except ComparisonCancelled:
            diagnostic.finish('cancelled', 'comparison_cancelled')
            raise
        except Exception as error:
            diagnostic.finish('failed', 'comparison_failed', error=error)
            raise
        finally:
            if acquired:
                self._lock.release()

    @staticmethod
    def _checkpoint(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise ComparisonCancelled('Comparison cancelled')

    def _compare(
        self,
        records,
        selections,
        *,
        inventory_revision,
        rules,
        requirements,
        cancel_event,
        snapshot,
        diagnostic=None,
    ):
        snapshot = snapshot or self.provider.active
        if not isinstance(snapshot, WeightSnapshot):
            raise ValueError('Expected a fixed weight snapshot')
        rules = dict(rules or {})
        if any(
            not isinstance(key, tuple)
            or len(key) != 2
            or not all(isinstance(value, str) for value in key)
            or not isinstance(rule, CompatibilityRule)
            for key, rule in rules.items()
        ):
            raise ValueError('Expected explicit compatibility rules')
        requirements = dict(requirements or {})
        slots = {}
        for key, spec in requirements.items():
            costs = tuple(spec.costs)
            names = tuple(tuple(row) for row in (spec.main_names or ((),) * 5))
            pins = tuple(spec.fixed_item_ids or ('',) * 5)
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or len(costs) != 5
                or len(names) != 5
                or len(pins) != 5
            ):
                raise ValueError('角色的五槽要求无效；请重新确认角色设置。')
            slots[key] = dict(
                costs=costs,
                main_names=names,
                fixed_item_ids=pins,
                allowed_sonatas=tuple(spec.allowed_sonatas),
                set_requirements=tuple(tuple(row) for row in spec.set_requirements),
            )
        # Validate all chosen IDs first; no silent fallback for a removed modal.
        selected = []
        for char_id, modal in selections:
            self._checkpoint(cancel_event)
            template = snapshot.scoring_template(char_id, modal)
            rule = snapshot.compatibility_rule(
                char_id,
                rules.get(
                    (char_id, modal), CompatibilityRule(self.character_elements.get(char_id, ''))
                ),
            )
            selected.append((char_id, modal, plain(template), rule))
        revision_key = (
            inventory_revision,
            snapshot.version,
            ALGORITHM_VERSION,
            tuple(sorted(self.character_elements.items())),
            tuple(sorted(rules.items())),
            tuple(
                sorted(
                    (
                        key,
                        value['costs'],
                        value['main_names'],
                        value['fixed_item_ids'],
                        value['allowed_sonatas'],
                        value['set_requirements'],
                    )
                    for key, value in slots.items()
                )
            ),
        )
        if revision_key != self._cache_revision:
            self._cache.clear()
            self._cache_revision = revision_key
        if len(selected) > 128 or len({(row[0], row[1]) for row in selected}) != len(selected):
            raise ValueError('匹配角色/玩法重复或超过 128 项')
        materialized = []
        ids = set()
        for record in records:
            self._checkpoint(cancel_event)
            if record.item_id in ids:
                raise ValueError('匹配输入含重复 UUID')
            ids.add(record.item_id)
            materialized.append(record)
            if len(materialized) > 20000 or len(materialized) * len(selected) > 160000:
                raise ComputeFailure('input_limit', '匹配输入超过单批规模限额')
        self._checkpoint(cancel_event)
        cells = {}
        groups = {}
        keys = {}
        hits = 0
        if self._cache_limit:
            for row_index, record in enumerate(materialized):
                self._checkpoint(cancel_event)
                content = (
                    record.item_id,
                    record.profile_id,
                    record.cost,
                    record.main_stats,
                    record.sub_stats,
                    record.name,
                    record.sonata,
                    record.level,
                    record.review,
                    record.issues,
                )
                missing = []
                for column, (char_id, modal, template, rule) in enumerate(selected):
                    key = (content, char_id, modal)
                    keys[row_index, column] = key
                    value = self._cache.get(key)
                    if value is None:
                        missing.append(column)
                    else:
                        cells[row_index, column] = value
                        hits += 1
                if missing:
                    groups.setdefault(tuple(missing), []).append(row_index)
        else:
            # No result cache: skip per-cell identity keys and cache probing.
            groups = {tuple(range(len(selected))): list(range(len(materialized)))}
        if diagnostic is not None:
            diagnostic.count('roles', len(selected))
            diagnostic.count('records', len(materialized))
            diagnostic.count('cache_hits', hits)
            diagnostic.count('cache_misses', len(keys) - hits)
        for columns, indices in groups.items():
            block_size = min(512, max(1, 4096 // len(columns)))
            wire_selections = []
            for i in columns:
                char_id, modal, template, rule = selected[i]
                wire = dict(
                    character_id=char_id,
                    modal_id=modal,
                    template=template,
                    rule=rule.to_dict(),
                )
                spec = slots.get((char_id, modal))
                if spec is not None:
                    wire['slots'] = plain(spec)
                wire_selections.append(wire)
            for offset in range(0, len(indices), block_size):
                self._checkpoint(cancel_event)
                block = indices[offset : offset + block_size]

                def build_payload(block=block, wire_selections=wire_selections):
                    self._checkpoint(cancel_event)
                    return {
                        'records': [record_payload(materialized[i]) for i in block],
                        'selections': wire_selections,
                    }

                try:
                    response = self.compute.submit(
                        'match_batch', build_payload, timeout=30, cancel_event=cancel_event
                    ).result(timeout=32)
                except CancelledError as error:
                    raise ComparisonCancelled('Comparison cancelled') from error
                except ComputeFailure as error:
                    if error.kind in ('cancelled', 'closed'):
                        raise ComparisonCancelled('Comparison cancelled') from error
                    raise
                native = response.get('cells')
                ids_native = response.get('ids')
                standards = response.get('standards')
                if (
                    not isinstance(standards, list)
                    or not isinstance(ids_native, list)
                    or len(ids_native) != len(block)
                ):
                    raise ComputeFailure('protocol', '匹配批次结构错误')
                if not isinstance(native, list) or len(native) != len(block) * len(columns):
                    raise ComputeFailure('protocol', '匹配矩阵尺寸错误')
                prepared = prepare_rating_fields(standards)
                for position, entry in enumerate(native):
                    local_row = position // len(columns)
                    index = block[local_row]
                    column = columns[position % len(columns)]
                    char_id, modal, _, rule = selected[column]
                    identity = (materialized[index].item_id, char_id, modal)
                    if ids_native[local_row] != identity[0]:
                        raise ComputeFailure('request_identity', '匹配矩阵身份错误')
                    if not isinstance(entry, list) or len(entry) != 6:
                        raise ComputeFailure('protocol', '匹配单元结构错误')
                    score_entry, compatibility, sonata, recommendation, reason, issues = entry
                    if not isinstance(issues, list):
                        raise ComputeFailure('protocol', '匹配问题列表格式错误')
                    cells[index, column] = ComparisonScore(
                        *identity,
                        snapshot.version,
                        ALGORITHM_VERSION,
                        inventory_revision,
                        _decode_score(score_entry, standards, prepared),
                        _code(_COMPATIBILITY, compatibility, '匹配兼容性编码错误'),
                        tuple(issues),
                        sonata_compatibility=_code(_SONATA, sonata, '匹配套装编码错误'),
                        recommendation=_code(_RECOMMENDATION, recommendation, '匹配推荐编码错误'),
                        explicit_rule=rule,
                        exclusion=_code(_REASON, reason, '匹配可见性编码错误'),
                    )
        self._checkpoint(cancel_event)
        result = []
        for index in range(len(materialized)):
            for column in range(len(selected)):
                value = cells[index, column]
                result.append(value)
                if self._cache_limit:
                    key = keys[index, column]
                    self._cache[key] = value
                    self._cache.move_to_end(key)
                    if len(self._cache) > self._cache_limit:
                        self._cache.popitem(last=False)
        if diagnostic is not None:
            diagnostic.count('processed_cells', len(result))
        self._checkpoint(cancel_event)
        return tuple(result)

    def close(self):
        if self._owns_compute:
            self.compute.close()
