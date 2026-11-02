# 读取、校验和原子导入策略评分数据包；只处理声明式 JSON，不执行远程代码。
import json
import os
import tempfile
from pathlib import Path

from ww_echo_workshop.core.policy import PolicyError
from ww_echo_workshop.core.policy_presets import validate_data_pack

MAX_BYTES = 8 * 1024 * 1024
BUILTIN_PATH = Path(__file__).resolve().parents[1] / 'resources' / 'policy' / 'echo-data.json'


class PolicyDataStore:
    def __init__(self, data_dir, *, builtin_path=None):
        self.path = Path(data_dir) / 'policy' / 'echo-data.json'
        self.builtin_path = Path(builtin_path) if builtin_path is not None else BUILTIN_PATH

    def _read(self, path):
        size = path.stat().st_size
        if size <= 0 or size > MAX_BYTES:
            raise PolicyError('invalid_data', '策略数据包大小无效')
        data = json.loads(path.read_text(encoding='utf-8'))
        return validate_data_pack(data)

    def load_with_source(self):
        try:
            if self.path.is_file():
                return self._read(self.path), 'imported'
            if self.builtin_path.is_file():
                return self._read(self.builtin_path), 'builtin'
            return None, 'none'
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PolicyError('invalid_data', f'策略数据包无法读取：{error}') from error

    def load(self):
        return self.load_with_source()[0]

    def import_bytes(self, content):
        if isinstance(content, str):
            content = content.encode('utf-8')
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise PolicyError('invalid_data', '策略数据包为空')
        if len(content) > MAX_BYTES:
            raise PolicyError('invalid_data', '策略数据包超过 8 MiB')
        try:
            data = json.loads(bytes(content).decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise PolicyError('invalid_data', f'策略数据包不是有效 JSON：{error}') from error
        validate_data_pack(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode='wb', dir=self.path.parent, prefix='.echo-data-', suffix='.tmp', delete=False
        )
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(bytes(content))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return data

    def import_file(self, path):
        try:
            content = Path(path).read_bytes()
        except OSError as error:
            raise PolicyError('invalid_data', f'策略数据包无法读取：{error}') from error
        return self.import_bytes(content)

    @staticmethod
    def version(pack):
        return str(pack.get('version', '')) if isinstance(pack, dict) else ''
