# 统一定位内置资源、安装目录和当前包版本信息。
from importlib.resources import files
import json
from pathlib import Path

from echo_workshop import __version__


def builtin_path(name=''):
    """插件安装为目录；资源位置不依赖调用模块的相邻文件。"""
    if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Invalid builtin resource name')
    return Path(str(files('echo_workshop.resources').joinpath('builtin', name)))


def plugin_root():
    return Path(str(files('echo_workshop'))).resolve().parent


def package_manifest():
    path = plugin_root() / 'manifest.json'
    if path.is_file():
        if path.is_symlink() or path.stat().st_size > 8192:
            raise ValueError('Invalid plugin manifest')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('version'), str):
            raise ValueError('Invalid plugin manifest fields')
        return data
    return {'file_name': 'echo-workshop', 'script_name': '声骸工坊', 'version': __version__}
