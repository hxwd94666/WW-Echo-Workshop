# 准备独立匹配计算输入和角色键。
"""Declarative matching controls; priorities affect allocation, not weights."""

from dataclasses import asdict, dataclass
from uuid import UUID

# One profile's explicit candidate list stays far below the config size limit.
MAX_CANDIDATE_IDS = 20000


def normalize_candidate_ids(values):
    """Explicit instance UUIDs; ``None`` keeps the dynamic whole-library scope.

    An empty collection is a deliberate empty scope and never means ``all``.
    """
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError('候选声骸须为实例 ID 集合。')
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError('候选声骸包含无效实例 ID。')
        try:
            canonical = str(UUID(value))
        except ValueError as error:
            raise ValueError('候选声骸包含无效实例 ID。') from error
        if canonical != value:
            raise ValueError('候选声骸包含无效实例 ID。')
        result.append(value)
    unique = tuple(sorted(set(result)))
    if len(unique) > MAX_CANDIDATE_IDS:
        raise ValueError(f'候选声骸最多 {MAX_CANDIDATE_IDS} 件。')
    return unique


def parse_candidate_ids(raw):
    """Tolerant read for restored configuration; unknown entries never block startup."""
    if not isinstance(raw, (list, tuple)):
        return None
    result = []
    for value in tuple(raw)[:MAX_CANDIDATE_IDS]:
        if not isinstance(value, str):
            continue
        try:
            canonical = str(UUID(value))
        except ValueError:
            continue
        if canonical == value:
            result.append(value)
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class MatchCalculationOptions:
    minimum_level: int = 0
    exclude_locked: bool = False
    include_equipped: bool = True
    seconds: int = 10
    nodes: int = 200000

    def __post_init__(self):
        if type(self.minimum_level) is not int or self.minimum_level not in (0, 5, 10, 15, 20, 25):
            raise ValueError('最低等级选项无效')
        if type(self.exclude_locked) is not bool or type(self.include_equipped) is not bool:
            raise ValueError('计算范围选项无效')
        if type(self.seconds) is not int or not 1 <= self.seconds <= 120:
            raise ValueError('搜索时间须为 1–120 秒')
        if type(self.nodes) is not int or not 1 <= self.nodes <= 5000000:
            raise ValueError('搜索节点上限无效')

    @classmethod
    def from_dict(cls, row):
        if not isinstance(row, dict):
            raise ValueError('计算选项格式无效；原配置保留')
        try:
            return cls(**row)
        except TypeError as error:
            raise ValueError('计算选项字段无效；原配置保留') from error

    def to_dict(self):
        return asdict(self)

    def filters(self):
        return dict(
            minimum_level=self.minimum_level,
            exclude_locked=self.exclude_locked,
            include_equipped=self.include_equipped,
        )


def stable_keys(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > 512:
        raise ValueError('角色顺序配置无效')
    keys = []
    for row in rows:
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 2
            or not all(isinstance(x, str) and x for x in row)
        ):
            raise ValueError('角色顺序身份无效')
        key = tuple(row)
        if key in keys:
            raise ValueError('角色顺序含重复项')
        keys.append(key)
    return tuple(keys)


def ordered_keys(selected, order):
    selected = tuple(selected)
    return tuple(key for key in order if key in selected) + tuple(
        key for key in selected if key not in order
    )


def links_to_groups(keys, links):
    keys = tuple(keys)
    if len(links) != max(0, len(keys) - 1) or any(value not in ('=', '>') for value in links):
        raise ValueError('优先级关系无效')
    groups = []
    for index, key in enumerate(keys):
        if not index or links[index - 1] == '>':
            groups.append([])
        groups[-1].append(key)
    return tuple(tuple(group) for group in groups)


def restrict_priority_groups(groups, keys):
    """Keep relative priority when editing a subset; new roles join last group."""
    keys = tuple(keys)
    result = [tuple(key for key in group if key in keys) for group in groups]
    result = [group for group in result if group]
    known = {key for group in result for key in group}
    new = tuple(key for key in keys if key not in known)
    if new:
        if result:
            result[-1] += new
        else:
            result.append(new)
    return tuple(result)
