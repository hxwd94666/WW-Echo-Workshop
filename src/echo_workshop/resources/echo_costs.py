# 读取经静态审查的声骸 COST 目录，未知或同名冲突保持未确定。
from functools import lru_cache
import json
from types import MappingProxyType

from echo_workshop.resources.paths import builtin_path


@lru_cache(maxsize=1)
def known_costs():
    path = builtin_path('echo_costs.json')
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError('声骸 COST 目录大小无效')
    data = json.loads(path.read_bytes())
    if data.get('schema_version') != 1 or not isinstance(data.get('items'), dict):
        raise ValueError('声骸 COST 目录格式错误')
    result = {}
    for ident, row in data['items'].items():
        name, cost = row['name'], row['cost']
        if (
            not ident.isdigit()
            or not isinstance(name, str)
            or not name
            or type(cost) is not int
            or cost not in (1, 3, 4)
        ):
            raise ValueError('声骸 COST 条目错误')
        if name in result and result[name] != cost:
            result[name] = None
        else:
            result[name] = cost
    return MappingProxyType(result)


def known_cost(name):
    return known_costs().get(name)
