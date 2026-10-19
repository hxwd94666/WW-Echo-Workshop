# 定义声骸与词条实体和无损序列化规则。
"""Geometry-free, lossless inventory records. No game item identity is assumed."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import PurePosixPath
from types import MappingProxyType
from uuid import UUID, uuid4

from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES, SUBSTAT_TIERS


# Observation acceptance is not the legacy +25 scoring normalization basis.
# XWUID's local role_info_change.phantom_main_value table declares COST4 DEF
# 41.8% and healing 26.4%; using 41.5/26.0 scoring constants rejected these
# candidates. Do not mutate scoring coefficients, snap OCR, or infer a level
# curve/rarity from that replacement-tool table. Provenance/audit are in docs.
_validation_limits = {
    cost: tuple(dict(slot) for slot in slots) for cost, slots in MAX_MAINSTAT_VALUES.items()
}
_validation_limits[1][0].update({'防御%': 18.0})
_validation_limits[4][0].update({'防御%': 41.8, '治疗效果加成': 26.4})
MAINSTAT_VALIDATION_MAXIMA = MappingProxyType(
    {
        cost: tuple(MappingProxyType(slot) for slot in slots)
        for cost, slots in _validation_limits.items()
    }
)
del _validation_limits


def cost_from_main_stats(main_stats, *, level=None):
    """Infer a unique +25 COST from the two main-stat name/value pairs."""
    if level != 25 or len(main_stats) != 2:
        return None
    matches = []
    for cost, slots in MAINSTAT_VALIDATION_MAXIMA.items():
        if all(_main_stat_matches(slot, stat, cost) for slot, stat in zip(slots, main_stats)):
            matches.append(cost)
    return matches[0] if len(matches) == 1 else None


def _main_stat_matches(slot, stat, cost):
    amount = slot.get(stat.stat_name)
    if amount is None or not isinstance(stat.value, (int, float)):
        return False
    if abs(stat.value - amount) <= 0.011:
        return True
    if cost == 4 and stat.stat_name in ('防御%', '治疗效果加成'):
        alternate = MAX_MAINSTAT_VALUES[4][0].get(stat.stat_name)
        return alternate is not None and abs(stat.value - alternate) <= 0.011
    return False


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    )


def validate_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Expected a canonical local UUID')


@dataclass(frozen=True)
class StatValue:
    stat_name: str
    value: float
    value_text: str = ''

    def __post_init__(self):
        if not isinstance(self.stat_name, str) or not self.stat_name:
            raise ValueError('Stat name is required')
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not math.isfinite(self.value)
        ):
            raise ValueError('Stat value must be finite')
        if not isinstance(self.value_text, str):
            raise ValueError('Raw OCR value must be text')
        object.__setattr__(self, 'value', float(self.value))


@dataclass(frozen=True)
class EchoRecord:
    profile_id: str
    cost: int
    main_stats: tuple[StatValue, ...]
    sub_stats: tuple[StatValue, ...] = ()
    item_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ''
    sonata: str = ''
    level: int | None = None
    captured_at: str = field(default_factory=utc_now)
    raw_text: str = ''
    screenshot_ref: str = ''
    source: str = 'ocr'
    issues: tuple[str, ...] = ()
    review: str = 'pending'
    locked: bool = False
    owner: str = ''
    owner_updated_at: str = ''
    note: str = ''
    handled: bool = False
    owner_state: str = 'unknown'
    owner_character_id: str = ''
    # Game observations are distinct from local protection and reservations.
    game_discarded: bool | None = None
    game_locked: bool | None = None
    game_equipped: bool | None = None
    game_status_updated_at: str = ''

    def __post_init__(self):
        validate_id(self.item_id)
        validate_id(self.profile_id)
        if type(self.cost) is not int or self.cost not in (1, 3, 4):
            raise ValueError('COST must be 1, 3 or 4')
        if self.level is not None and (type(self.level) is not int or not 0 <= self.level <= 25):
            raise ValueError('Level must be 0..25 or unknown')
        if not isinstance(self.main_stats, tuple) or not isinstance(self.sub_stats, tuple):
            raise ValueError('Stats must be immutable tuples')
        if not all(isinstance(stat, StatValue) for stat in self.main_stats + self.sub_stats):
            raise ValueError('Invalid stat record')
        if self.review not in ('pending', 'confirmed'):
            raise ValueError('Unknown review state')
        if type(self.locked) is not bool or type(self.handled) is not bool:
            raise ValueError('Invalid inventory flag')
        for attr in ('game_discarded', 'game_locked', 'game_equipped'):
            if getattr(self, attr) is not None and type(getattr(self, attr)) is not bool:
                raise ValueError('Game observations must be boolean or unknown')
        if self.owner_state not in ('unknown', 'equipped', 'unequipped', 'ambiguous'):
            raise ValueError('Invalid observed owner state')
        if not isinstance(self.owner_character_id, str) or (
            self.owner_character_id and not re.fullmatch(r'[0-9]{1,8}', self.owner_character_id)
        ):
            raise ValueError('Invalid observed character ID')
        if self.owner_state == 'unequipped' and (self.owner or self.owner_character_id):
            raise ValueError('Unequipped observation cannot have an owner')
        if self.owner_state == 'equipped' and (not self.owner or not self.owner_character_id):
            raise ValueError('Equipped observation needs a name and unique character ID')
        if self.owner_state in ('unknown', 'ambiguous') and self.owner_character_id:
            raise ValueError('Unresolved observation cannot pick a character ID')
        for attr in (
            'name',
            'sonata',
            'captured_at',
            'raw_text',
            'screenshot_ref',
            'source',
            'owner',
            'owner_updated_at',
            'note',
            'game_status_updated_at',
        ):
            if not isinstance(getattr(self, attr), str):
                raise ValueError(f'{attr} must be text')
        if not isinstance(self.issues, tuple) or not all(
            isinstance(issue, str) for issue in self.issues
        ):
            raise ValueError('Issues must be immutable text')
        if self.screenshot_ref:
            path = PurePosixPath(self.screenshot_ref)
            if (
                path.is_absolute()
                or '..' in path.parts
                or '\\' in self.screenshot_ref
                or ':' in self.screenshot_ref
            ):
                raise ValueError('Screenshot reference must stay inside the data directory')

    @property
    def validation_issues(self):
        """Test/legacy structural projection; production uses native quality facts."""
        problems = list(self.issues)
        for field_name in ('name', 'sonata', 'level'):
            if getattr(self, field_name) in (None, ''):
                problems.append(f'missing:{field_name}')
        slots = MAINSTAT_VALIDATION_MAXIMA[self.cost]
        if len(self.main_stats) != 2:
            problems.append('main:expected_two_slots')
        for index, stat in enumerate(self.main_stats):
            maximum = slots[index].get(stat.stat_name) if index < 2 else None
            # The +25 table is an upper bound only at max level; lower-level
            # main stats scale down and must not be rejected by that bound.
            if (
                maximum is None
                or not 0 < stat.value
                or (self.level == 25 and stat.value > maximum + 0.01)
            ):
                problems.append(f'main:invalid:{index}')
        if len(self.sub_stats) > 5 or len({stat.stat_name for stat in self.sub_stats}) != len(
            self.sub_stats
        ):
            problems.append('sub:duplicate_or_excess')
        for stat in self.sub_stats:
            tiers = SUBSTAT_TIERS.get(stat.stat_name)
            if tiers is None or not any(abs(stat.value - tier) <= 0.011 for tier in tiers):
                problems.append(f'sub:invalid_roll:{stat.stat_name}')
        if self.level is not None and len(self.sub_stats) > self.level // 5:
            problems.append('sub:exceeds_level_capacity')
        return tuple(dict.fromkeys(problems))

    @property
    def usable(self):
        """Test/legacy convenience only; production must consume native facts."""
        return self.review == 'confirmed' and not self.validation_issues

    @property
    def fully_tuned(self):
        return len(self.sub_stats) == 5

    @property
    def fingerprint(self):
        # Raw OCR, geometry, evidence, owner and UUID are not physical identity.
        # Equal fingerprints indicate candidates for review, not the same item.
        content = {
            'name': self.name,
            'sonata': self.sonata,
            'cost': self.cost,
            'level': self.level,
            'main': [(s.stat_name, s.value) for s in self.main_stats],
            'sub': sorted((s.stat_name, s.value) for s in self.sub_stats),
        }
        return hashlib.sha256(canonical_json(content).encode('utf-8')).hexdigest()

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('Inventory record must be an object')
        row = dict(data)
        # 旧库存/旧档案包里的已停用归档标记只丢弃，不让整库不可读。
        row.pop('archived', None)
        try:
            row['main_stats'] = tuple(StatValue(**stat) for stat in row['main_stats'])
            row['sub_stats'] = tuple(StatValue(**stat) for stat in row.get('sub_stats', []))
            # A successful metadata re-read recovered the value; keep the old
            # raw observation but do not keep the record pending forever.
            row['issues'] = tuple(
                issue
                for issue in row.get('issues', [])
                if not issue.startswith('ocr:metadata_recovered:')
            )
            return cls(**row)
        except (KeyError, TypeError) as error:
            raise ValueError('Malformed inventory record') from error
