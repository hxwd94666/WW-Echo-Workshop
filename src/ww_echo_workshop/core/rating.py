# 提供冻结评级数据契约与列校验；评级求值只在 Rust 侧执行。
"""Declarative XWUID grade-table fields; grading itself runs in the native engine.

Python keeps only the published table's shape validation and the frozen `Rating`
value object that the native result is mapped into. No grading mathematics is
duplicated here: `single_rating`/`loadout_rating`/`rating_template` were removed
with the native migration.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import math
from types import MappingProxyType


LABELS = ('C', 'B', 'A', 'S', 'SS', 'SSS')
COSTS = (1, 3, 4)


def project_grade_fields(template, upstream_template):
    """Custom numeric pins do not freeze newly published grade standards.

    A removed upstream role/modal retains its last known table. An existing
    upstream template missing a table makes that grade explicitly unavailable.
    This is a scoring projection only: no snapshot fields or hashes are edited.
    """
    data = dict(template)
    if isinstance(upstream_template, Mapping):
        for field in ('props_grade', 'total_grade'):
            data.pop(field, None)
            if field in upstream_template:
                data[field] = upstream_template[field]
    return MappingProxyType(data)


def validate_grade_table(values):
    """Six finite, strictly ascending ratio lower bounds, starting at zero."""
    if not isinstance(values, (list, tuple)) or len(values) != len(LABELS):
        raise ValueError('评级标准应包含 C/B/A/S/SS/SSS 六个下限')
    if any(
        type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value)
        for value in values
    ):
        raise ValueError('评级下限应为 0–1 的有限数值')
    values = tuple(float(value) for value in values)
    if values[0] != 0 or any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError('评级下限应从零开始且严格递增')
    return values


def validate_rating_tables(template):
    """Optional old tables stay optional; malformed present tables are rejected."""
    if 'props_grade' in template:
        tables = template['props_grade']
        if not isinstance(tables, (list, tuple)) or len(tables) != len(COSTS):
            raise ValueError('单件评级标准应对应 COST 1、3、4')
        for values in tables:
            validate_grade_table(values)
    if 'total_grade' in template:
        validate_grade_table(template['total_grade'])


@dataclass(frozen=True)
class Rating:
    label: str
    score: float
    scale: int
    thresholds: tuple
    cost: int | None
    standard_id: str
    source: str
    tooltip: str


@lru_cache(maxsize=512)
def _rating_tooltip(cost, thresholds):
    scope = f'单件声骸 · COST {cost} · 50 分制' if cost is not None else '五件声骸总评级 · 250 分制'
    rows = ' / '.join(f'{label}≥{value:g}' for label, value in zip(LABELS, thresholds))
    basis = (
        '按单件当前分及当次模版下限评级；不改变原分数。'
        if cost is not None
        else '仅完整五件；按上游卡片近满分修约评级，求解累计分保持原值。'
    )
    return f'{scope}\n{rows}\n{basis}'


def rating_result(data):
    if data is None:
        return None
    thresholds = tuple(data['thresholds'])
    return Rating(
        data['label'],
        data['score'],
        data['scale'],
        thresholds,
        data['cost'],
        data['standard_id'],
        data['source'],
        _rating_tooltip(data['cost'], thresholds),
    )
