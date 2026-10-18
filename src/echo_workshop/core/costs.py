# 验证五槽声骸费用及总费用限制。
"""Current five-slot COST constraints; retired oversized budgets are rejected."""

MAX_TOTAL_COST = 12
COST_OPTIONS = (1, 3, 4)
DEFAULT_COST_LAYOUT = (4, 3, 3, 1, 1)
COST_PRESETS = ((4, 3, 3, 1, 1), (4, 4, 1, 1, 1))


def read_cost_layout(costs):
    """Validate shape/types only, for explicitly displaying unchanged history."""
    if (
        not isinstance(costs, (tuple, list))
        or len(costs) != 5
        or any(type(cost) is not int or cost not in COST_OPTIONS for cost in costs)
    ):
        raise ValueError('请为五个槽位各选择 COST 1、3 或 4。')
    return tuple(costs)


def _role_key(value):
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise ValueError('COST 配装须对应固定的角色和模版 ID。')
    return tuple(value)


def cost_layouts_to_rows(layouts):
    """JSON-friendly rows, scoped by the owning profile and exact stable key."""
    if not isinstance(layouts, dict) or len(layouts) > 512:
        raise ValueError('角色 COST 配装配置无效。')
    return [
        dict(
            character_id=_role_key(key)[0],
            modal_id=_role_key(key)[1],
            costs=read_cost_layout(costs),
        )
        for key, costs in layouts.items()
    ]


def cost_layouts_from_rows(rows):
    if not isinstance(rows, (tuple, list)) or len(rows) > 512:
        raise ValueError('角色 COST 配装配置格式无效；原配置保留。')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'character_id', 'modal_id', 'costs'}:
            raise ValueError('角色 COST 配装配置字段无效；原配置保留。')
        key = _role_key((row['character_id'], row['modal_id']))
        if key in result:
            raise ValueError('同一角色模版的 COST 配装配置重复；原配置保留。')
        result[key] = read_cost_layout(row['costs'])
    return result
