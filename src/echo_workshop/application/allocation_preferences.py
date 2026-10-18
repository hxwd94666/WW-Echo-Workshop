# 校验独立配装工作区的待确认角色，不将草稿伪装为已确认装备要求。
import json

from echo_workshop.core.allocation.models import RoleRequest


def allocation_settings(config):
    raw = config.get('AllocationSettingsJSON', '')
    if not raw:
        return dict(schema_version=1, profiles={})
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise ValueError('配装配置过大或类型无效，原配置保留。')
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get('schema_version') != 1
        or not isinstance(value.get('profiles'), dict)
    ):
        raise ValueError('配装配置格式不兼容，原配置保留。')
    return value


def _key(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(
            not isinstance(part, str)
            or not part
            or part != part.strip()
            or len(part) > 128
            or any(ord(char) < 32 for char in part)
            for part in value
        )
    ):
        raise ValueError('配装角色与模版须为有效的稳定 ID。')
    return tuple(value)


def pending_role_keys(values, roles=()):
    """Return pending stable keys, checking all participants without inventing a request.

    Known-but-currently-unavailable IDs remain visible for explicit removal or
    repair. Only choosing/editing a role consults the live weight catalogue.
    """
    if not isinstance(values, (list, tuple)) or len(values) > 8:
        raise ValueError('待确认配装角色须为列表，全部参与角色最多 8 个。')
    if not isinstance(roles, (list, tuple)) or len(roles) > 8:
        raise ValueError('已确认配装角色须为列表，全部参与角色最多 8 个。')
    confirmed = []
    for role in roles:
        if isinstance(role, RoleRequest):
            confirmed.append(_key(role.key))
        elif isinstance(role, dict) and {'character_id', 'modal_id'} <= set(role):
            confirmed.append(_key((role['character_id'], role['modal_id'])))
        else:
            raise ValueError('已确认配装角色身份无效。')
    pending = tuple(_key(value) for value in values)
    characters = [key[0] for key in (*confirmed, *pending)]
    if len(characters) != len(set(characters)):
        raise ValueError('同一角色只参加一次配装；请先移除后再选择其他模版。')
    if len(characters) > 8:
        raise ValueError('本次配装最多 8 个角色，包含待确认角色。')
    return pending
