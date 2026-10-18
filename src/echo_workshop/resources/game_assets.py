# 独立缓存主备图片，图片失效不阻塞名称、属性和套装效果更新。
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import threading
import http.client
import zlib

from echo_workshop.application.operations import checkpoint, OperationCancelled
from echo_workshop.resources.game_transport import get as conditional_get
from echo_workshop.resources.game_sources import approve, asset_urls


def placeholder_png():
    def chunk(kind, value):
        return (
            struct.pack('>I', len(value))
            + kind
            + value
            + struct.pack('>I', zlib.crc32(kind + value) & 0xFFFFFFFF)
        )

    # Missing set icons remain transparent; do not impersonate another set during recognition.
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 6, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(b'\0' * (8 * 33)))
        + chunk(b'IEND', b'')
    )


class GameAssets:
    def __init__(self, data_dir, *, transport=None, clock=time.monotonic):
        self.root = Path(data_dir) / 'game-assets'
        self.transport, self.clock = transport, clock
        self.requests = self.cache_hits = 0
        self._counter_lock = threading.Lock()

    def _cache(self, url):
        from echo_workshop.resources.sonata import checked_png

        path = self.root / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        try:
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('图片缓存大小无效')
            data = json.loads(path.read_bytes())
            body = bytes.fromhex(data['png'])
            checked_png(body, limit=512 * 1024)
            if data['url'] != url or data['sha256'] != hashlib.sha256(body).hexdigest():
                raise ValueError('图片缓存来源或校验和错误')
            headers = data['headers']
            if (
                not isinstance(headers, dict)
                or set(headers) - {'If-None-Match', 'If-Modified-Since'}
                or any(
                    not isinstance(v, str) or len(v) > 1024 or '\r' in v or '\n' in v
                    for v in headers.values()
                )
            ):
                raise ValueError('图片缓存条件头无效')
            return path, body, headers
        except (OSError, ValueError, KeyError, TypeError):
            return path, None, {}

    def load(self, urls, *, cancel_event=None, deadline=None):
        from echo_workshop.resources.sonata import normalize_icon

        if tuple(urls) != asset_urls(urls[0]):
            raise ValueError('图片主备地址不匹配')
        deadline = min(deadline or self.clock() + 8, self.clock() + 8)
        errors = []
        for url in urls:
            approve(url)
            path, old, headers = self._cache(url)
            checkpoint(cancel_event)
            remaining = min(4, deadline - self.clock())
            if remaining <= 0:
                break
            try:
                with self._counter_lock:
                    self.requests += 1
                args = (url, headers, 512 * 1024, remaining)
                status, raw, reply = (
                    self.transport(*args)
                    if self.transport
                    else conditional_get(*args, check=lambda: checkpoint(cancel_event))
                )
                checkpoint(cancel_event)
                if self.clock() > deadline:
                    raise TimeoutError('图片检查超时')
                if status == 304:
                    if old is None or not headers:
                        raise ValueError('图片 304 缺少同源有效缓存')
                    with self._counter_lock:
                        self.cache_hits += 1
                    return old
                if status != 200:
                    raise ValueError('图片 HTTP 响应无效')
                body = normalize_icon(raw, limit=512 * 1024)
                if (
                    not isinstance(reply, dict)
                    or set(reply) - {'If-None-Match', 'If-Modified-Since'}
                    or any(
                        not isinstance(v, str) or len(v) > 1024 or '\r' in v or '\n' in v
                        for v in reply.values()
                    )
                ):
                    raise ValueError('图片条件头无效')
                self.root.mkdir(parents=True, exist_ok=True)
                if self.root.is_symlink() or path.is_symlink():
                    raise ValueError('图片缓存路径无效')
                data = dict(
                    url=url, png=body.hex(), sha256=hashlib.sha256(body).hexdigest(), headers=reply
                )
                fd, temporary = tempfile.mkstemp(prefix='.asset-', dir=self.root)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                        json.dump(data, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    checkpoint(cancel_event)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return body
            except OperationCancelled:
                raise
            except (OSError, ValueError, http.client.HTTPException) as error:
                errors.append(type(error).__name__)
        # Caller already holds the old picture; retain it without marking a failed check fresh.
        from echo_workshop.resources.avatar_download import AvatarDeferred

        raise AvatarDeferred('network', errors)


class CatalogAvatarFetcher:
    def __init__(self, catalog, assets):
        self.catalog, self.assets = catalog, assets

    def __call__(self, ident, token):
        from echo_workshop.resources.avatar_download import AvatarDeferred

        row = self.catalog.active.characters.get(ident)
        if row is None:
            raise AvatarDeferred('not-found')
        return self.assets.load(row['icons'], cancel_event=token, deadline=self.assets.clock() + 6)
