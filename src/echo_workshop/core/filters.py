# 组合库存筛选条件和多选并集。
"""View-only filters: status union, intersection across groups. No inventory mutation, scoring or Qt imports."""

from dataclasses import dataclass

from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES, SUBSTAT_TIERS
from echo_workshop.core.text import simplify_echo_text

STATUS_OPTIONS = (
    ('equipped', '已装备'),
    ('locked', '已保护'),
    ('pending', '待核对'),
    ('other', '其他'),
)
_RETIRED_STATUS_KEYS = frozenset(('discarded',))
STAT_LABELS = {
    '攻击%': '攻击百分比',
    '生命%': '生命百分比',
    '防御%': '防御百分比',
    '普攻': '普攻伤害加成',
    '重击': '重击伤害加成',
    '共鸣技能': '共鸣技能伤害加成',
    '共鸣解放': '共鸣解放伤害加成',
}
MAIN_STAT_KEYS = tuple(
    dict.fromkeys(key for slots in MAX_MAINSTAT_VALUES.values() for key in slots[0])
)
SUB_STAT_KEYS = tuple(SUBSTAT_TIERS)
_ALIASES = {label: key for key, label in STAT_LABELS.items()}


def canonical_stat(name):
    text = simplify_echo_text(name).strip()
    return _ALIASES.get(text, text)


def set_name(name):
    return simplify_echo_text(name).strip()


def sonata_options(snapshot=None, records=()):
    """Current validated catalog plus retained names; never trigger network IO."""
    names = {}
    for icon in getattr(snapshot, 'icons', {}).values():
        name = set_name(icon.name)
        if name:
            names.setdefault(name, name)
    for record in records:
        name = set_name(record.sonata)
        if name:
            names.setdefault(name, name)
    return tuple(names)


def is_protected(record, reservation=None):
    """Local manual protection or a current-loadout reservation, never game lock."""
    return bool(record.locked or reservation)


@dataclass(frozen=True)
class InventoryFilters:
    statuses: tuple[str, ...] = ()
    cost: int | None = None
    main_stat: str = ''
    sub_stats: tuple[str, ...] = ()
    sonata: str = ''
    review_mode: int = 0
    costs: tuple[int, ...] = ()
    main_stats: tuple[str, ...] = ()
    sonatas: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.review_mode) is not int or self.review_mode not in range(4):
            raise ValueError('Unknown inventory review filter')
        # Drop retired view filters from legacy callers; never hide inventory by them.
        statuses = tuple(
            key for key in dict.fromkeys(self.statuses) if key not in _RETIRED_STATUS_KEYS
        )
        subs = tuple(dict.fromkeys(canonical_stat(s) for s in self.sub_stats))
        main = canonical_stat(self.main_stat)
        if not all(
            isinstance(values, (tuple, list))
            for values in (self.costs, self.main_stats, self.sonatas)
        ):
            raise ValueError('Multi-select filters must be lists or tuples')
        if any(type(cost) is not int or cost not in (1, 3, 4) for cost in self.costs):
            raise ValueError('COST filters must contain 1, 3 or 4')
        if any(
            not isinstance(name, str) or not name.strip()
            for name in (*self.main_stats, *self.sonatas)
        ):
            raise ValueError('Filter names must be nonempty text')
        mains = tuple(dict.fromkeys(canonical_stat(name) for name in self.main_stats))
        if set(mains) - set(MAIN_STAT_KEYS):
            raise ValueError('Unknown primary stat filter')
        if set(statuses) - {key for key, _ in STATUS_OPTIONS}:
            raise ValueError('Unknown inventory status filter')
        if self.cost is not None and (type(self.cost) is not int or self.cost not in (1, 3, 4)):
            raise ValueError('COST filter must be 1, 3, 4 or all')
        if main and main not in MAIN_STAT_KEYS:
            raise ValueError('Unknown primary stat filter')
        if len(subs) > 5 or set(subs) - set(SUB_STAT_KEYS):
            raise ValueError('Choose at most five known substats')
        object.__setattr__(self, 'statuses', statuses)
        object.__setattr__(self, 'sub_stats', subs)
        object.__setattr__(self, 'main_stat', main)
        object.__setattr__(self, 'sonata', set_name(self.sonata))
        object.__setattr__(self, 'costs', tuple(cost for cost in (1, 3, 4) if cost in self.costs))
        object.__setattr__(
            self, 'main_stats', tuple(name for name in MAIN_STAT_KEYS if name in mains)
        )
        object.__setattr__(
            self, 'sonatas', tuple(sorted({set_name(name) for name in self.sonatas}))
        )

    @property
    def selected_costs(self):
        return tuple(cost for cost in (1, 3, 4) if cost in self.costs or cost == self.cost)

    @property
    def selected_main_stats(self):
        return tuple(
            name for name in MAIN_STAT_KEYS if name in self.main_stats or name == self.main_stat
        )

    @property
    def selected_sonatas(self):
        return tuple(sorted(set(self.sonatas) | ({self.sonata} if self.sonata else set())))
