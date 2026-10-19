# 定义每档案独立配装搜索预算和显式偏好写入。
from dataclasses import asdict, dataclass
import json

from echo_workshop.core.allocation.models import AllocationRequest
from echo_workshop.core.allocation_preferences import pending_role_keys
from echo_workshop.core.models import canonical_json, validate_id


@dataclass(frozen=True)
class SearchBudget:
    seconds: int = 10
    nodes: int = 200000

    def __post_init__(self):
        if type(self.seconds) is not int or not 1 <= self.seconds <= 120:
            raise ValueError('配装搜索时间须为 1–120 秒。')
        if type(self.nodes) is not int or not 1 <= self.nodes <= 5000000:
            raise ValueError('配装搜索节点须为 1–5000000。')

    @classmethod
    def from_dict(cls, row):
        if not isinstance(row, dict) or set(row) != {'seconds', 'nodes'}:
            raise ValueError('配装搜索预算字段无效，原设置保留。')
        return cls(row['seconds'], row['nodes'])

    def to_dict(self):
        return asdict(self)

    def solver_kwargs(self):
        return {'seconds': self.seconds, 'max_nodes': self.nodes}


def _unique_pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError('档案设置含重复字段，原设置保留。')
        data[key] = value
    return data


def _settings(config, key):
    raw = config.get(key, '')
    if raw == '':
        return {'schema_version': 1, 'profiles': {}}
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise ValueError('档案设置类型或大小无效，原设置保留。')
    try:
        data = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError) as error:
        raise ValueError('档案设置格式无效，原设置保留。') from error
    if (
        not isinstance(data, dict)
        or set(data) != {'schema_version', 'profiles'}
        or type(data['schema_version']) is not int
        or data['schema_version'] != 1
        or not isinstance(data['profiles'], dict)
    ):
        raise ValueError('档案设置版本或结构不兼容，原设置保留。')
    return data


def _allocation_row(data, profile_id, *, create=False):
    if profile_id not in data['profiles']:
        return {'roles': [], 'fully_tuned': True} if create else None
    row = data['profiles'][profile_id]
    required = {'roles', 'fully_tuned'}
    optional = {'priority_groups', 'search_budget', 'pending_roles', 'candidate_item_ids'}
    if (
        not isinstance(row, dict)
        or not required <= set(row) <= required | optional
        or not isinstance(row['roles'], list)
        or type(row['fully_tuned']) is not bool
    ):
        raise ValueError('配装档案设置字段错误，原设置保留。')
    pending_role_keys(row.get('pending_roles', []), row['roles'])
    request_row = {
        key: value
        for key, value in row.items()
        if key not in ('search_budget', 'pending_roles', 'candidate_item_ids')
    }
    if row['roles']:
        AllocationRequest.from_dict(dict(profile_id=profile_id, **request_row))
    elif row.get('priority_groups', []) != []:
        raise ValueError('空配装角色只接受空优先级分组。')
    return row


def allocation_budget(config, profile_id):
    """Read this profile's explicit budget; absent means the current default."""
    validate_id(profile_id)
    data = _settings(config, 'AllocationSettingsJSON')
    row = _allocation_row(data, profile_id)
    if row is not None and 'search_budget' in row:
        return SearchBudget.from_dict(row['search_budget'])
    return SearchBudget()


def set_allocation_budget(config, profile_id, budget):
    """Explicit single-field preference write; never change matching or roles."""
    validate_id(profile_id)
    if not isinstance(budget, SearchBudget):
        raise ValueError('请提供有效的配装搜索预算。')
    data = _settings(config, 'AllocationSettingsJSON')
    row = dict(_allocation_row(data, profile_id, create=True))
    row['search_budget'] = budget.to_dict()
    data['profiles'][profile_id] = row
    raw = canonical_json(data)
    if len(raw) > 1024 * 1024:
        raise ValueError('档案设置超过大小限制，原设置保留。')
    config['AllocationSettingsJSON'] = raw
    return budget
