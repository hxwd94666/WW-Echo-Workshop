# 协调各资料通道的自动更新作业。
"""One low-frequency resource job, with independent validated channel commits.

Weights retain their existing lease and schedule. Catalogue precedes effects so
new sets become usable in the same job. Avatars are optional, bounded and lazy;
their failure never invalidates a scoring snapshot. Opening a page is not a job.
"""

from dataclasses import dataclass, replace
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import uuid4

from echo_workshop.application.operations import OperationCancelled, checkpoint


@dataclass(frozen=True)
class ResourcePart:
    channel: str
    state: str = 'idle'
    message: str = ''
    version: str = ''
    source: str = ''
    requests: int = 0
    files: int = 0
    cache_hits: int = 0


@dataclass(frozen=True)
class ResourceUpdateStatus:
    state: str = 'idle'
    message: str = '已有资料离线可用'
    operation_id: str = ''
    last_checked: float = 0
    last_success: float = 0
    next_check: float = 0
    started_at: float = 0
    parts: tuple = ()
    channel: str = ''
    completed: int = 0
    total: int = 0

    def part(self, channel):
        return next((part for part in self.parts if part.channel == channel), ResourcePart(channel))


class _CancelToken:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)


class ResourceUpdateService:
    """Single-flight automatic/manual checks; old channel APIs delegate here."""

    def __init__(
        self,
        weights,
        sonata,
        effects,
        avatars,
        data_dir,
        *,
        stop_event=None,
        clock=time.time,
        defaults=None,
        catalog=None,
    ):
        self.weights, self.sonata, self.effects, self.avatars = weights, sonata, effects, avatars
        self.defaults, self.catalog = defaults, catalog
        self.stop_event, self.clock = stop_event or threading.Event(), clock
        self._lock, self._wake, self._cancel = (
            threading.RLock(),
            threading.Event(),
            threading.Event(),
        )
        self._thread, self._pending, self._auto = None, None, False
        self._running = False
        self.path = Path(data_dir).resolve() / 'resources/update-state.json'
        self._avatar_due = clock() + 30
        self._status = ResourceUpdateStatus()
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 4096:
                raise ValueError('Invalid resource check state')
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved.get('schema_version') != 1:
                raise ValueError('Invalid resource check schema')
            values = {
                key: saved[key] for key in ('last_checked', 'last_success', 'avatar_next_check')
            }
            if any(
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 4102444800
                for value in values.values()
            ):
                raise ValueError('Invalid resource check schedule')
            self._avatar_due = values.pop('avatar_next_check')
            self._status = replace(self._status, **values)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self._status = replace(self._status, message='检查时间记录待更新；已有资料保留')
        for _, service in self._channels():
            service.coordinator = self
        self._status = replace(self._status, next_check=self._next_due(), parts=self._parts())

    def _channels(self):
        channels = ((('catalog', self.catalog),) if self.catalog is not None else ()) + (
            ('weights', self.weights),
            ('sonata', self.sonata),
            ('effects', self.effects),
        )
        return channels + ((('role-defaults', self.defaults),) if self.defaults is not None else ())

    def _parts(self):
        parts = []
        for channel, service in self._channels():
            state = service.status
            snapshot = service.provider.active
            parts.append(
                ResourcePart(
                    channel,
                    'partial' if getattr(state, 'missing_properties', 0) else state.state,
                    state.message,
                    snapshot.version,
                    snapshot.source,
                    getattr(state, 'requests', 0),
                    getattr(state, 'files', 0),
                    getattr(state, 'cache_hits', 0),
                )
            )
        state = self.avatars.status
        parts.append(
            ResourcePart(
                'avatars',
                state.state,
                state.message,
                str(self.avatars.revision),
                '',
                state.requests,
                state.checked,
                state.cache_hits,
            )
        )
        return tuple(parts)

    def _next_due(self):
        return min(
            *(service.status.next_check for _, service in self._channels()),
            self._avatar_due,
        )

    @property
    def status(self):
        with self._lock:
            status = self._status
        # Never hold the coordinator lock while acquiring a channel lock: local
        # custom edits start their worker while holding the weight status lock.
        return replace(status, parts=self._parts())

    def start(self, *, auto_enabled):
        with self._lock:
            self._auto = bool(auto_enabled)
            if self._thread is None and not self.stop_event.is_set():
                self._thread = threading.Thread(
                    target=self._run, name='echo-resource-updates', daemon=True
                )
                self._thread.start()
        self._wake.set()

    def set_auto(self, enabled):
        self.start(auto_enabled=enabled)

    def request(self, *, allow_removals=False):
        weight_busy = self.weights.status.state in ('checking', 'queued')
        with self._lock:
            if (
                self.stop_event.is_set()
                or self._pending is not None
                or self._running
                or self._status.state in ('checking', 'queued')
                or weight_busy
            ):
                return False
            self._pending = bool(allow_removals)
            self._cancel = threading.Event()
            self._status = replace(
                self._status,
                state='queued',
                message='资料检查已排队',
                channel='',
                completed=0,
                total=len(self._channels()) + 1,
            )
            self.start(auto_enabled=self._auto)
        self._wake.set()
        return True

    def cancel(self):
        """Cancel this job, not runtime lifetime; later manual checks still work."""
        with self._lock:
            if self._status.state not in ('queued', 'checking'):
                return False
            self._cancel.set()
            self._pending = None
            if self._status.state == 'queued':
                self._status = replace(
                    self._status,
                    state='cancelled',
                    message='资料检查已取消；已有资料保留',
                    next_check=max(self._status.next_check, self.clock() + 300),
                )
        self._wake.set()
        return True

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError('Invalid resource schedule path')
        data = dict(
            schema_version=1,
            last_checked=self._status.last_checked,
            last_success=self._status.last_success,
            avatar_next_check=self._avatar_due,
        )
        fd, temporary = tempfile.mkstemp(prefix='.resource-check-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def check_once(self, *, allow_removals=False, manual=True, _queued=False):
        with self._lock:
            if (
                self.stop_event.is_set()
                or self._running
                or (_queued and self._status.state != 'queued')
            ):
                return False
            if not _queued:
                self._cancel = threading.Event()
            self._running = True
            self._status = replace(
                self._status,
                state='checking',
                message='正在检查资料',
                started_at=self.clock(),
                channel='',
                completed=0,
                total=len(self._channels()) + 1,
            )
        token = _CancelToken(self.stop_event, self._cancel)
        # Diagnostics is a small optional adapter for isolated older fixtures.
        try:
            from echo_workshop.application.diagnostics import begin_job

            job = begin_job('resource-update', metadata={'auto_enabled': self._auto})
        except ImportError:
            job = None
        with self._lock:
            self._status = replace(
                self._status, operation_id=job.operation_id if job else uuid4().hex
            )
        changed, failures, warnings, completed, cancelled = 0, [], [], [], False
        now = self.clock()
        sonata_changed = False
        catalog_changed = False
        try:
            for step, (channel, service) in enumerate(self._channels()):
                checkpoint(token)
                if (
                    not manual
                    and now < service.status.next_check
                    and not (channel == 'effects' and sonata_changed)
                    and not (catalog_changed and channel in ('weights', 'sonata', 'effects'))
                ):
                    continue
                with self._lock:
                    self._status = replace(
                        self._status,
                        channel=channel,
                        completed=step,
                        message={
                            'catalog': '正在检查角色、声骸与套装基础资料',
                            'weights': '正在检查评分模版',
                            'sonata': '正在检查套装名称与图标',
                            'effects': '正在检查套装效果',
                            'role-defaults': '正在检查角色默认套装',
                        }[channel],
                    )
                if job:
                    job.stage(channel)
                before_keys = (
                    {
                        (cid, modal)
                        for cid, variants in self.weights.provider.active.templates.items()
                        for modal in variants
                    }
                    if channel == 'weights'
                    else set()
                )
                result = (
                    service.check_once(allow_removals=allow_removals, cancel_event=token)
                    if channel == 'weights'
                    else service.check_once(cancel_event=token)
                )
                changed += bool(result)
                if channel == 'catalog':
                    catalog_changed = bool(result)
                if channel == 'sonata':
                    sonata_changed = bool(result)
                completed.append(channel)
                with self._lock:
                    self._status = replace(self._status, completed=step + 1)
                if job:
                    snapshot = service.provider.active
                    source = next(
                        (
                            host
                            for host in ('ww3', 'ww1', 'ww2')
                            if '//' + host + '.' in snapshot.source
                        ),
                        'nanoka'
                        if 'nanoka.cc' in snapshot.source
                        else 'encore'
                        if 'encore.moe' in snapshot.source
                        else 'builtin',
                    )
                    job.metadata(
                        {'channel': channel, 'version': snapshot.version[:12], 'source': source}
                    )
                    job.count(channel + '_requests', getattr(service.status, 'requests', 0))
                    job.count(channel + '_cache_hits', getattr(service.status, 'cache_hits', 0))
                    if channel == 'weights':
                        after_keys = {
                            (cid, modal)
                            for cid, variants in snapshot.templates.items()
                            for modal in variants
                        }
                        job.count('new_templates', len(after_keys - before_keys))
                        job.count('removed_templates', len(before_keys - after_keys))
                        job.count(
                            'custom_roles_retained', len(service.provider.customized_characters)
                        )
                        job.count('role_attributes', len(snapshot.character_elements))
                        job.count('missing_properties', service.status.missing_properties)
                    else:
                        job.count(
                            channel + '_entries',
                            len(
                                getattr(snapshot, 'icons' if channel == 'sonata' else 'entries', {})
                            ),
                        )
                    channel_failed = service.status.state in (
                        'failed',
                        'conflict',
                        'busy',
                        'deferred',
                    )
                    job.part(
                        channel,
                        'failed'
                        if channel_failed
                        else 'cached'
                        if service.status.state == 'current'
                        else 'completed',
                        metadata={'source': source, 'version': snapshot.version[:12]},
                        counters={
                            'requests': getattr(service.status, 'requests', 0),
                            'cache_hits': getattr(service.status, 'cache_hits', 0),
                        },
                    )
                    if channel == 'weights':
                        job.part(
                            'role-properties',
                            'failed' if service.status.missing_properties else 'completed',
                            metadata={'source': source, 'version': snapshot.version[:12]},
                            counters={
                                'available': len(snapshot.character_elements),
                                'missing': service.status.missing_properties,
                            },
                        )
                        if service.status.missing_properties:
                            failures.append('role-properties')
                state = service.status.state
                if state in ('failed', 'conflict', 'busy', 'deferred'):
                    failures.append(channel)
                    error = getattr(service, 'last_failure', None)
                    if job and error is not None:
                        job.exception(channel, error, summary_code='resource_failed')
                checkpoint(token)
            if manual or now >= self._avatar_due:
                with self._lock:
                    self._status = replace(
                        self._status,
                        channel='avatars',
                        completed=len(self._channels()),
                        message='正在检查已有角色头像',
                    )
                if job:
                    job.stage('avatars')
                avatar = self.avatars.refresh_cached(cancel_event=token, force=manual)
                completed.append('avatars')
                deferred = bool(
                    avatar.deferred or avatar.skipped_retry or avatar.skipped_busy or avatar.pending
                )
                if avatar.failed:
                    failures.append('avatars')
                    if job and self.avatars.last_failure is not None:
                        job.exception(
                            'avatars', self.avatars.last_failure, summary_code='resource_failed'
                        )
                if deferred:
                    warnings.append('avatars')
                if job:
                    job.count('avatar_requests', avatar.requests)
                    job.count('avatar_cache_hits', avatar.cache_hits)
                    job.count('avatar_failures', avatar.failed)
                    job.count('avatar_deferred', avatar.deferred)
                    job.count('avatar_skipped_retry', avatar.skipped_retry)
                    job.count('avatar_skipped_fresh', avatar.skipped_fresh)
                    job.count('avatar_pending', avatar.pending)
                    job.part(
                        'avatars',
                        'failed'
                        if avatar.failed
                        else 'skipped'
                        if deferred
                        else 'completed'
                        if avatar.updated
                        else 'cached',
                        metadata={'source': 'mirrors', 'version': str(self.avatars.revision)},
                        counters={
                            'checked': avatar.checked,
                            'requests': avatar.requests,
                            'cache_hits': avatar.cache_hits,
                            'fallbacks': avatar.failed + avatar.deferred,
                            'deferred': avatar.deferred,
                            'skipped_retry': avatar.skipped_retry,
                            'skipped_busy': avatar.skipped_busy,
                            'skipped_fresh': avatar.skipped_fresh,
                            'pending': avatar.pending,
                        },
                    )
                retry = 60 if avatar.pending else min(3600, max(60, avatar.retry_after or 300))
                self._avatar_due = self.clock() + (
                    1800 if avatar.failed else retry if deferred else 86400
                )
                if deferred:
                    reason = getattr(self.avatars, 'last_deferred', None)
                    logging.getLogger(__name__).warning(
                        'avatar update deferred reason=%s error_types=%s requests=%s deferred=%s retry_skipped=%s pending=%s retry_seconds=%s',
                        getattr(reason, 'reason', 'backoff-or-budget'),
                        ','.join(getattr(reason, 'error_types', ())) or 'none',
                        avatar.requests,
                        avatar.deferred,
                        avatar.skipped_retry,
                        avatar.pending,
                        retry,
                    )
                changed += bool(avatar.updated)
                checkpoint(token)
        except OperationCancelled:
            cancelled = True
        except Exception as error:
            failures.append('job')
            if job:
                job.exception('checking', error, summary_code='resource_failed')
        now = self.clock()
        state = (
            'cancelled'
            if cancelled
            else 'partial'
            if (failures or warnings) and completed
            else 'failed'
            if failures
            else 'updated'
            if changed
            else 'current'
        )
        message = (
            '资料检查已取消；已发布的完整资料和旧缓存保留'
            if cancelled
            else '部分资料未更新；可用资料保留，请展开诊断'
            if failures
            else '头像检查暂缓；评分资料和已有头像可用，将按退避时间重试'
            if warnings
            else '资料已更新；新计算使用最新资料'
            if changed
            else '资料检查完成；已有内容未变'
        )
        next_check = self._next_due()
        with self._lock:
            self._status = replace(
                self._status,
                state=state,
                message=message,
                last_checked=now,
                last_success=now
                if not failures and not warnings and not cancelled
                else self._status.last_success,
                next_check=next_check,
                completed=self._status.completed if cancelled else self._status.total,
            )
            # Avoid tight automatic retry after cancellation; manual ignores it.
            if cancelled:
                self._status = replace(
                    self._status, next_check=max(self._status.next_check, now + 300)
                )
        try:
            if not self.stop_event.is_set():
                self._save()
        except (OSError, ValueError):
            with self._lock:
                self._status = replace(self._status, message=message + '；检查时间保存未完成')
        if job:
            job.finish(
                'cancelled' if cancelled else 'failed' if failures else 'completed',
                summary_code='resource_cancelled'
                if cancelled
                else 'resource_partial'
                if failures or warnings
                else 'resource_updated',
                counters={
                    'published': changed,
                    'channels': len(completed),
                    'fallbacks': len(failures) + len(warnings),
                    'deferred_channels': len(warnings),
                },
            )
        with self._lock:
            self._running = False
        return bool(changed)

    def _run(self):
        while not self.stop_event.is_set():
            weight_busy = self.weights.status.state in ('checking', 'queued')
            with self._lock:
                requested = self._pending
                self._pending = None
                due = self._auto and self.clock() >= self._status.next_check and not weight_busy
            if requested is not None or due:
                self.check_once(
                    allow_removals=bool(requested),
                    manual=requested is not None,
                    _queued=requested is not None,
                )
            self._wake.wait(30)
            self._wake.clear()

    def stop(self):
        self.stop_event.set()
        self._cancel.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            # Network functions have their own budgets, but teardown must not
            # wait for an OS/DNS/socket implementation that ignores cancellation.
            # Every channel checks the same lifetime token before publication.
            self._thread.join(timeout=0.25)
