# 校验候选筛选的输入结构，库存求值由 Rust 查询服务执行。
from datetime import datetime, timezone
import math


def validate_filter_options(
    *,
    text='',
    cost=None,
    mode='confirmed',
    fully_tuned=False,
    max_age_days=0,
    now=None,
    minimum_level=0,
    exclude_locked=False,
    include_equipped=True,
    protected_ids=(),
):
    if cost not in (None, 1, 3, 4) or mode not in ('confirmed', 'pending', 'all'):
        raise ValueError('Unknown inventory filter')
    if (
        type(max_age_days) not in (int, float)
        or not math.isfinite(max_age_days)
        or max_age_days < 0
    ):
        raise ValueError('Invalid capture age limit')
    if (
        type(minimum_level) is not int
        or minimum_level not in (0, 5, 10, 15, 20, 25)
        or type(exclude_locked) is not bool
        or type(include_equipped) is not bool
    ):
        raise ValueError('计算范围选项无效')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Filter time must include timezone')
    return dict(
        text=text,
        cost=int(cost) if cost is not None else None,
        mode=mode,
        fully_tuned=bool(fully_tuned),
        max_age_days=max_age_days,
        now=now,
        minimum_level=minimum_level,
        exclude_locked=exclude_locked,
        include_equipped=include_equipped,
        protected_ids=tuple(value for value in frozenset(protected_ids) if isinstance(value, str)),
    )
