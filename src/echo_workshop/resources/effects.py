# 管理套装效果及件数门槛资料。
"""Small declarative Sonata-effect catalogue. Never modifies scoring weights.

Actual ``set`` keys determine bonus thresholds (including real one-piece sets).
Descriptions are presentation only, not executable formulas or recommendations.
One bounded auxiliary worker refreshes the complete snapshot, retaining the
previous snapshot on HTTP, schema, name, budget, cancellation or cache failures.
"""

from dataclasses import dataclass, replace
from html import unescape
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

from echo_workshop.application.operations import checkpoint, OperationCancelled
from echo_workshop.resources.game_sources import SOURCES


DETAIL_BASES = tuple(
    'https://' + host + '/XutheringWavesUID/resource/map/detail_json/sonata/'
    for host in ('ww3.loping151.cn', 'ww1.loping151.top', 'ww2.loping151.top')
)

DETAIL_BASES += SOURCES
MAX_FILE_BYTES = 16 * 1024
MAX_ENTRIES = 256
MAX_CACHE_BYTES = 4 * 1024 * 1024
SCHEMA_VERSION = 1
_ID = re.compile(r'[0-9]{1,8}\Z')
_NAME = re.compile(r'[\u4e00-\u9fff·・A-Za-z0-9 _-]{2,64}\Z')


def approved_url(url):
    if not isinstance(url, str):
        raise ValueError('Invalid Sonata-effect URL')
    for base in DETAIL_BASES:
        if url.startswith(base) and re.fullmatch(r'[0-9]{1,8}\.json', url[len(base) :]):
            return base
    raise ValueError('Unapproved Sonata-effect URL')


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid Sonata-effect check time')
    return float(value)


def _plain(text, params=()):
    if not isinstance(text, str) or not text.strip() or len(text) > 8192:
        raise ValueError('Invalid Sonata-effect description')
    if (
        not isinstance(params, (tuple, list))
        or len(params) > 64
        or any(not isinstance(value, str) or len(value) > 128 for value in params)
    ):
        raise ValueError('Invalid Sonata-effect parameters')
    for index, value in enumerate(params):
        text = text.replace('{' + str(index) + '}', value)
    # Leave unknown placeholders visible; never evaluate game scripts/formulas.
    return re.sub(r'<[^>]*>', '', unescape(text)).strip()


def _validators(values):
    if not isinstance(values, dict) or len(values) > MAX_ENTRIES:
        raise ValueError('Invalid Sonata-effect validators')
    result = {}
    for url, headers in values.items():
        approved_url(url)
        if not isinstance(headers, dict) or set(headers) - {'If-None-Match', 'If-Modified-Since'}:
            raise ValueError('Invalid Sonata-effect validators')
        if any(
            not isinstance(value, str) or len(value) > 1024 or '\r' in value or '\n' in value
            for value in headers.values()
        ):
            raise ValueError('Invalid Sonata-effect conditional header')
        result[url] = dict(headers)
    return result


@dataclass(frozen=True)
class SonataEffectEntry:
    ident: str
    name: str
    tiers: object
    data_version: str = ''


def _entries(items):
    if not isinstance(items, dict) or not 1 <= len(items) <= MAX_ENTRIES:
        raise ValueError('Invalid Sonata-effect catalogue')
    result, seen = {}, set()
    for ident, row in items.items():
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or not isinstance(row, dict):
            raise ValueError('Invalid Sonata-effect ID')
        name, tiers, version = row.get('name'), row.get('tiers'), row.get('data_version', '')
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in seen:
            raise ValueError('Invalid or duplicate Sonata-effect name')
        if not isinstance(version, str) or len(version) > 64:
            raise ValueError('Invalid Sonata-effect data version')
        if not isinstance(tiers, dict) or not 1 <= len(tiers) <= 5:
            raise ValueError('Missing Sonata-effect tiers')
        checked = {}
        for count, desc in tiers.items():
            if not isinstance(count, str) or count not in ('1', '2', '3', '4', '5'):
                raise ValueError('Invalid Sonata-effect piece count')
            checked[int(count)] = _plain(desc)
        result[ident] = SonataEffectEntry(ident, name, MappingProxyType(checked), version)
        seen.add(name)
    return MappingProxyType(result)


def _rows(entries):
    return {
        ident: dict(
            name=row.name,
            tiers={str(key): value for key, value in row.tiers.items()},
            data_version=row.data_version,
        )
        for ident, row in entries.items()
    }


@dataclass(frozen=True)
class SonataEffectsSnapshot:
    version: str = ''
    source: str = ''
    entries: object = None
    catalog_version: str = ''

    def _entry(self, name):
        return next((row for row in (self.entries or {}).values() if row.name == name), None)

    def tiers(self, name):
        row = self._entry(name)
        return tuple(sorted(row.tiers)) if row is not None else ()

    def descriptions(self, name):
        row = self._entry(name)
        return dict(row.tiers) if row is not None else {}


def _snapshot(items, source, catalog_version=''):
    if source not in DETAIL_BASES:
        raise ValueError('Invalid Sonata-effect source')
    entries = _entries(items)
    if not isinstance(catalog_version, str) or (
        catalog_version and not re.fullmatch(r'[a-f0-9]{64}', catalog_version)
    ):
        raise ValueError('Invalid Sonata-effect catalogue dependency')
    canonical = json.dumps(
        _rows(entries), ensure_ascii=False, sort_keys=True, separators=(',', ':')
    )
    # Source dependency is provenance, not score/content identity. Historical
    # snapshots keep their original hashes and one-piece/tier semantics.
    return SonataEffectsSnapshot(
        hashlib.sha256(canonical.encode('utf-8')).hexdigest(), source, entries, catalog_version
    )


class SonataEffectsProvider:
    def __init__(self, data_dir, builtin_dir=None):
        self.root = Path(data_dir).resolve() / 'sonata-effects'
        self.path = self.root / 'current.json'
        self._lock = threading.RLock()
        self._active = SonataEffectsSnapshot(entries=MappingProxyType({}))
        self.validators, self.last_check = {}, 0
        self.warning = ''
        from echo_workshop.resources.paths import builtin_path

        builtin = Path(builtin_dir) if builtin_dir is not None else builtin_path()
        try:
            data = self._read(builtin / 'sonata_effects_catalog.json')
            self._active = _snapshot(data['items'], data['source'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.warning = '内置套装效果资料读取未完成：' + str(error)
        if self.path.exists():
            try:
                data = self._read(self.path)
                active = _snapshot(data['items'], data['source'], data.get('catalog_version', ''))
                if data.get('version') != active.version:
                    raise ValueError('Sonata-effect version/hash mismatch')
                self.validators = _validators(data.get('validators', {}))
                self.last_check = _number(data.get('last_check', 0))
                self._active, self.warning = active, ''
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.warning = '套装效果缓存未通过校验，保留内置资料：' + str(error)

    @staticmethod
    def _read(path):
        if path.is_symlink() or path.stat().st_size > MAX_CACHE_BYTES:
            raise ValueError('Invalid Sonata-effect catalogue file')
        with path.open('rb') as stream:
            raw = stream.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError('Sonata-effect catalogue exceeds limit')
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or type(data.get('schema_version')) is not int
            or data.get('schema_version') != SCHEMA_VERSION
        ):
            raise ValueError('Invalid Sonata-effect catalogue schema')
        return data

    @property
    def active(self):
        with self._lock:
            return self._active

    def activate(
        self,
        items,
        source,
        validators=None,
        *,
        checked_at=None,
        cancel_event=None,
        catalog_version='',
    ):
        snapshot = _snapshot(items, source, catalog_version)
        validators = _validators(validators or {})
        checked_at = _number(time.time() if checked_at is None else checked_at)
        document = dict(
            schema_version=SCHEMA_VERSION,
            version=snapshot.version,
            source=source,
            items=_rows(snapshot.entries),
            validators=validators,
            last_check=checked_at,
        )
        if catalog_version:
            document['catalog_version'] = catalog_version
        raw = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError('Sonata-effect cache exceeds limit')
        with self._lock:
            checkpoint(cancel_event)
            if self.root.is_symlink() or self.path.is_symlink():
                raise ValueError('Invalid Sonata-effect cache path')
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.effects-', dir=self.root)
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
            changed = snapshot.version != self._active.version
            self._active, self.validators, self.last_check = snapshot, validators, checked_at
            self.warning = ''
            return changed


@dataclass(frozen=True)
class SonataEffectsStatus:
    state: str = 'idle'
    message: str = '套装效果资料离线可用'
    last_checked: float = 0
    next_check: float = 0
    requests: int = 0
    cache_hits: int = 0


class _StopToken:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)


class SonataEffectsUpdateService:
    def __init__(self, provider, sonata_provider, *, client=None, stop_event=None, clock=time.time):
        self.provider, self.sonata_provider = provider, sonata_provider
        self.client, self.clock = client, clock
        if client is None:
            from echo_workshop.resources.game_catalog import GameCatalog
            from echo_workshop.resources.game_projection import CatalogEffectsClient

            self.client = CatalogEffectsClient(GameCatalog(provider.root.parent))
        self.stop_event = stop_event or threading.Event()
        self._stop = threading.Event()
        self._cancel = _StopToken(self.stop_event, self._stop)
        self._lock, self._wake = threading.RLock(), threading.Event()
        self._thread, self._pending, self._auto = None, False, False
        self.coordinator = None
        self.last_failure = None
        self._status = SonataEffectsStatus(
            message=provider.warning or '套装效果资料离线可用',
            next_check=max(clock() + 30, provider.last_check + 86400),
        )

    @property
    def status(self):
        with self._lock:
            return self._status

    def start(self, *, auto_enabled):
        if self.coordinator is not None:
            self.coordinator.start(auto_enabled=auto_enabled)
            return
        with self._lock:
            self._auto = bool(auto_enabled)
            if self._thread is None and not self._cancel.is_set():
                self._thread = threading.Thread(
                    target=self._run, name='echo-sonata-effects', daemon=True
                )
                self._thread.start()
        self._wake.set()

    def request(self):
        if self.coordinator is not None:
            return self.coordinator.request()
        with self._lock:
            if (
                self._cancel.is_set()
                or self._pending
                or self._status.state in ('queued', 'checking')
            ):
                return False
            self._pending = True
            self._status = replace(self._status, state='queued', message='套装效果资料检查已排队')
            self.start(auto_enabled=self._auto)
        self._wake.set()
        return True

    def check_once(self, *, cancel_event=None):
        token = cancel_event or self._cancel
        self.last_failure = None
        with self._lock:
            if self._cancel.is_set() or self._status.state == 'checking':
                return False
            self._status = replace(self._status, state='checking', message='正在检查套装效果资料')
        try:
            catalog = getattr(self.sonata_provider.active, 'version', '')
            items, source, validators = self.client.fetch(
                self.provider, self.sonata_provider, cancel_event=token
            )
            if getattr(self.sonata_provider.active, 'version', '') != catalog:
                raise ValueError('套装目录检查期间已变化；效果旧资料保留')
            changed = self.provider.activate(
                items,
                source,
                validators,
                checked_at=self.clock(),
                cancel_event=token,
                catalog_version=catalog,
            )
            state, message, delay = (
                ('updated', '套装效果资料已更新', 86400)
                if changed
                else ('current', '套装效果资料已是最新', 86400)
            )
        except OperationCancelled:
            state, message, delay = 'stopped', '套装效果检查已停止，已有资料保留', 86400
        except Exception as error:
            self.last_failure = error
            state, message, delay = (
                'failed',
                '套装效果检查未完成，已有资料保留：' + str(error),
                1800,
            )
        now = self.clock()
        with self._lock:
            self._status = SonataEffectsStatus(
                state,
                message,
                now,
                now + delay,
                getattr(self.client, 'requests', 0),
                getattr(self.client, 'cache_hits', 0),
            )
        return state == 'updated'

    def _run(self):
        while not self._cancel.is_set():
            with self._lock:
                due = self._pending or (self._auto and self.clock() >= self._status.next_check)
                self._pending = False
            if due:
                self.check_once()
            self._wake.wait(30)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.25)
