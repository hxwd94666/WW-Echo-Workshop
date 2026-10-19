# 原子缓存游戏基础资料，并以 Nanoka 主源和 Encore 备用执行低频更新。
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import http.client
from types import MappingProxyType

from echo_workshop.core.cancellation import checkpoint, OperationCancelled
from echo_workshop.resources.game_transport import get as conditional_get
from echo_workshop.resources import game_sources as sources


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(row) for key, row in value.items()})
    if isinstance(value, list):
        return tuple(freeze(row) for row in value)
    return value


@dataclass(frozen=True)
class GameSnapshot:
    version: str
    source: str
    release: str
    characters: object
    echoes: object
    entries: object

    @classmethod
    def from_data(cls, data):
        sources.checked(data)
        return cls(
            digest(data),
            data['source'],
            data['release'],
            freeze(data['characters']),
            freeze(data['echoes']),
            freeze(data['sonatas']),
        )

    @property
    def echo_names(self):
        return tuple(sorted({row['name'] for row in self.echoes.values()}))


class GameCatalog:
    def __init__(self, data_dir, *, builtin=None):
        from echo_workshop.resources.paths import builtin_path

        self.path = Path(data_dir).resolve() / 'game-resources/current.json'
        self._lock = threading.RLock()
        self.active = GameSnapshot.from_data(
            json.loads((builtin or builtin_path('game_catalog.json')).read_bytes())
        )
        self.raw, self.last_check, self.warning = {}, 0, ''
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('游戏资料缓存超限')
            data = json.loads(self.path.read_bytes())
            if data['schema_version'] != 1 or digest(data['data']) != data['version']:
                raise ValueError('游戏资料缓存校验失败')
            active = GameSnapshot.from_data(data['data'])
            raw = self.check_raw(data['raw'])
            if (
                type(data['checked_at']) not in (int, float)
                or not 0 <= data['checked_at'] <= 4102444800
            ):
                raise ValueError('游戏资料缓存时间无效')
            self.active, self.raw, self.last_check = active, raw, data['checked_at']
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
            self.warning = '游戏资料缓存校验失败，使用内置目录：' + str(error)

    @staticmethod
    def check_raw(raw):
        if not isinstance(raw, dict) or len(raw) > 8:
            raise ValueError('游戏资料条件缓存超限')
        for url, row in raw.items():
            sources.approve(url)
            if not isinstance(row['text'], str) or len(row['text'].encode()) > sources.MAX_JSON:
                raise ValueError('游戏资料条件缓存大小无效')
            if hashlib.sha256(row['text'].encode()).hexdigest() != row['sha256']:
                raise ValueError('游戏资料条件缓存内容损坏')
            sources.document(row['text'].encode())
            headers = row['headers']
            if not isinstance(headers, dict) or set(headers) - {
                'If-None-Match',
                'If-Modified-Since',
            }:
                raise ValueError('游戏资料条件头无效')
            if any(
                not isinstance(v, str) or len(v) > 1024 or '\r' in v or '\n' in v
                for v in headers.values()
            ):
                raise ValueError('游戏资料条件头无效')
        return raw

    def activate(self, data, raw, *, checked_at=None, cancel_event=None):
        active = GameSnapshot.from_data(data)
        raw = self.check_raw(raw)
        stamp = time.time() if checked_at is None else checked_at
        body = json.dumps(
            dict(schema_version=1, version=active.version, data=data, raw=raw, checked_at=stamp),
            ensure_ascii=False,
        ).encode()
        if len(body) > 16 * 1024 * 1024:
            raise ValueError('游戏资料缓存超限')
        with self._lock:
            checkpoint(cancel_event)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.parent.is_symlink() or self.path.is_symlink():
                raise ValueError('游戏资料缓存路径无效')
            fd, name = tempfile.mkstemp(prefix='.catalog-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                checkpoint(cancel_event)
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            changed = active.version != self.active.version
            self.active, self.raw, self.last_check, self.warning = active, raw, stamp, ''
            return changed


class GameCatalogClient:
    def __init__(self, *, transport=None, clock=time.monotonic, budget=40):
        self.transport = transport
        self.clock, self.budget = clock, budget
        self.requests = self.cache_hits = 0

    def fetch(self, provider, *, cancel_event=None):
        end = self.clock() + self.budget
        self.requests = self.cache_hits = 0
        errors = []
        for source in sources.SOURCES:
            raw = {}
            # Reserve time for the fallback instead of spending the entire budget on the primary.
            source_end = min(end, self.clock() + self.budget / 2)

            def get(url):
                checkpoint(cancel_event)
                remaining = min(5, source_end - self.clock())
                if remaining <= 0:
                    raise TimeoutError('游戏资料请求超时')
                sources.approve(url)
                old = provider.raw.get(url)
                headers = old['headers'] if old else {}
                self.requests += 1
                args = (url, headers, sources.MAX_JSON, remaining)
                status, body, reply = (
                    self.transport(*args)
                    if self.transport
                    else conditional_get(*args, check=lambda: checkpoint(cancel_event))
                )
                checkpoint(cancel_event)
                if self.clock() > source_end:
                    raise TimeoutError('游戏资料请求超时')
                if status == 304:
                    if not old or not headers:
                        raise ValueError('304 缺少同源有效缓存')
                    self.cache_hits += 1
                    raw[url] = old
                    return sources.document(old['text'].encode())
                if status != 200:
                    raise ValueError('游戏资料响应无效')
                data = sources.document(body)
                raw[url] = dict(
                    text=body.decode('utf-8'),
                    sha256=hashlib.sha256(body).hexdigest(),
                    headers=reply,
                )
                return data

            try:
                if source == sources.NANOKA:
                    version = sources.live_version(get(sources.MANIFEST))
                    base = source + '/ww/' + version + '/'
                    data = sources.nanoka(
                        get(base + 'character.json'),
                        get(base + 'echo.json'),
                        get(base + 'zh/sonata.json'),
                        version,
                    )
                else:
                    data = sources.encore(
                        get(source + '/api/zh-Hans/character'), get(source + '/api/zh-Hans/echo')
                    )
                # Reject partial catalogues and stable-ID reassignment; never trim old users or sets.
                if not set(provider.active.characters) <= set(data['characters']):
                    raise ValueError('角色目录缩减，保留已有资料')
                for ident, old in provider.active.entries.items():
                    if data['sonatas'].get(ident, {}).get('name') != old['name']:
                        raise ValueError('套装目录缺失或 ID 名称错位')
                provider.check_raw(raw)
                return data, raw
            except OperationCancelled:
                raise
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                IndexError,
                http.client.HTTPException,
            ) as error:
                errors.append(source + ': ' + str(error)[:200])
        raise ValueError('主备游戏资料检查未完成；' + '; '.join(errors))


@dataclass(frozen=True)
class CatalogStatus:
    state: str = 'idle'
    message: str = '基础资料离线可用'
    next_check: float = 0
    requests: int = 0
    cache_hits: int = 0


class GameCatalogUpdates:
    def __init__(self, provider, *, stop_event=None, client=None, clock=time.time):
        self.provider, self.clock = provider, clock
        self.client = client or GameCatalogClient()
        self.stop_event = stop_event or threading.Event()
        self.coordinator = None
        self.last_failure = None
        self.status = CatalogStatus(
            next_check=max(clock() + 30, provider.last_check + 86400 if provider.last_check else 0)
        )

    def check_once(self, *, cancel_event=None):
        token = cancel_event or self.stop_event
        self.last_failure = None
        try:
            data, raw = self.client.fetch(self.provider, cancel_event=token)
            changed = self.provider.activate(data, raw, checked_at=self.clock(), cancel_event=token)
            self.status = CatalogStatus(
                'updated' if changed else 'current',
                '基础资料已检查',
                self.clock() + 86400,
                self.client.requests,
                self.client.cache_hits,
            )
            return changed
        except OperationCancelled:
            self.status = CatalogStatus('stopped', '资料检查已取消', self.clock() + 30)
            raise
        except Exception as error:
            self.last_failure = error
            self.status = CatalogStatus(
                'failed',
                '资料检查未完成，已有资料保留',
                self.clock() + 1800,
                self.client.requests,
                self.client.cache_hits,
            )
            return False
