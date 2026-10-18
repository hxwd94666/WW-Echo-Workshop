# 读取带来源记录的离线声骸名称目录，不执行外部代码或发起网络请求。
from functools import lru_cache
import json

from echo_workshop.resources.paths import builtin_path


@lru_cache(maxsize=1)
def builtin_echo_names():
    path = builtin_path('echo_names.json')
    if path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise ValueError('Invalid builtin echo name catalog')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Invalid builtin echo name catalog document')
    names = data.get('names')
    if (
        data.get('schema_version') != 1
        or type(data.get('schema_version')) is not int
        or not isinstance(names, list)
        or not 1 <= len(names) <= 1024
        or any(not isinstance(name, str) or not name.strip() or len(name) > 128 for name in names)
        or len(names) != len(set(names))
    ):
        raise ValueError('Invalid builtin echo names')
    return tuple(names)
