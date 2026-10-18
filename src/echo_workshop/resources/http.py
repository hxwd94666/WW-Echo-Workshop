# 有界下载并校验评分声明式资料。
"""Bounded same-source JSON acquisition. No inventory or account data is sent."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import http.client
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
import threading
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request
from urllib.parse import urlsplit

from echo_workshop.resources.weights import WeightSnapshot
from echo_workshop.resources.download import NoRedirect as _NoRedirect, read_body, remaining_time
from echo_workshop import __version__

PRIMARY_BASE = 'https://ww1.loping151.top/XutheringWavesUID/resource/map/character/'
MIRROR_BASES = (
    PRIMARY_BASE,
    'https://ww2.loping151.top/XutheringWavesUID/resource/map/character/',
    'https://ww3.loping151.cn/XutheringWavesUID/resource/map/character/',
)
DEFAULT_SOURCES = (MIRROR_BASES[2], MIRROR_BASES[0], MIRROR_BASES[1])
MAX_FILE_BYTES = 256 * 1024
MAX_DIRECTORY_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
_CHAR_DIR = re.compile(r'(?:default|[0-9]{1,8})/\Z')
_FILE = re.compile(r'(?:calc(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?|condition)\.json\Z')


def resource_source(url):
    if not isinstance(url, str):
        raise ValueError('Unapproved resource URL')
    for base in MIRROR_BASES:
        if url.startswith(base):
            path = url[len(base) :]
            if not path or _CHAR_DIR.fullmatch(path):
                return base
            parts = path.split('/')
            if (
                len(parts) == 2
                and _CHAR_DIR.fullmatch(parts[0] + '/')
                and _FILE.fullmatch(parts[1])
            ):
                return base
            break
    # Only the compact character-property reader may use these JSON paths.
    for base in MIRROR_BASES:
        metadata_base = base.replace('map/character/', 'map/detail_json/char/')
        if url.startswith(metadata_base) and re.fullmatch(
            r'[0-9]{1,8}\.json', url[len(metadata_base) :]
        ):
            return base
    raise ValueError('Unapproved resource URL or relative path')


class DownloadCancelled(Exception):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    etag: str = ''
    last_modified: str = ''


class HttpTransport:
    def __init__(self, *, use_system_proxy=False):
        # Public weight mirrors are directly reachable. A stale local HTTP proxy
        # must not prevent their download; this affects only this transport.
        self.use_system_proxy = use_system_proxy
        self._local = threading.local()
        self._connections = []
        self._guard = threading.Lock()

    def _direct_open(self, url, headers, timeout, check, *, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + timeout
        parsed = urlsplit(url)
        connections = getattr(self._local, 'connections', None)
        if connections is None:
            connections = self._local.connections = {}
        connection = connections.get(parsed.hostname)
        if connection is None:
            connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
            connections[parsed.hostname] = connection
            with self._guard:
                self._connections.append(connection)
        reused = connection.sock is not None
        for attempt in range(2):
            connection.timeout = remaining_time(deadline, check)
            if connection.sock is not None:
                connection.sock.settimeout(connection.timeout)
            try:
                connection.request('GET', parsed.path, headers=headers)
                remaining = remaining_time(deadline, check)
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                return connection.getresponse()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                connection.close()
                if not reused or attempt:
                    raise
                # A server may expire an idle keep-alive socket. Retry the GET
                # once on a fresh connection, within the existing time budget.

    def close(self):
        with self._guard:
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()
        self._local = threading.local()

    def reconnect(self, url):
        # Discard only this worker's failed socket, never another download's.
        connection = getattr(self._local, 'connections', {}).get(urlsplit(url).hostname)
        if connection is not None:
            connection.close()

    def get(self, url, headers, limit, timeout, check):
        check()
        resource_source(url)  # Exact approved base and relative JSON/directory path.
        deadline = time.monotonic() + timeout
        remaining_time(deadline, check)
        request = Request(url, headers={'User-Agent': 'Echo Workshop/' + __version__, **headers})
        try:
            if self.use_system_proxy:
                response = build_opener(ProxyHandler(), _NoRedirect()).open(
                    request, timeout=timeout
                )
            else:
                response = self._direct_open(
                    url, dict(request.header_items()), timeout, check, deadline=deadline
                )
        except HTTPError as error:
            if error.code != 304:
                error.close()
                raise
            error.close()
            remaining_time(deadline, check)
            return HttpResponse(304, b'')
        with response:
            remaining_time(deadline, check)
            if response.status == 304:
                return HttpResponse(304, b'')
            if response.status != 200:
                raise ValueError(f'Resource HTTP status {response.status}')
            content = read_body(response, limit, check=check, deadline=deadline)
            return HttpResponse(
                response.status,
                content,
                response.headers.get('ETag', ''),
                response.headers.get('Last-Modified', ''),
            )


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.extend(
                value for key, value in attrs if key == 'href' and isinstance(value, str)
            )


def directory_links(body, *, root=False):
    if not isinstance(body, bytes) or len(body) > MAX_DIRECTORY_BYTES:
        raise ValueError('Invalid resource directory')
    parser = _Links()
    parser.feed(body.decode('utf-8'))
    pattern = _CHAR_DIR if root else _FILE
    if not root and any(
        value.startswith('calc') and value.endswith('.json') and not pattern.fullmatch(value)
        for value in parser.links
    ):
        raise ValueError('Unsupported modal resource filename')
    return tuple(sorted({value for value in parser.links if pattern.fullmatch(value)}))


def strict_json(text):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('Non-finite JSON value: ' + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _validator(value):
    if (
        not isinstance(value, str)
        or len(value) > 512
        or any(ord(char) < 32 or ord(char) > 126 for char in value)
    ):
        raise ValueError('Invalid HTTP cache validator')
    return value


@dataclass(frozen=True)
class FetchResult:
    snapshot: WeightSnapshot
    cache: dict
    removed: tuple[str, ...]
    requests: int
    files: int
    attempts: tuple = ()
    metadata_missing: tuple = ()
    cache_hits: int = 0


class WeightHttpClient:
    def __init__(
        self,
        data_dir,
        *,
        transport=None,
        timeout=8,
        deadline=180,
        workers=8,
        base=PRIMARY_BASE,
        catalog=None,
    ):
        if base not in MIRROR_BASES:
            raise ValueError('Unapproved scoring source')
        from echo_workshop.resources.game_catalog import GameCatalog

        self.catalog = catalog or GameCatalog(data_dir)
        self.base = base
        filename = (
            'http-cache.json'
            if base == PRIMARY_BASE
            else 'http-cache-ww' + str(MIRROR_BASES.index(base) + 1) + '.json'
        )
        self.cache_path = Path(data_dir).resolve() / 'weights' / filename
        self.transport = transport or HttpTransport()
        if not 0 < timeout <= 30 or not 0 < deadline <= 600 or not 1 <= workers <= 8:
            raise ValueError('Invalid resource request budget')
        self.timeout, self.deadline, self.workers = timeout, deadline, workers
        self.last_cache_error = ''

    def load_cache(self):
        try:
            if self.cache_path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('HTTP cache exceeds size limit')
            cache = strict_json(self.cache_path.read_text(encoding='utf-8'))
            if (
                not isinstance(cache, dict)
                or cache.get('schema_version') != 1
                or cache.get('source') != self.base
                or not isinstance(cache.get('files'), dict)
            ):
                raise ValueError('HTTP cache source/format mismatch')
            for path, row in cache['files'].items():
                if (
                    not isinstance(path, str)
                    or len(path.split('/')) != 2
                    or not _CHAR_DIR.fullmatch(path.split('/')[0] + '/')
                    or not _FILE.fullmatch(path.split('/')[1])
                ):
                    raise ValueError('Invalid cached relative path')
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get('text'), str)
                    or len(row['text'].encode('utf-8')) > MAX_FILE_BYTES
                    or _hash(row['text']) != row.get('sha256')
                ):
                    raise ValueError('HTTP cache content mismatch')
                _validator(row.get('etag', ''))
                _validator(row.get('last_modified', ''))
                strict_json(row['text'])
            return cache['files']
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, RecursionError) as error:
            self.last_cache_error = str(error)
            return {}

    def persist(self, result):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result.cache, ensure_ascii=False, allow_nan=False)
        if len(payload.encode('utf-8')) > 20 * 1024 * 1024:
            raise ValueError('HTTP cache exceeds size limit')
        fd, name = tempfile.mkstemp(prefix='.http-', dir=self.cache_path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.cache_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def fetch(self, active, *, cancel_event=None, progress=None, _budget=None):
        # Reuse the same bounded workers and their HTTPS sockets across all
        # directory/download/recheck phases, then close every socket on exit.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            try:
                return self._fetch(
                    active, cancel_event=cancel_event, progress=progress, _budget=_budget, pool=pool
                )
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
                if isinstance(self.transport, HttpTransport):
                    self.transport.close()

    def _fetch(self, active, *, cancel_event=None, progress=None, _budget=None, pool):
        import threading

        started = time.monotonic()
        count = 0
        cache_hits = 0
        total_bytes = 0
        count_lock = threading.Lock()
        failed = threading.Event()
        first_error = []
        cached = self.load_cache()

        def check():
            if failed.is_set():
                raise DownloadCancelled('Remaining resource requests cancelled after a failure')
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled('Weight download cancelled')
            if _budget is not None:
                _budget.check()
            if time.monotonic() - started > self.deadline:
                raise TimeoutError('Resource check exceeded overall deadline')

        def parallel(operation, paths):
            def call(path):
                try:
                    return operation(path)
                except Exception as error:
                    with count_lock:
                        if not first_error:
                            first_error.append(error)
                        failed.set()
                    raise

            futures = [pool.submit(call, path) for path in paths]
            try:
                return [future.result() for future in futures]
            except Exception:
                failed.set()
                for future in futures:
                    future.cancel()
                raise first_error[0] if first_error else DownloadCancelled()

        def get(path, limit, old=None):
            nonlocal count, total_bytes, cache_hits
            check()
            with count_lock:
                count += 1
                if count > 2048:
                    raise ValueError('Resource request count limit exceeded')
            if _budget is not None:
                _budget.request()
            headers = {}
            if old:
                if old.get('etag'):
                    headers['If-None-Match'] = old['etag']
                elif old.get('last_modified'):
                    headers['If-Modified-Since'] = old['last_modified']
            for attempt in range(2):
                check()
                remaining = self.deadline - (time.monotonic() - started)
                if _budget is not None:
                    remaining = min(remaining, _budget.end - time.monotonic())
                try:
                    response = self.transport.get(
                        self.base + path,
                        headers,
                        limit,
                        min(self.timeout, max(0.1, remaining)),
                        check,
                    )
                    break
                except (OSError, http.client.HTTPException):
                    if attempt or not isinstance(self.transport, HttpTransport):
                        raise
                    check()
                    self.transport.reconnect(self.base + path)
                    with count_lock:
                        count += 1
                        if count > 2048:
                            raise ValueError('Resource request count limit exceeded')
                    if _budget is not None:
                        _budget.request()
            check()
            if response.status == 304:
                if not old:
                    raise ValueError('304 response has no validated local body')
                if _budget is not None:
                    _budget.account(len(old['text'].encode('utf-8')))
                with count_lock:
                    cache_hits += 1
                    total_bytes += len(old['text'].encode('utf-8'))
                    if total_bytes > MAX_TOTAL_BYTES:
                        raise ValueError('Resource check exceeds aggregate size limit')
                return dict(old)
            if _budget is not None and isinstance(response.body, bytes):
                _budget.account(len(response.body))
            if (
                response.status != 200
                or not isinstance(response.body, bytes)
                or len(response.body) > limit
            ):
                raise ValueError('Incomplete or oversized resource response')
            with count_lock:
                total_bytes += len(response.body)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError('Resource check exceeds aggregate size limit')
            text = response.body.decode('utf-8')
            return dict(
                text=text,
                sha256=_hash(text),
                etag=_validator(response.etag),
                last_modified=_validator(response.last_modified),
            )

        def enumerate_tree():
            dirs = directory_links(get('', MAX_DIRECTORY_BYTES)['text'].encode('utf-8'), root=True)
            if not dirs or 'default/' not in dirs or len(dirs) > 256:
                raise ValueError('Incomplete character root listing')
            lists = parallel(
                lambda path: directory_links(
                    get(path, MAX_DIRECTORY_BYTES)['text'].encode('utf-8')
                ),
                dirs,
            )
            tree = dict(zip(dirs, lists))
            paths = tuple(path + name for path in dirs for name in tree[path])
            if not paths or len(paths) > 1024 or 'default/calc.json' not in paths:
                raise ValueError('Incomplete scoring resource listing')
            return tree, paths

        if progress:
            progress('正在读取角色目录')
        tree, paths = enumerate_tree()

        def get_files(old_files, label):
            completed = 0
            last_report = 0
            report_lock = threading.Lock()
            if progress:
                progress(f'{label}：0 / {len(paths)}')

            def download(path):
                nonlocal completed, last_report
                row = get(path, MAX_FILE_BYTES, old_files.get(path))
                with report_lock:
                    completed += 1
                    now = time.monotonic()
                    if progress and (now - last_report >= 1 or completed == len(paths)):
                        progress(f'{label}：{completed} / {len(paths)}')
                        last_report = now
                return row

            return parallel(download, paths)

        rows = get_files(cached, '正在读取评分文件')
        files = dict(zip(paths, rows))
        if sum(len(row['text'].encode('utf-8')) for row in rows) > MAX_TOTAL_BYTES:
            raise ValueError('Resource bundle exceeds total size limit')
        templates, conditions = {}, {}
        for path, row in files.items():
            character, name = path.split('/')
            document = strict_json(row['text'])
            if name == 'condition.json':
                conditions[character] = document
            else:
                modal = 'default' if name == 'calc.json' else name[5:-5]
                templates.setdefault(character, {})[modal] = document
        # Metadata is fetched after the weight files have passed validation.
        # Do not save bulky character details in this scoring HTTP cache.
        snapshot = WeightSnapshot(
            templates, conditions, self.base, fetched_at=datetime.now(timezone.utc).isoformat()
        )
        if progress:
            progress(f'已校验 {len(snapshot.options)} 个模版；正在复查目录和文件验证器')
        again, again_paths = enumerate_tree()
        if again != tree or again_paths != paths:
            raise ValueError('Resource listing changed during download')
        checked = get_files(files, '正在复查评分文件')
        if any(row['sha256'] != files[path]['sha256'] for path, row in zip(paths, checked)):
            raise ValueError('Resource content changed during download')
        check()
        from echo_workshop.resources.characters import project_elements

        elements, missing = project_elements(snapshot.templates, self.catalog.active)
        element_cache, metadata_requests = {}, 0
        count += metadata_requests
        snapshot = replace(snapshot, character_elements=elements)
        old_ids = {
            (character, modal)
            for character, variants in active.templates.items()
            for modal in variants
        }
        new_ids = {
            (character, modal)
            for character, variants in snapshot.templates.items()
            for modal in variants
        }
        removed = tuple(sorted('/'.join(key) for key in old_ids - new_ids)) + tuple(
            'condition:' + character
            for character in sorted(set(active.conditions) - set(snapshot.conditions))
        )
        return FetchResult(
            snapshot,
            {'schema_version': 1, 'source': self.base, 'files': files, 'elements': element_cache},
            removed,
            count,
            len(files),
            metadata_missing=missing,
            cache_hits=cache_hits,
        )


@dataclass(frozen=True)
class MirrorAttempt:
    source: str
    state: str
    error: str
    requests: int
    elapsed_seconds: float


class MirrorFetchError(OSError):
    def __init__(self, attempts):
        self.attempts = tuple(attempts)
        super().__init__(
            '完整权重来源均未通过；旧权重保留：'
            + '; '.join(row.source + ' ' + row.error[:160] for row in self.attempts)
        )


class _MirrorBudget:
    def __init__(self, deadline):
        import threading

        self.end = time.monotonic() + deadline
        self.requests = 0
        self.bytes = 0
        self.lock = threading.Lock()

    def check(self):
        if time.monotonic() >= self.end:
            raise TimeoutError('All-source resource deadline exceeded')

    def request(self):
        self.check()
        with self.lock:
            if self.requests >= 2048:
                raise ValueError('All-source resource request limit exceeded')
            self.requests += 1

    def account(self, size):
        with self.lock:
            self.bytes += size
            if self.bytes > MAX_TOTAL_BYTES:
                raise ValueError('All-source aggregate resource size limit exceeded')


class MirrorWeightHttpClient:
    """Sequential whole-source checks. Never splice files or validators across mirrors.

    No service revision/atomic manifest exists: successful fallback need not be
    newer than the active snapshot. Removals still require the service's consent.
    """

    def __init__(
        self,
        data_dir,
        *,
        transport=None,
        timeout=8,
        deadline=300,
        attempt_deadline=120,
        workers=8,
        sources=DEFAULT_SOURCES,
        catalog=None,
    ):
        sources = tuple(sources)
        if (
            not sources
            or len(sources) > 3
            or len(set(sources)) != len(sources)
            or any(base not in MIRROR_BASES for base in sources)
            or not 0 < deadline <= 600
            or not 0 < attempt_deadline <= deadline
        ):
            raise ValueError('Invalid whole-source mirror budget or allowlist')
        self.deadline = deadline
        self.clients = tuple(
            WeightHttpClient(
                data_dir,
                transport=transport,
                timeout=timeout,
                deadline=attempt_deadline,
                workers=workers,
                base=base,
                catalog=catalog,
            )
            for base in sources
        )
        self.attempts = ()

    def fetch(self, active, *, cancel_event=None, progress=None):
        budget = _MirrorBudget(self.deadline)
        attempts = []
        self.attempts = ()
        for index, client in enumerate(self.clients, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled('Weight download cancelled')
            started = time.monotonic()
            before = budget.requests
            try:
                budget.check()
                if budget.requests >= 2048 or budget.bytes > MAX_TOTAL_BYTES:
                    break

                def report(message):
                    if progress:
                        progress(f'通道 {index} / {len(self.clients)} · {message}')

                report('正在连接')
                result = client.fetch(
                    active, cancel_event=cancel_event, progress=report, _budget=budget
                )
            except DownloadCancelled as error:
                attempts.append(
                    MirrorAttempt(
                        client.base,
                        'cancelled',
                        str(error)[:256],
                        budget.requests - before,
                        time.monotonic() - started,
                    )
                )
                self.attempts = tuple(attempts)
                error.attempts = self.attempts
                raise
            except (OSError, ValueError, TypeError, http.client.HTTPException) as error:
                attempts.append(
                    MirrorAttempt(
                        client.base,
                        'failed',
                        (type(error).__name__ + ': ' + str(error))[:256],
                        budget.requests - before,
                        time.monotonic() - started,
                    )
                )
                self.attempts = tuple(attempts)
            else:
                attempts.append(
                    MirrorAttempt(
                        client.base,
                        'validated',
                        '',
                        budget.requests - before,
                        time.monotonic() - started,
                    )
                )
                self.attempts = tuple(attempts)
                return replace(result, requests=budget.requests, attempts=self.attempts)
        raise MirrorFetchError(attempts)

    def persist(self, result):
        if not isinstance(result, FetchResult) or result.snapshot.source != result.cache.get(
            'source'
        ):
            raise ValueError('Mirror cache/snapshot provenance mismatch')
        client = next(
            (client for client in self.clients if client.base == result.snapshot.source), None
        )
        if client is None:
            raise ValueError('Result source is not an approved configured mirror')
        client.persist(result)
