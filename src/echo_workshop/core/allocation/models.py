# 定义配装请求、约束、结果及独占保护政策。
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import math
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.models import canonical_json, validate_id

POLICY_VERSION = 'exclusive-five-target-cap-v2'
PRIORITY_POLICY_VERSION = 'exclusive-five-priority-target-cap-v2'


class AllocationCancelled(Exception):
    pass


def cents(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('目标分须为有限数值')
    try:
        number = Decimal(str(value)) * 100
        if number != number.to_integral_value() or not 0 <= number <= 10**14:
            raise ValueError('目标分须不小于 0，最多两位小数')
        return int(number)
    except InvalidOperation as error:
        raise ValueError('目标分格式错误') from error


def set_name(value):
    from echo_workshop.core.text import simplify_echo_text

    return simplify_echo_text(value).strip()


@dataclass(frozen=True)
class SlotRule:
    cost: int
    main_names: tuple[str, ...] = ()
    fixed_item_id: str = ''

    def __post_init__(self):
        if type(self.cost) is not int or self.cost not in (1, 3, 4):
            raise ValueError('槽位 COST 须为 1、3 或 4')
        if not isinstance(self.main_names, tuple) or any(
            not isinstance(name, str) or not name.strip() for name in self.main_names
        ):
            raise ValueError('主词条要求须为明确名称列表')
        if not isinstance(self.fixed_item_id, str):
            raise ValueError('固定实例须为本地 UUID 文本或空白')
        if self.fixed_item_id:
            validate_id(self.fixed_item_id)

    @classmethod
    def from_dict(cls, row):
        row = dict(row)
        row['main_names'] = tuple(row['main_names'])
        return cls(**row)


@dataclass(frozen=True)
class RoleRequest:
    character_id: str
    modal_id: str
    target: float
    rule: CompatibilityRule
    slots: tuple[SlotRule, ...]
    set_requirements: tuple[tuple[str, int], ...] = ()
    cost_budget: int = 12
    distinct_names: bool = True
    owner_label: str = ''
    set_unique_names: bool = True

    def __post_init__(self):
        if (
            not isinstance(self.character_id, str)
            or not self.character_id
            or not isinstance(self.modal_id, str)
            or not self.modal_id
        ):
            raise ValueError('角色和固定模态 ID 必填')
        cents(self.target)
        if (
            not isinstance(self.rule, CompatibilityRule)
            or not self.rule.element
            or not self.rule.sonatas
        ):
            raise ValueError('配装须明确元素和允许套装；未知适配不进入分配池')
        if (
            not isinstance(self.slots, tuple)
            or len(self.slots) != 5
            or not all(isinstance(slot, SlotRule) for slot in self.slots)
        ):
            raise ValueError('每个角色须明确五个槽位')
        if type(self.cost_budget) is not int:
            raise ValueError('COST 预算须为整数')
        if type(self.distinct_names) is not bool or not isinstance(self.owner_label, str):
            raise ValueError('同名去重/历史归属标签格式错误')
        if self.set_unique_names is not True:
            raise ValueError('仅支持当前套装不同名称计件规则')
        if not isinstance(self.set_requirements, tuple):
            raise ValueError('套装数量要求须为不可变列表')
        for row in self.set_requirements:
            if (
                not isinstance(row, tuple)
                or len(row) != 2
                or not isinstance(row[0], str)
                or not row[0].strip()
                or type(row[1]) is not int
            ):
                raise ValueError('套装数量要求格式错误')

    @property
    def key(self):
        return self.character_id, self.modal_id

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, row):
        row = dict(row)
        if 'set_unique_names' not in row:
            raise ValueError('配装要求须声明当前套装名称计件政策')
        row['slots'] = tuple(SlotRule.from_dict(slot) for slot in row['slots'])
        rule = dict(row['rule'])
        if isinstance(rule.get('sonatas'), tuple):
            rule['sonatas'] = list(rule['sonatas'])
        row['rule'] = CompatibilityRule.from_dict(rule)
        row['set_requirements'] = tuple(tuple(value) for value in row['set_requirements'])
        return cls(**row)


@dataclass(frozen=True)
class AllocationRequest:
    profile_id: str
    roles: tuple[RoleRequest, ...]
    fully_tuned: bool = True
    priority_groups: tuple[tuple[tuple[str, str], ...], ...] = ()

    def __post_init__(self):
        validate_id(self.profile_id)
        if (
            not isinstance(self.roles, tuple)
            or not 1 <= len(self.roles) <= 8
            or not all(isinstance(role, RoleRequest) for role in self.roles)
        ):
            raise ValueError('配装参与角色数须为 1–8')
        if len({role.character_id for role in self.roles}) != len(self.roles):
            raise ValueError('同一角色只选一个固定模态，不同时分配多种构筑')
        pins = [
            slot.fixed_item_id for role in self.roles for slot in role.slots if slot.fixed_item_id
        ]
        if len(set(pins)) != len(pins):
            raise ValueError('同一 UUID 被多个槽位固定')
        if type(self.fully_tuned) is not bool:
            raise ValueError('完整调谐选项格式错误')
        if not isinstance(self.priority_groups, tuple):
            raise ValueError('优先级须为不可变分组')
        if self.priority_groups:
            keys = []
            for group in self.priority_groups:
                if not isinstance(group, tuple) or not group:
                    raise ValueError('优先级分组须包含角色')
                for key in group:
                    if (
                        not isinstance(key, tuple)
                        or len(key) != 2
                        or any(not isinstance(value, str) or not value for value in key)
                    ):
                        raise ValueError('优先级角色须为固定角色/模版 ID')
                    keys.append(key)
            if len(keys) != len(self.roles) or set(keys) != {role.key for role in self.roles}:
                raise ValueError('优先级须恰好包含所有参与角色各一次')

    def to_dict(self):
        row = asdict(self)
        row['roles'] = [role.to_dict() for role in self.roles]
        # The current schema omits an empty explicit-priority partition.
        if not self.priority_groups:
            row.pop('priority_groups')
        return row

    @property
    def version(self):
        return hashlib.sha256(canonical_json(self.to_dict()).encode('utf-8')).hexdigest()

    @classmethod
    def from_dict(cls, row):
        row = dict(row)
        row['roles'] = tuple(RoleRequest.from_dict(role) for role in row['roles'])
        if 'priority_groups' in row:
            groups = row['priority_groups']
            if (
                not isinstance(groups, (list, tuple))
                or any(not isinstance(group, (list, tuple)) for group in groups)
                or any(not isinstance(key, (list, tuple)) for group in groups for key in group)
            ):
                raise ValueError('优先级分组格式错误')
            row['priority_groups'] = tuple(tuple(tuple(key) for key in group) for group in groups)
        return cls(**row)


@dataclass(frozen=True)
class RoleAllocation:
    character_id: str
    modal_id: str
    label: str
    item_ids: tuple[str, ...]
    scores: tuple[int, ...]
    total_cents: int
    target_cents: int
    moves: int
    reasons: tuple[str, ...]
    # 评级与分值同源：由 Rust 在产出分值时冻结，界面与方案载荷直接消费。
    rating: dict | None = None
    item_ratings: tuple = ()


@dataclass(frozen=True)
class AllocationResult:
    plan_id: str
    request: AllocationRequest
    inventory_revision: int
    weight_version: str
    algorithm_version: str
    policy_version: str
    roles: tuple[RoleAllocation, ...]
    exhaustive: bool
    stop_reason: str
    nodes: int
    elapsed: float
    objective: tuple

    def to_dict(self):
        row = asdict(self)
        row['request'] = self.request.to_dict()
        # 评级是附加冻结证据：缺失时不写出，旧载荷与既有样本保持逐字节一致。
        for role in row['roles']:
            if role.get('rating') is None:
                role.pop('rating', None)
            if not role.get('item_ratings'):
                role.pop('item_ratings', None)
        return row
