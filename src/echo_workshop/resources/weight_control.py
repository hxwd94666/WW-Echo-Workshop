# 维护权重快照发布和回退控制记录。
"""Same-protocol weight directory ownership and bounded atomic-head records."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import threading

from echo_workshop.storage.locks import plan_file_lock, PlanBusyError

CONTROL_ID = '0d43cbea-c30b-5044-bc1b-9e9755b3745b'
VERSION = re.compile(r'[0-9a-f]{64}\Z')


class WeightBusyError(OSError):
    pass


class WeightConflictError(ValueError):
    pass


def actual(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError('权重路径须为实际绝对目录')
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('权重路径不接受符号链接或目录联接')
    return path


def pairs(rows):
    data = {}
    for key, value in rows:
        if key in data:
            raise ValueError('权重指针含重复字段')
        data[key] = value
    return data


class WeightControl:
    def __init__(self, folder):
        self.folder = actual(folder)
        self._guard = threading.RLock()
        self._owner = None
        self._depth = 0

    def raw(self, name='current.json'):
        if name != 'current.json':
            raise ValueError('未知权重控制指针')
        path = actual(self.folder / name)
        try:
            if path.stat().st_size > 4096:
                raise ValueError('权重指针超过4KiB')
            with path.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('权重指针超过4KiB')
            return raw
        except FileNotFoundError:
            return None

    def fingerprint(self):
        raw = self.raw()
        return hashlib.sha256(raw).hexdigest() if raw is not None else None

    def record(self, name='current.json'):
        raw = self.raw(name)
        if raw is None:
            raise FileNotFoundError(name)
        data = json.loads(raw, object_pairs_hook=pairs)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get('version'), str)
            or not VERSION.fullmatch(data['version'])
        ):
            raise ValueError('Invalid cache pointer')
        if set(data) == {'version'}:
            return data
        schema = data.get('pointer_schema_version')
        # Read legacy v2 heads to migrate existing installations; never read or
        # publish their historical partner. New writes contain only current.
        fields = {'version', 'pointer_schema_version', 'revision'}
        if type(schema) is not int or schema not in (2, 3):
            raise ValueError('权重指针格式不兼容')
        if schema == 2:
            fields.add('previous_version')
            if not isinstance(data.get('previous_version'), str) or not VERSION.fullmatch(
                data['previous_version']
            ):
                raise ValueError('Invalid legacy cache pointer')
        if (
            set(data) != fields
            or type(data.get('revision')) is not int
            or not 1 <= data['revision'] < 2**63
        ):
            raise ValueError('权重指针格式不兼容')
        return data

    @contextmanager
    def lease(self):
        ident = threading.get_ident()
        with self._guard:
            if self._owner == ident:
                self._depth += 1
                nested = True
            elif self._owner is not None:
                raise WeightBusyError('权重目录正被本实例其他作业使用；保留当前快照')
            else:
                self._owner = ident
                self._depth = 1
                nested = False
        try:
            if nested:
                yield
            else:
                actual(self.folder)
                actual(self.folder / '.locks')
                actual(self.folder / '.locks' / (CONTROL_ID + '.lock'))
                try:
                    with plan_file_lock(self.folder, CONTROL_ID):
                        yield
                except PlanBusyError as error:
                    raise WeightBusyError(
                        '权重目录正被其他实例更新；保留当前快照，稍后后台重新加载'
                    ) from error
        finally:
            with self._guard:
                self._depth -= 1
                if not self._depth:
                    self._owner = None
