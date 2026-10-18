# 管理套装名称图标目录与更新。
"""Small offline-first set catalogue and bounded same-frame icon recognition.

No import-time IO, role/skill data, remote code, or change to weight URL rules.
"""

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
from types import MappingProxyType
import zlib


from echo_workshop.resources.game_sources import SOURCES, approve as approved_url

CATALOG_SOURCES = tuple(
    'https://' + host + '/XutheringWavesUID/resource/map/sonata_id.json'
    for host in ('ww3.loping151.cn', 'ww1.loping151.top', 'ww2.loping151.top')
)
CATALOG_SOURCES += SOURCES
MAX_PNG = 128 * 1024
MAX_JSON = 64 * 1024


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def _names(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 256:
        raise ValueError('Invalid sonata catalogue')
    result = {}
    for ident, name in value.items():
        if (
            not isinstance(ident, str)
            or not re.fullmatch(r'[1-9][0-9]{0,5}', ident)
            or not isinstance(name, str)
            or not re.fullmatch(r'[\u4e00-\u9fff·・A-Za-z0-9 _-]{2,64}', name)
        ):
            raise ValueError('Invalid sonata ID/name')
        result[ident] = name
    if len(set(result.values())) != len(result):
        raise ValueError('Duplicate sonata names')
    return result


def checked_png(body, *, limit=MAX_PNG):
    """Check size, dimensions and chunk integrity before caching/decoding."""
    if (
        not isinstance(body, bytes)
        or not 45 <= len(body) <= limit
        or body[:8] != b'\x89PNG\r\n\x1a\n'
    ):
        raise ValueError('Invalid sonata PNG')
    pos, first, ended, compressed = 8, True, False, bytearray()
    while pos + 12 <= len(body):
        length = struct.unpack('>I', body[pos : pos + 4])[0]
        kind = body[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(body):
            raise ValueError('Truncated sonata PNG')
        chunk = body[pos + 8 : pos + 8 + length]
        crc = struct.unpack('>I', body[end - 4 : end])[0]
        if zlib.crc32(kind + chunk) & 0xFFFFFFFF != crc:
            raise ValueError('Invalid sonata PNG checksum')
        if first:
            if kind != b'IHDR' or length != 13:
                raise ValueError('Sonata PNG needs IHDR')
            w, h, depth, color, comp, filt, interlace = struct.unpack('>IIBBBBB', chunk)
            if not (
                8 <= w <= 512
                and 8 <= h <= 512
                and depth == 8
                and color in (2, 6)
                and comp == filt == interlace == 0
            ):
                raise ValueError('Unsupported sonata PNG dimensions/format')
            first = False
        if kind == b'IDAT':
            compressed.extend(chunk)
        if kind == b'IEND':
            if length or end != len(body):
                raise ValueError('Invalid sonata PNG ending')
            ended = True
            break
        pos = end
    if not ended:
        raise ValueError('Incomplete sonata PNG')
    expected = h * (1 + w * (4 if color == 6 else 3))
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
        if (
            len(pixels) != expected
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise ValueError('Invalid sonata PNG pixels')
    except zlib.error as error:
        raise ValueError('Invalid sonata PNG compression') from error
    return body


def normalize_icon(body, *, limit=MAX_PNG):
    """Upstream .png URLs also serve WebP. Normalize pixels, not filenames."""
    if body[:8] == b'\x89PNG\r\n\x1a\n':
        return checked_png(body, limit=limit)
    if (
        not 30 <= len(body) <= limit
        or body[:4] != b'RIFF'
        or body[8:12] != b'WEBP'
        or struct.unpack('<I', body[4:8])[0] + 8 != len(body)
    ):
        raise ValueError('Invalid sonata image format')
    dimensions, pos = None, 12
    while pos + 8 <= len(body):
        kind, size = body[pos : pos + 4], struct.unpack('<I', body[pos + 4 : pos + 8])[0]
        chunk = body[pos + 8 : pos + 8 + size]
        if len(chunk) != size:
            raise ValueError('Truncated sonata WebP')
        if kind == b'VP8X' and size == 10:
            if chunk[0] & 2:
                raise ValueError('Animated sonata icons are unsupported')
            dimensions = (
                1 + int.from_bytes(chunk[4:7], 'little'),
                1 + int.from_bytes(chunk[7:10], 'little'),
            )
        elif dimensions is None and kind == b'VP8L' and size >= 5 and chunk[0] == 0x2F:
            bits = int.from_bytes(chunk[1:5], 'little')
            dimensions = (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
        elif (
            dimensions is None and kind == b'VP8 ' and size >= 10 and chunk[3:6] == b'\x9d\x01\x2a'
        ):
            dimensions = (
                int.from_bytes(chunk[6:8], 'little') & 0x3FFF,
                int.from_bytes(chunk[8:10], 'little') & 0x3FFF,
            )
        pos += 8 + size + (size % 2)
    if dimensions is None or pos != len(body) or not all(8 <= n <= 512 for n in dimensions):
        raise ValueError('Invalid sonata WebP dimensions')
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_UNCHANGED)
    if (
        image is None
        or image.ndim != 3
        or image.shape[2] not in (3, 4)
        or image.shape[1::-1] != dimensions
    ):
        raise ValueError('Sonata WebP decode failed')
    ok, encoded = cv2.imencode('.png', image, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise ValueError('Sonata PNG normalization failed')
    return checked_png(encoded.tobytes(), limit=limit)


@dataclass(frozen=True)
class SonataIcon:
    ident: str
    name: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class SonataSnapshot:
    icons: object
    version: str
    source: str

    @classmethod
    def load(cls, folder, filename='sonata_catalog.json'):
        folder = Path(folder).resolve()
        path = folder / filename
        if path.is_symlink() or path.stat().st_size > MAX_JSON:
            raise ValueError('Invalid sonata manifest file')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema_version') != 1 or type(data.get('schema_version')) is not int:
            raise ValueError('Unsupported sonata schema')
        entries = data.get('items')
        if not isinstance(entries, dict) or any(
            not isinstance(item, dict) for item in entries.values()
        ):
            raise ValueError('Invalid sonata entries')
        names = _names({ident: item['name'] for ident, item in entries.items()})
        icons = {}
        for ident, name in names.items():
            entry = entries[ident]
            if entry.get('file') != f'sonata_{ident}.png':
                raise ValueError('Sonata icon path must be a fixed ID filename')
            icon_path = folder / entry['file']
            if icon_path.is_symlink() or icon_path.stat().st_size > MAX_PNG:
                raise ValueError('Invalid sonata icon file')
            body = checked_png(icon_path.read_bytes())
            if _digest(body) != entry.get('sha256'):
                raise ValueError('Sonata icon hash mismatch')
            icons[ident] = SonataIcon(ident, name, icon_path, entry['sha256'])
        source = data.get('source', '')
        if source not in CATALOG_SOURCES:
            raise ValueError('Unapproved sonata catalogue source')
        canonical = json.dumps(
            {k: (v.name, v.sha256) for k, v in icons.items()}, sort_keys=True, ensure_ascii=False
        )
        return cls(MappingProxyType(icons), _digest(canonical.encode('utf-8')), source)


class SonataProvider:
    def __init__(self, data_dir, *, builtin_dir=None):
        self.root = Path(data_dir).resolve() / 'sonata'
        self._lock = threading.RLock()
        from echo_workshop.resources.paths import builtin_path

        self.active = SonataSnapshot.load(builtin_dir or builtin_path())
        self.warning = ''
        self.validators = {}
        self.last_check = 0
        try:
            pointer = self.root / 'current.json'
            if pointer.stat().st_size > MAX_JSON or pointer.is_symlink():
                raise ValueError('Invalid sonata pointer')
            data = json.loads(pointer.read_text(encoding='utf-8'))
            version = data['version']
            if not isinstance(version, str) or not re.fullmatch(r'[a-f0-9]{64}', version):
                raise ValueError('Invalid sonata cache version')
            folder = self.root / version
            if folder.is_symlink():
                raise ValueError('Invalid sonata cache folder')
            snapshot = SonataSnapshot.load(folder)
            if snapshot.version != version:
                raise ValueError('Sonata cache version mismatch')
            validators = data.get('validators', {})
            if not isinstance(validators, dict) or len(validators) > 257:
                raise ValueError('Invalid sonata validators')
            for url, headers in validators.items():
                approved_url(url)
                if (
                    not isinstance(headers, dict)
                    or set(headers) - {'If-None-Match', 'If-Modified-Since'}
                    or any(
                        not isinstance(v, str) or len(v) > 512 or '\r' in v or '\n' in v
                        for v in headers.values()
                    )
                ):
                    raise ValueError('Invalid sonata conditional headers')
            last_check = data.get('last_check', 0)
            if type(last_check) not in (int, float) or not 0 <= last_check <= 4102444800:
                raise ValueError('Invalid sonata check time')
            self.active, self.validators, self.last_check = snapshot, validators, last_check
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            self.warning = '套装缓存未通过校验，使用内置资料：' + str(error)

    def activate(
        self, names, icons, source, validators=None, *, checked_at=None, cancel_event=None
    ):
        names = _names(names)
        if set(names) != set(icons) or source not in CATALOG_SOURCES:
            raise ValueError('Incomplete sonata update')
        items = {}
        for ident, name in names.items():
            checked_png(icons[ident])
            items[ident] = dict(name=name, file=f'sonata_{ident}.png', sha256=_digest(icons[ident]))
        data = dict(schema_version=1, source=source, icon_source=source, items=items)
        from echo_workshop.application.operations import checkpoint

        checkpoint(cancel_event)
        self.root.mkdir(parents=True, exist_ok=True)
        # A full validated snapshot precedes the small atomic active pointer.
        # No deletion of old snapshots, user inventory, or installation files.
        with self._lock, tempfile.TemporaryDirectory(prefix='.stage-', dir=self.root) as stage:
            staging = Path(stage)
            for ident, body in icons.items():
                checkpoint(cancel_event)
                (staging / f'sonata_{ident}.png').write_bytes(body)
            (staging / 'sonata_catalog.json').write_text(
                json.dumps(data, ensure_ascii=False), encoding='utf-8'
            )
            snapshot = SonataSnapshot.load(staging)
            destination = self.root / snapshot.version
            if destination.is_symlink():
                raise ValueError('Invalid sonata destination')
            if destination.exists():
                existing = SonataSnapshot.load(destination)
                if existing.version != snapshot.version:
                    raise ValueError('Existing sonata snapshot differs')
            else:
                # TemporaryDirectory's container stays in place for its own
                # cleanup; the completed child is moved into the cache.
                child = staging / 'ready'
                child.mkdir()
                for path in tuple(staging.iterdir()):
                    if path != child:
                        os.replace(path, child / path.name)
                checkpoint(cancel_event)
                os.replace(child, destination)
            checkpoint(cancel_event)
            result = SonataSnapshot.load(destination)
            checked = time.time() if checked_at is None else checked_at
            pointer = dict(version=result.version, validators=validators or {}, last_check=checked)
            fd, temporary = tempfile.mkstemp(prefix='.current-', dir=self.root)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(pointer, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                checkpoint(cancel_event)
                os.replace(temporary, self.root / 'current.json')
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            changed = result.version != self.active.version
            self.active, self.validators, self.last_check = result, validators or {}, checked
            self.warning = ''
            return changed


@dataclass(frozen=True)
class SonataUpdateStatus:
    state: str = 'idle'
    message: str = '套装资料离线可用'
    last_checked: float = 0
    next_check: float = 0
    requests: int = 0
    cache_hits: int = 0


class SonataUpdateService:
    """Independent bounded auxiliary worker; never holds the weight lease."""

    def __init__(self, provider, *, client=None, stop_event=None, clock=time.time):
        self.provider, self.client = provider, client
        if client is None:
            from echo_workshop.resources.game_catalog import GameCatalog
            from echo_workshop.resources.game_assets import GameAssets
            from echo_workshop.resources.game_projection import CatalogSonataClient

            self.client = CatalogSonataClient(
                GameCatalog(provider.root.parent), GameAssets(provider.root.parent)
            )
        self.stop_event, self.clock = stop_event or threading.Event(), clock
        self._lock, self._wake = threading.RLock(), threading.Event()
        self._thread, self._pending, self._auto = None, False, False
        self.coordinator = None
        self.last_failure = None
        if hasattr(self.client, 'progress'):
            self.client.progress = self._progress
        self._status = SonataUpdateStatus(
            next_check=max(clock() + 30, provider.last_check + 86400),
            message=provider.warning or '套装资料离线可用',
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
            if self._thread is None and not self.stop_event.is_set():
                self._thread = threading.Thread(
                    target=self._run, name='echo-sonata-updates', daemon=True
                )
                self._thread.start()
        self._wake.set()

    def request(self):
        if self.coordinator is not None:
            return self.coordinator.request()
        with self._lock:
            if (
                self.stop_event.is_set()
                or self._pending
                or self._status.state in ('queued', 'checking')
            ):
                return False
            self._pending = True
            self._status = replace(self._status, state='queued', message='套装资料检查已排队')
            self.start(auto_enabled=self._auto)
        self._wake.set()
        return True

    def _progress(self, text):
        with self._lock:
            self._status = replace(self._status, message=text)

    def check_once(self, *, cancel_event=None):
        from echo_workshop.application.operations import OperationCancelled

        token = cancel_event or self.stop_event
        self.last_failure = None
        with self._lock:
            if self.stop_event.is_set() or self._status.state == 'checking':
                return False
            self._status = replace(self._status, state='checking', message='正在检查套装资料')
        try:
            names, icons, source, validators = self.client.fetch(self.provider, cancel_event=token)
            changed = self.provider.activate(
                names, icons, source, validators, checked_at=self.clock(), cancel_event=token
            )
            state, message, delay = (
                ('updated', '套装资料已更新', 86400)
                if changed
                else ('current', '套装资料已是最新', 86400)
            )
            if getattr(self.client, 'deferred', 0):
                message += '；部分图标暂缓，基础资料已保留'
                delay = 1800
        except OperationCancelled:
            state, message, delay = 'stopped', '套装检查已停止，已有资料保留', 86400
        except Exception as error:
            self.last_failure = error
            state, message, delay = 'failed', '套装检查未完成，已有资料保留：' + str(error), 1800
        now = self.clock()
        with self._lock:
            self._status = SonataUpdateStatus(
                state,
                message,
                now,
                now + delay,
                getattr(self.client, 'requests', 0),
                getattr(self.client, 'cache_hits', 0),
            )
        return state == 'updated'

    def _run(self):
        while not self.stop_event.is_set():
            with self._lock:
                due = self._pending or (self._auto and self.clock() >= self._status.next_check)
                self._pending = False
            if due:
                self.check_once()
            self._wake.wait(30)
            self._wake.clear()

    def stop(self):
        self.stop_event.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.25)
