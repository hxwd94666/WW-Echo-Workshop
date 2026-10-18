# 校验和缓存角色默认套装，为未配置角色提供离线与在线快照。
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import MappingProxyType

from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.application.operations import checkpoint
from echo_workshop.resources.paths import builtin_path

SOURCES = tuple(
    'https://' + host + '/XutheringWavesUID/resource/map/1.json'
    for host in ('ww3.loping151.cn', 'ww1.loping151.top', 'ww2.loping151.top')
)
MAX_BYTES = 4 * 1024 * 1024
CACHE_SCHEMA_VERSION = 2  # v1 sorted arrays have lost source order; fetch fresh before using 304.
_ID = re.compile(r'[1-9][0-9]{0,7}\Z')
_NAME = re.compile(r'[\u4e00-\u9fff·・A-Za-z0-9 _-]{1,64}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')


def digest(body):
    return hashlib.sha256(body).hexdigest()


def checked_entries(characters):
    if not isinstance(characters, dict) or not 1 <= len(characters) <= 512:
        raise ValueError('角色默认套装目录为空或超限')
    result = {}
    for ident, row in characters.items():
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or not isinstance(row, dict):
            raise ValueError('角色默认套装ID格式错误')
        sets = row.get('sets')
        if (
            not isinstance(sets, (list, tuple))
            or not 1 <= len(sets) <= 5
            or any(
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not _NAME.fullmatch(pair[0])
                or pair[0].strip() != pair[0]
                or type(pair[1]) is not int
                or not 1 <= pair[1] <= 5
                for pair in sets
            )
            or len({name for name, _ in sets}) != len(sets)
            or sum(count for _, count in sets) != 5
        ):
            raise ValueError('角色默认套装件数格式错误')
        # Source order is presentation data too; alphabetical sorting loses the upstream
        # arrangement and makes builtin, live and persisted defaults disagree.
        result[ident] = tuple((name, count) for name, count in sets)
    return MappingProxyType(result)


def parse_upstream(body):
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError('角色默认套装资源超限')
    data = json.loads(body)
    if not isinstance(data, list) or not 1 <= len(data) <= 512:
        raise ValueError('角色默认套装来源格式错误')
    result = {}
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get('role'), dict):
            raise ValueError('角色默认套装条目格式错误')
        ident = row['role'].get('roleId')
        if type(ident) not in (int, str) or not _ID.fullmatch(str(ident)):
            raise ValueError('角色默认套装角色ID错误')
        ident = str(ident)
        if ident in result:
            raise ValueError('角色默认套装角色ID重复')
        phantom = row.get('phantomData')
        equipped = phantom.get('equipPhantomList') if isinstance(phantom, dict) else None
        if not isinstance(equipped, list) or len(equipped) != 5:
            raise ValueError('角色默认套装必须包含五件声骸')
        names = []
        for item in equipped:
            detail = item.get('fetterDetail') if isinstance(item, dict) else None
            name = detail.get('name') if isinstance(detail, dict) else None
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError('角色默认套装名称错误')
            names.append(name)
        result[ident] = {'sets': list(Counter(names).items())}
    return checked_entries(result)


def serialized(entries):
    return {ident: {'sets': list(sets)} for ident, sets in entries.items()}


@dataclass(frozen=True)
class RoleDefaultsSnapshot:
    entries: object
    version: str
    source: str
    raw_sha256: str


def snapshot(entries, source, raw_sha256):
    if source not in SOURCES or not isinstance(raw_sha256, str) or not _HASH.fullmatch(raw_sha256):
        raise ValueError('角色默认套装来源或原始校验和错误')
    entries = checked_entries(serialized(entries))
    canonical = json.dumps(
        serialized(entries), ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return RoleDefaultsSnapshot(entries, digest(canonical), source, raw_sha256)


@lru_cache(maxsize=1)
def builtin_snapshot():
    data = json.loads(builtin_path('role_sonatas.json').read_text(encoding='utf-8'))
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise ValueError('角色默认套装资料格式错误')
    return snapshot(checked_entries(data['characters']), data['source'], data['sha256'])


def default_sets(character_id, active=None):
    return (active or builtin_snapshot()).entries.get(str(character_id), ())


def default_rule(character_id, active=None):
    return CompatibilityRule(sonatas=tuple(name for name, _ in default_sets(character_id, active)))


def checked_validators(value):
    if (
        not isinstance(value, dict)
        or len(value) > 2
        or any(
            key not in ('If-None-Match', 'If-Modified-Since')
            or not isinstance(val, str)
            or not 1 <= len(val) <= 1024
            or '\r' in val
            or '\n' in val
            for key, val in value.items()
        )
    ):
        raise ValueError('角色默认套装条件缓存格式错误')
    return dict(value)


class RoleDefaultsProvider:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve() / 'role-defaults'
        self.path = self.root / 'current.json'
        self._lock = threading.RLock()
        self._active = builtin_snapshot()
        self.validators, self.last_check, self.warning = {}, 0, ''
        try:
            if (
                self.root.is_symlink()
                or self.path.is_symlink()
                or self.path.stat().st_size > MAX_BYTES
            ):
                raise ValueError('角色默认套装缓存路径或大小错误')
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('角色默认套装缓存超限')
            data = json.loads(raw)
            if (
                type(data.get('schema_version')) is not int
                or data['schema_version'] != CACHE_SCHEMA_VERSION
            ):
                raise ValueError('角色默认套装缓存版本错误')
            value = snapshot(
                checked_entries(data['characters']), data['source'], data['raw_sha256']
            )
            if value.version != data['version']:
                raise ValueError('角色默认套装缓存校验和不符')
            validators = checked_validators(data['validators'])
            checked = self._time(data['last_check'])
            self._active, self.validators, self.last_check = value, validators, checked
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.warning = '默认套装缓存未通过校验，使用内置资料：' + str(exc)[:160]

    @staticmethod
    def _time(value):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= time.time() + 300
        ):
            raise ValueError('角色默认套装检查时间错误')
        return float(value)

    @property
    def active(self):
        with self._lock:
            return self._active

    def activate(self, value, validators, *, checked_at, cancel_event=None):
        value = snapshot(value.entries, value.source, value.raw_sha256)
        validators = checked_validators(validators)
        checked_at = self._time(checked_at)
        document = dict(
            schema_version=CACHE_SCHEMA_VERSION,
            characters=serialized(value.entries),
            version=value.version,
            source=value.source,
            raw_sha256=value.raw_sha256,
            validators=validators,
            last_check=checked_at,
        )
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ValueError('角色默认套装缓存超限')
        with self._lock:
            checkpoint(cancel_event)
            if set(self._active.entries) - set(value.entries):
                raise ValueError('来源缺少已有角色，保留上次完整默认套装')
            if self.root.is_symlink() or self.path.is_symlink():
                raise ValueError('角色默认套装缓存路径错误')
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.defaults-', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                checkpoint(cancel_event)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            changed = value.version != self._active.version
            self._active, self.validators, self.last_check = value, validators, checked_at
            self.warning = ''
            return changed
