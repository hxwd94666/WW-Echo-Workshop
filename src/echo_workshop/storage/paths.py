# 从宿主配置路径解析插件用户数据根目录，拒绝安装目录内的数据。
from pathlib import Path

from echo_workshop.resources.paths import plugin_root


def data_dir_for_config(config_file):
    if not isinstance(config_file, (str, Path)) or not str(config_file):
        raise ValueError('Host config_file is required')
    root = Path(config_file).resolve().parent / 'plugin_data' / 'echo-workshop'
    installation = plugin_root()
    if root == installation or installation in root.parents:
        raise ValueError('User data must not be inside the plugin installation')
    return root
