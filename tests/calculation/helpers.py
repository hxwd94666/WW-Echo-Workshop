# 构造固定计算候选、请求和可控评分服务。
from dataclasses import replace
from echo_workshop.core.allocation.models import RoleRequest, SlotRule
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.allocation.models import set_name
from echo_workshop.core.costs import MAX_TOTAL_COST, read_cost_layout
from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES
from echo_workshop.core.models import EchoRecord, StatValue
from echo_workshop.core.requirements import RoleRequirements
from echo_workshop.resources.weights import WeightSnapshot
from types import SimpleNamespace
from uuid import uuid4


def sample(profile_id, **changes):
    record = EchoRecord(
        profile_id,
        3,
        (StatValue('衍射伤害加成', 30, '30.0%'), StatValue('攻击', 100)),
        (StatValue('暴击', 6.3), StatValue('暴击伤害', 12.6)),
        name='测试声骸',
        sonata='测试套装',
        level=25,
    )
    return replace(record, **changes)


def tuned(profile, cost=1, **changes):
    main = {
        1: (StatValue('攻击%', 18), StatValue('生命', 2280)),
        3: (StatValue('衍射伤害加成', 30), StatValue('攻击', 100)),
        4: (StatValue('暴击', 22), StatValue('攻击', 150)),
    }[cost]
    record = EchoRecord(
        profile,
        cost,
        main,
        (
            StatValue('暴击', 6.3),
            StatValue('暴击伤害', 12.6),
            StatValue('攻击%', 6.4),
            StatValue('共鸣效率', 6.8),
            StatValue('共鸣技能', 6.4),
        ),
        name='声骸-' + str(uuid4())[:8],
        sonata='测试套装',
        level=25,
        review='confirmed',
    )
    return replace(record, **changes)


def role(character='1304', target=150, **changes):
    value = RoleRequest(
        character,
        'default',
        target,
        CompatibilityRule('衍射', ('测试套装',)),
        tuple(SlotRule(cost) for cost in (4, 3, 3, 1, 1)),
        (('测试套装', 5),),
    )
    return replace(value, **changes)


class FakeScores:
    def __init__(self, scores):
        self.scores = scores

    def compare(self, records, selections, **kwargs):
        return tuple(
            SimpleNamespace(
                item_id=record.item_id,
                character_id=key[0],
                modal_id=key[1],
                score=SimpleNamespace(current_score=self.scores.get((record.item_id, key[0]), 10)),
                recommendation='match',
            )
            for record in records
            for key in selections
        )


def changed_snapshot():
    data = WeightSnapshot.builtin().to_dict()
    data['templates']['1304']['default']['sub_props']['暴击'] += 0.5
    return WeightSnapshot(data['templates'], data['conditions'], source='test')


def sample_cost4(**changes):
    value = EchoRecord(
        str(uuid4()),
        4,
        (StatValue('暴击伤害', 44), StatValue('攻击', 150)),
        (
            StatValue('攻击%', 7.9),
            StatValue('攻击', 40),
            StatValue('暴击', 10.5),
            StatValue('共鸣效率', 9.2),
            StatValue('暴击伤害', 17.4),
        ),
        name='共鸣回响·鸣式·利维亚坦',
        sonata='命理崩毁之弦',
        level=25,
        review='confirmed',
    )
    return replace(value, **changes)


# 以下为测试专用辅助：生产已删除这些“仅测试调用”的编辑/计数入口
# （见 docs/maintainer/dead-code-cleanup.md §6）。


def with_cost_layout(role, costs):
    if not isinstance(role, RoleRequest):
        raise ValueError('COST 配装需要有效的角色装备要求。')
    costs = read_cost_layout(costs)
    budget = min(role.cost_budget, MAX_TOTAL_COST)
    if sum(costs) > budget:
        raise ValueError(f'COST 合计 {sum(costs)} 超过此角色已设置的预算 {budget}。')
    slots = []
    for index, (slot, cost) in enumerate(zip(role.slots, costs), 1):
        if cost != slot.cost and slot.fixed_item_id:
            raise ValueError(
                f'第 {index} 槽已固定声骸；请先解除固定或在装备要求中确认，再修改 COST。'
            )
        unsupported = tuple(
            name for name in slot.main_names if name not in MAX_MAINSTAT_VALUES[cost][0]
        )
        if unsupported:
            raise ValueError(
                f'第 {index} 槽的主词条要求（{"、".join(unsupported)}）与 COST {cost} 冲突；请先在装备要求中调整。'
            )
        slots.append(replace(slot, cost=cost))
    return replace(role, slots=tuple(slots), cost_budget=budget)


def with_role_requirements(role, value, *, rule=None):
    if not isinstance(role, RoleRequest) or not isinstance(value, RoleRequirements):
        raise ValueError('合并要求需要有效的角色配装与主词条/套装配置。')
    if rule is not None and not isinstance(rule, CompatibilityRule):
        raise ValueError('角色属性规则无效。')
    budget = min(role.cost_budget, 12)
    if sum(value.costs) > budget:
        raise ValueError(f'COST 合计 {sum(value.costs)} 超过此角色已设置的预算 {budget}。')
    slots = []
    for index, (old, cost, main) in enumerate(zip(role.slots, value.costs, value.main_names), 1):
        pin = old.fixed_item_id if value.fixed_item_ids is None else value.fixed_item_ids[index - 1]
        if pin and pin == old.fixed_item_id and old.cost != cost:
            raise ValueError(
                f'第 {index} 槽已固定声骸，修改 COST 前请确认固定件；原固定件和配置保留。'
            )
        slots.append(replace(old, cost=cost, main_names=main, fixed_item_id=pin))
    effective = replace(rule if rule is not None else role.rule, sonatas=value.allowed_sonatas)
    return replace(
        role,
        slots=tuple(slots),
        rule=effective,
        set_requirements=value.set_requirements,
        cost_budget=budget,
    )


def set_counts(records, *, unique_names=False):
    counts, names = {}, {}
    for record in records:
        sonata = set_name(record.sonata)
        if unique_names:
            names.setdefault(sonata, set()).add(set_name(record.name))
        else:
            counts[sonata] = counts.get(sonata, 0) + 1
    return {name: len(values) for name, values in names.items()} if unique_names else counts


def set_requirements_satisfied(role, records):
    counts = set_counts(records, unique_names=role.set_unique_names)
    return all(counts.get(set_name(name), 0) >= minimum for name, minimum in role.set_requirements)


def set_custom_template(provider, char_id, variant, template, **kwargs):
    return provider._set_custom(char_id, variant, template=template, **kwargs)
