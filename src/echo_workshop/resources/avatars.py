# 按需缓存角色头像资料。
"""Tiny lazy avatar cache; names/IDs stay derived from current score weights."""

from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import os
import re
import tempfile
import threading
import time
from echo_workshop.core.cancellation import checkpoint, OperationCancelled
from echo_workshop.resources.avatar_download import (
    AVATAR_BASES as AVATAR_BASES,
    MAX_AVATAR_BYTES,
    AVATAR_TOTAL_SECONDS,
    AvatarDeferred,
    avatar_validators,
    checked_avatar,
    fetch_avatar,
    fetch_avatar_resource,
)


class _AvatarToken:
    def __init__(self, lifetime, external):
        self.lifetime, self.external = lifetime, external

    def is_set(self):
        return self.lifetime.is_set() or (self.external is not None and self.external.is_set())


@dataclass(frozen=True)
class AvatarStatus:
    state: str = 'idle'
    message: str = '头像按需读取；缺图显示占位'
    checked: int = 0
    requests: int = 0
    updated: int = 0
    failed: int = 0
    cache_hits: int = 0
    deferred: int = 0
    skipped_retry: int = 0
    skipped_busy: int = 0
    skipped_fresh: int = 0
    pending: int = 0
    retry_after: float = 0


class AvatarCache:
    """One lazy worker, 64 decoded-source blobs, no IO/network on the painter."""

    def __init__(
        self, data_dir, characters, *, fetch=fetch_avatar, clock=time.monotonic, transport=None
    ):
        self.folder = Path(data_dir) / 'avatars'
        self.characters, self.fetch = characters, fetch
        self.clock, self.transport = clock, transport
        self._network_gate, self._refresh_gate = threading.Semaphore(4), threading.Lock()
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._pending, self._queued = deque(), set()
        self._values, self._retry = OrderedDict(), {}
        self._retry_counts = {}
        self._fresh_until = {}
        self._thread, self._revision = None, 0
        self._status = AvatarStatus()
        self._validator_failure = None
        try:
            self._validators = self._read_validators()
        except (OSError, ValueError, TypeError) as error:
            self._validators = {}
            self._validator_failure = error
            self._status = AvatarStatus('failed', '头像缓存索引待修复；已有图片保留', failed=1)
        self._refresh_cursor = 0
        self.last_failure = self._validator_failure
        self.last_deferred = None

    def _read_validators(self):
        path = self.folder / 'validators.json'
        try:
            if self.folder.is_symlink() or path.is_symlink() or path.stat().st_size > 128 * 1024:
                raise ValueError('Invalid avatar validator path/size')
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or len(data) > 256:
                raise ValueError('Invalid avatar validator schema')
            for ident, row in data.items():
                if not row:
                    raise ValueError('Invalid empty avatar validator')
                avatar_validators(ident, row)
            return data
        except FileNotFoundError:
            return {}

    @property
    def status(self):
        with self._condition:
            return self._status

    @property
    def revision(self):
        with self._condition:
            return self._revision

    def lookup(self, ident):
        with self._condition:
            return self._values.get(ident)

    def request(self, ident):
        if (
            not isinstance(ident, str)
            or not re.fullmatch(r'[0-9]{1,8}', ident)
            or ident not in self.characters()
        ):
            return False
        with self._condition:
            if (
                self._stop.is_set()
                or ident in self._values
                or ident in self._queued
                or self._retry.get(ident, 0) > self.clock()
                or len(self._queued) >= 64
            ):
                return False
            self._pending.append(ident)
            self._queued.add(ident)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='echo-avatars', daemon=True)
                self._thread.start()
            self._condition.notify()
            return True

    def _cached(self, ident):
        path = self.folder / ('role_head_' + ident + '.png')
        if self.folder.is_symlink() or path.is_symlink():
            raise ValueError('Invalid avatar cache path')
        try:
            if path.stat().st_size > MAX_AVATAR_BYTES or not path.is_file():
                raise ValueError('Invalid avatar cache size/type')
            return checked_avatar(path.read_bytes())
        except FileNotFoundError:
            return None

    def _load(self, ident, *, refresh=False, cancel_event=None, deadline=None):
        token = _AvatarToken(self._stop, cancel_event)
        path = self.folder / ('role_head_' + ident + '.png')
        old = self._cached(ident)
        if old is not None and not refresh:
            return old
        checkpoint(token)
        deadline = (
            min(deadline, self.clock() + AVATAR_TOTAL_SECONDS)
            if deadline is not None
            else self.clock() + AVATAR_TOTAL_SECONDS
        )
        if self.clock() >= deadline:
            raise AvatarDeferred('deadline')
        if not self._network_gate.acquire(blocking=False):
            raise AvatarDeferred('busy')
        try:
            if self.fetch is fetch_avatar:
                body, meta, hit = fetch_avatar_resource(
                    ident,
                    token,
                    previous=old,
                    validators=self._validators.get(ident),
                    deadline=deadline,
                    on_request=self._count_request,
                    clock=self.clock,
                    transport=self.transport,
                )
            else:
                self._count_request()
                try:
                    body = self.fetch(ident, token)
                except AvatarDeferred:
                    raise
                except OSError as error:
                    raise AvatarDeferred('network', (type(error).__name__,)) from error
                meta, hit = {}, False
                if self.clock() >= deadline:
                    raise AvatarDeferred('deadline')
        finally:
            self._network_gate.release()
        if body is None:
            raise AvatarDeferred('not-found')
        checkpoint(token)
        checked_avatar(body)
        with self._condition:
            self._status = replace(self._status, cache_hits=self._status.cache_hits + int(hit))
        if old == body:
            if meta:
                with self._condition:
                    self._validators[ident] = meta
            with self._condition:
                self._fresh_until[ident] = self.clock() + 86400
            return body
        if self.folder.is_symlink() or path.is_symlink():
            raise ValueError('Invalid avatar cache path')
        self.folder.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.avatar-', dir=self.folder)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            checkpoint(token)
            os.replace(temporary, path)
            if meta:
                with self._condition:
                    self._validators[ident] = meta
            with self._condition:
                self._fresh_until[ident] = self.clock() + 86400
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return body

    def _count_request(self):
        with self._condition:
            self._status = replace(self._status, requests=self._status.requests + 1)

    def _publish(self, ident, body):
        with self._condition:
            changed = self._values.get(ident) != body
            self._values[ident] = body
            while len(self._values) > 64:
                self._values.popitem(last=False)
            if changed:
                self._revision += 1
            return changed

    def _save_validators(self, cancel_event=None):
        if self._validator_failure is not None:
            raise self._validator_failure
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / 'validators.json'
        if self.folder.is_symlink() or path.is_symlink():
            raise ValueError('Invalid avatar validator path')
        with self._condition:
            values = {key: self._validators[key] for key in sorted(self._validators)[-256:]}
        fd, temporary = tempfile.mkstemp(prefix='.avatar-meta-', dir=self.folder)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(values, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            checkpoint(cancel_event or self._stop)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _retry_later(self, ident, error):
        with self._condition:
            count = min(5, self._retry_counts.get(ident, 0) + 1)
            self._retry_counts[ident] = count
            delay = min(
                3600, (30 if getattr(error, 'reason', '') == 'busy' else 300) * 2 ** (count - 1)
            )
            self._retry[ident] = self.clock() + delay
        return delay

    def refresh_cached(self, *, cancel_event=None, budget=30, force=False):
        """At most 64 existing images within one budget; manual force bypasses backoff once.

        Lazy cold fetches share the same nonblocking network gate. Busy/timeout
        checks retain bytes and defer, instead of queueing another mirror loop.
        """
        if (
            type(force) is not bool
            or type(budget) not in (int, float)
            or not math.isfinite(budget)
            or budget < 0
        ):
            raise ValueError('Invalid avatar refresh budget/options')
        if not self._refresh_gate.acquire(blocking=False):
            return AvatarStatus(
                'deferred',
                '已有头像检查进行中；稍后重试',
                deferred=1,
                skipped_busy=1,
                retry_after=30,
            )
        try:
            return self._refresh_cached(cancel_event, min(60, budget), force)
        finally:
            self._refresh_gate.release()

    def _refresh_cached(self, cancel_event, budget, force):
        token = _AvatarToken(self._stop, cancel_event)
        self.last_failure = None
        self.last_deferred = None
        with self._condition:
            self._status = AvatarStatus('checking', '正在检查已有头像')
        visited, attempted, ids, offset = 0, 0, (), 0
        deadline = self.clock() + budget
        try:
            checkpoint(token)
            if self._validator_failure is not None:
                self._validators = self._read_validators()
                self._validator_failure = None
            if self.folder.is_symlink():
                raise ValueError('Invalid avatar cache directory')
            known = self.characters()
            # Enumerate known character IDs, not arbitrary directory contents.
            # Cursor covers all cached known IDs; only work, not discovery, caps at64.
            if len(known) > 2048:
                raise ValueError('Too many avatar character identities')
            with self._condition:
                loaded = set(self._values)
            existing = []
            for ident in sorted(known):
                checkpoint(token)
                if not isinstance(ident, str) or not re.fullmatch(r'[0-9]{1,8}', ident):
                    continue
                path = self.folder / ('role_head_' + ident + '.png')
                if ident in loaded or path.exists() or path.is_symlink():
                    existing.append(ident)
            self._prefetch_missing(known, existing, token, deadline)
            ids = tuple(existing)
            if ids:
                offset = self._refresh_cursor % len(ids)
                ids = ids[offset:] + ids[:offset]
            for ident in ids:
                checkpoint(token)
                if self.clock() >= deadline:
                    break
                visited += 1
                with self._condition:
                    if ident in self._queued:
                        self._status = replace(
                            self._status, skipped_busy=self._status.skipped_busy + 1
                        )
                        continue
                    if not force and self._fresh_until.get(ident, 0) > self.clock():
                        self._status = replace(
                            self._status, skipped_fresh=self._status.skipped_fresh + 1
                        )
                        continue
                    if not force and self._retry.get(ident, 0) > self.clock():
                        self._status = replace(
                            self._status, skipped_retry=self._status.skipped_retry + 1
                        )
                        continue
                    if attempted >= 64:
                        visited -= 1
                        break
                    self._queued.add(ident)
                attempted += 1
                old = None
                try:
                    old = self._cached(ident)
                    body = self._load(
                        ident, refresh=bool(force), cancel_event=token, deadline=deadline
                    )
                    if body is not None:
                        checkpoint(token)
                        self._publish(ident, body)
                        with self._condition:
                            self._retry.pop(ident, None)
                            self._retry_counts.pop(ident, None)
                            self._status = replace(
                                self._status,
                                checked=self._status.checked + 1,
                                updated=self._status.updated + int(old != body),
                            )
                except OperationCancelled:
                    raise
                except AvatarDeferred as error:
                    self.last_deferred = error
                    self._retry_later(ident, error)
                    if old is not None:
                        self._publish(ident, old)
                    with self._condition:
                        self._status = replace(self._status, deferred=self._status.deferred + 1)
                except (OSError, ValueError) as error:
                    self.last_failure = error
                    self._retry_later(ident, error)
                    if old is not None:
                        self._publish(ident, old)
                    with self._condition:
                        self._status = replace(self._status, failed=self._status.failed + 1)
                finally:
                    with self._condition:
                        self._queued.discard(ident)
            checkpoint(token)
            self._save_validators(token)
        except OperationCancelled:
            with self._condition:
                self._status = replace(
                    self._status, state='cancelled', message='头像检查已取消；旧图保留'
                )
            raise
        except (OSError, ValueError, TypeError) as error:
            self.last_failure = error
            with self._condition:
                self._status = replace(self._status, failed=self._status.failed + 1)
        finally:
            if ids:
                self._refresh_cursor = (offset + visited) % len(ids)
        with self._condition:
            pending = sum(
                self._fresh_until.get(ident, 0) <= self.clock()
                and self._retry.get(ident, 0) <= self.clock()
                and ident not in self._queued
                for ident in ids[visited:]
            )
            waits = [
                due - self.clock()
                for ident, due in self._retry.items()
                if ident in ids and due > self.clock()
            ]
            delayed = bool(
                self._status.deferred
                or self._status.skipped_retry
                or self._status.skipped_busy
                or pending
            )
            state = (
                'partial'
                if self._status.failed
                else 'deferred'
                if delayed
                else 'updated'
                if self._status.updated
                else 'current'
            )
            self._status = replace(
                self._status,
                state=state,
                pending=pending,
                retry_after=min(waits)
                if waits
                else 30
                if pending or self._status.skipped_busy
                else 0,
                message='头像缓存或资料校验失败；旧图保留，请查看诊断'
                if self._status.failed
                else '部分头像暂缓检查；旧图保留，稍后自动重试'
                if delayed
                else f'已检查已有头像 {self._status.checked} 件；缺图按需获取',
            )
        return self.status

    def _prefetch_missing(self, known, existing, token, deadline):
        with self._condition:
            loaded = set(self._values)
        missing = [
            ident
            for ident in sorted(known)
            if ident not in loaded
            and ident not in existing
            and not (self.folder / ('role_head_' + ident + '.png')).exists()
        ][:32]
        if not missing:
            return

        def fetch(ident):
            try:
                return ident, self._load(ident, cancel_event=token, deadline=deadline), None
            except (OperationCancelled, AvatarDeferred, OSError, ValueError) as error:
                return ident, None, error

        with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
            futures = [pool.submit(fetch, ident) for ident in missing]
            for future in as_completed(futures):
                ident, body, error = future.result()
                checkpoint(token)
                if body is not None:
                    self._publish(ident, body)
                    with self._condition:
                        self._retry.pop(ident, None)
                        self._retry_counts.pop(ident, None)
                elif isinstance(error, AvatarDeferred):
                    self._retry_later(ident, error)
                elif isinstance(error, (OSError, ValueError)):
                    self._retry_later(ident, error)
        try:
            self._save_validators(token)
        except (OperationCancelled, OSError, ValueError):
            raise

    def _run(self):
        while not self._stop.is_set():
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._stop.is_set())
                if self._stop.is_set():
                    break
                ident = self._pending.popleft()
            try:
                body = self._load(ident)
                with self._condition:
                    if body is not None and not self._stop.is_set():
                        self._publish(ident, body)
                        self._retry.pop(ident, None)
                        self._retry_counts.pop(ident, None)
                if body is not None and not self._stop.is_set():
                    self._save_validators()
            except OperationCancelled:
                pass
            except AvatarDeferred as error:
                self.last_deferred = error
                self._retry_later(ident, error)
                with self._condition:
                    self._status = replace(
                        self._status,
                        deferred=self._status.deferred + 1,
                        state='checking' if self._status.state == 'checking' else 'deferred',
                    )
            except (OSError, ValueError) as error:
                self.last_failure = error
                self._retry_later(ident, error)
                with self._condition:
                    self._status = replace(
                        self._status,
                        failed=self._status.failed + 1,
                        state='checking' if self._status.state == 'checking' else 'partial',
                        message='头像缓存或资料校验失败；请查看诊断',
                    )
            finally:
                with self._condition:
                    self._queued.discard(ident)

    def close(self):
        self._stop.set()
        with self._condition:
            self._pending.clear()
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=0.25)
