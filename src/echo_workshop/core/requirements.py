# 定义并校验角色配装要求。
"""Stable-role main/set constraints; no guessed set tiers or presentation state."""

from dataclasses import dataclass

from echo_workshop.core.allocation.models import RoleRequest, normalize_energy_range
from echo_workshop.core.costs import read_cost_layout
from echo_workshop.core.models import validate_id


def normalize_fixed_items(values):
    if values is None:
        return None  # Legacy requirements never specified pins: preserve them.
    if not isinstance(values, (tuple, list)) or len(values) != 5:
        raise ValueError('固定声骸须对应五个槽位。')
    for value in values:
        if not isinstance(value, str):
            raise ValueError('固定声骸须为 UUID 或空白。')
        if value:
            validate_id(value)
    ids = [value for value in values if value]
    if len(ids) != len(set(ids)):
        raise ValueError('同一声骸不能固定到多个槽位。')
    return tuple(values)


def normalize_main_requirements(costs, rows):
    read_cost_layout(costs)
    if not isinstance(rows, (tuple, list)) or len(rows) != 5:
        raise ValueError('主词条要求须对应五个槽位；空列表表示该槽不限。')
    values = []
    for index, names in enumerate(rows, 1):
        if not isinstance(names, (tuple, list)) or any(
            not isinstance(name, str) or not name.strip() for name in names
        ):
            raise ValueError(f'第 {index} 槽主词条须为明确名称列表，历史多选要求不自动删减。')
        values.append(tuple(names))
    return tuple(values)


def normalize_set_requirements(allowed_sonatas, rows):
    if (
        not isinstance(allowed_sonatas, (tuple, list))
        or len(allowed_sonatas) > 512
        or any(not isinstance(name, str) or not name.strip() for name in allowed_sonatas)
    ):
        raise ValueError('允许套装须为明确名称列表。')
    allowed = tuple(allowed_sonatas)
    if not isinstance(rows, (tuple, list)) or len(rows) > 5:
        raise ValueError('套装件数要求须为列表，总件数不超过五件。')
    requirements = []
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError('套装件数要求须为名称与最少件数。')
        name, count = row
        if not isinstance(name, str) or not name.strip() or type(count) is not int:
            raise ValueError('套装件数须为名称与整数件数。')
        requirements.append((name, count))
    return allowed, tuple(requirements)


@dataclass(frozen=True)
class RoleRequirements:
    costs: tuple[int, ...]
    main_names: tuple[tuple[str, ...], ...]
    allowed_sonatas: tuple[str, ...]
    set_requirements: tuple[tuple[str, int], ...] = ()
    fixed_item_ids: tuple[str, ...] | None = None
    weapon_id: str = ''
    energy_regen_min: float = 100.0
    energy_regen_max: float | None = None
    base_energy_regen: float = 100.0

    def __post_init__(self):
        costs = read_cost_layout(self.costs)
        main = normalize_main_requirements(costs, self.main_names)
        allowed, sets = normalize_set_requirements(self.allowed_sonatas, self.set_requirements)
        object.__setattr__(self, 'costs', costs)
        object.__setattr__(self, 'main_names', main)
        object.__setattr__(self, 'allowed_sonatas', allowed)
        object.__setattr__(self, 'set_requirements', sets)
        object.__setattr__(self, 'fixed_item_ids', normalize_fixed_items(self.fixed_item_ids))
        if not isinstance(self.weapon_id, str) or any(c < ' ' for c in self.weapon_id):
            raise ValueError('武器 ID 格式错误')
        minimum, maximum, base = normalize_energy_range(
            self.energy_regen_min, self.energy_regen_max, self.base_energy_regen
        )
        object.__setattr__(self, 'energy_regen_min', minimum)
        object.__setattr__(self, 'energy_regen_max', maximum)
        object.__setattr__(self, 'base_energy_regen', base)

    def to_dict(self):
        row = dict(
            costs=list(self.costs),
            main_names=[list(names) for names in self.main_names],
            allowed_sonatas=list(self.allowed_sonatas),
            set_requirements=[list(row) for row in self.set_requirements],
            weapon_id=self.weapon_id,
            energy_regen_min=self.energy_regen_min,
            energy_regen_max=self.energy_regen_max,
            base_energy_regen=self.base_energy_regen,
        )
        if self.fixed_item_ids is not None:
            row['fixed_item_ids'] = list(self.fixed_item_ids)
        return row

    @classmethod
    def from_dict(cls, row):
        fields = {
            'costs',
            'main_names',
            'allowed_sonatas',
            'set_requirements',
        }
        optional = {
            'fixed_item_ids',
            'weapon_id',
            'energy_regen_min',
            'energy_regen_max',
            'base_energy_regen',
        }
        if not isinstance(row, dict) or not fields <= set(row) <= fields | optional:
            raise ValueError('角色主词条与套装要求配置字段无效；原配置保留。')
        return cls(**row)

    @classmethod
    def from_role(cls, role):
        if not isinstance(role, RoleRequest):
            raise ValueError('读取要求需要有效的角色配装。')
        # Historical RoleRequest remains readable on its old path. New specs
        # reject oversized historical layouts instead of replacing their data.
        return cls(
            tuple(slot.cost for slot in role.slots),
            tuple(slot.main_names for slot in role.slots),
            role.rule.sonatas,
            role.set_requirements,
            tuple(slot.fixed_item_id for slot in role.slots),
            role.weapon_id,
            role.energy_regen_min,
            role.energy_regen_max,
            role.base_energy_regen,
        )


def _key(value):
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise ValueError('主词条与套装要求须对应固定角色和模版 ID。')
    return tuple(value)


def requirements_to_rows(values):
    if not isinstance(values, dict) or len(values) > 512:
        raise ValueError('角色要求配置无效。')
    rows = []
    for key, value in values.items():
        key = _key(key)
        if not isinstance(value, RoleRequirements):
            raise ValueError('角色要求配置值无效。')
        rows.append(dict(character_id=key[0], modal_id=key[1], **value.to_dict()))
    return rows


def requirements_from_rows(rows):
    if not isinstance(rows, (tuple, list)) or len(rows) > 512:
        raise ValueError('角色要求配置格式无效；原配置保留。')
    values = {}
    fields = {
        'character_id',
        'modal_id',
        'costs',
        'main_names',
        'allowed_sonatas',
        'set_requirements',
    }
    for row in rows:
        optional = {
            'fixed_item_ids',
            'weapon_id',
            'energy_regen_min',
            'energy_regen_max',
            'base_energy_regen',
        }
        if not isinstance(row, dict) or not fields <= set(row) <= fields | optional:
            raise ValueError('角色要求配置字段无效；原配置保留。')
        key = _key((row['character_id'], row['modal_id']))
        if key in values:
            raise ValueError('同一角色模版的要求配置重复；原配置保留。')
        values[key] = RoleRequirements.from_dict(
            {name: row[name] for name in set(row) - {'character_id', 'modal_id'}}
        )
    return values


role_requirements_to_rows = requirements_to_rows
role_requirements_from_rows = requirements_from_rows
