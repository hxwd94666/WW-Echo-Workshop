# 读取可复现的内置声明式权重快照。
from importlib.resources import files
import json

_DATA = json.loads(
    files('echo_workshop.resources').joinpath('builtin/weights.json').read_text(encoding='utf-8')
)
TEMPLATES = _DATA['templates']
CONDITIONS = _DATA['conditions']
SOURCE_REPOSITORY = _DATA['source_repository']
SOURCE_COMMIT = _DATA['source_commit']
