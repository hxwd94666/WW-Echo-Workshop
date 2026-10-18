# 检查更新并原子激活权重资料。
"""Single-flight offline-first checks, persisted daily schedule and backoff."""

from dataclasses import dataclass, replace, asdict
import json
import math
import os
from pathlib import Path
import random
import tempfile
import threading
import time

from echo_workshop.application.operations import OperationCancelled
from echo_workshop.resources.weight_control import WeightBusyError, WeightConflictError

from echo_workshop.resources.http import (
    DownloadCancelled,
    PRIMARY_BASE,
    MIRROR_BASES,
    MirrorWeightHttpClient,
)


@dataclass(frozen=True)
class UpdateStatus:
    state: str = 'idle'
    message: str = '使用本地权重；等待低频检查或手动检查'
    last_checked: float = 0
    last_success: float = 0
    next_check: float = 0
    failures: int = 0
    files: int = 0
    requests: int = 0
    source: str = PRIMARY_BASE
    version: str = ''
    attempts: tuple = ()
    operation: str = ''
    started_at: float = 0
    cache_hits: int = 0
    missing_properties: int = 0


class WeightUpdateService:
    def __init__(
        self, provider, data_dir, *, client=None, stop_event=None, clock=time.time, jitter=None
    ):
        self.provider = provider
        self.client = client or MirrorWeightHttpClient(data_dir)
        self.state_path = Path(data_dir).resolve() / 'weights/update-state.json'
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.jitter = jitter or (lambda: random.uniform(0, 120))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._pending = None
        self._auto = False
        self.coordinator = None
        self.last_failure = None
        self._status = UpdateStatus(
            version=provider.active.version, next_check=clock() + self.jitter()
        )
        try:
            if self.state_path.stat().st_size > 8192:
                raise ValueError('Update state exceeds limit')
            saved = json.loads(self.state_path.read_text(encoding='utf-8'))
            numeric = {name: saved[name] for name in ('last_checked', 'last_success', 'next_check')}
            if any(
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 4102444800
                for value in numeric.values()
            ):
                raise ValueError('Invalid update schedule')
            failures = saved['failures']
            if type(failures) is not int or not 0 <= failures <= 20:
                raise ValueError('Invalid retry counter')
            source = saved.get('source', PRIMARY_BASE)
            if source not in MIRROR_BASES:
                raise ValueError('Invalid saved scoring source')
            self._status = replace(self._status, **numeric, failures=failures, source=source)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._status = replace(
                self._status, message='检查记录损坏，保留可用权重：' + str(error)
            )

    @property
    def status(self):
        with self._lock:
            return self._status

    def _save_state(self, status=None):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.update-', dir=self.state_path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(
                    asdict(self._status if status is None else status),
                    stream,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def start(self, *, auto_enabled):
        if self.coordinator is not None:
            # Only custom edits/local reload use this worker in a runtime.
            # Standalone services retain their historical automatic behavior.
            if not (isinstance(self._pending, tuple) or self._pending == 'reload'):
                self._auto = bool(auto_enabled)
                self.coordinator.start(auto_enabled=auto_enabled)
                return
            # A local edit does not own the resource auto preference. It must
            # not overwrite a newer UI toggle with this legacy worker's flag.
        with self._lock:
            self._auto = bool(auto_enabled)
            if self._thread is None and not self.stop_event.is_set():
                self._thread = threading.Thread(
                    target=self._run, name='echo-weight-updates', daemon=True
                )
                self._thread.start()
        self._wake.set()

    def set_auto(self, enabled):
        if self.coordinator is not None:
            self.coordinator.set_auto(enabled)
            self._auto = bool(enabled)
            return
        self.start(auto_enabled=enabled)

    def request(self, *, allow_removals=False):
        if self.coordinator is not None:
            return self.coordinator.request(allow_removals=allow_removals)
        with self._lock:
            if (
                self.stop_event.is_set()
                or self._status.state in ('checking', 'queued')
                or self._pending is not None
            ):
                return False
            self._pending = bool(allow_removals)
            self._status = replace(
                self._status,
                state='queued',
                operation='check',
                message='权重检查已排队',
                started_at=0,
                files=0,
                requests=0,
                attempts=(),
            )
            self.start(auto_enabled=self._auto)
        self._wake.set()
        return True

    def _progress(self, text):
        with self._lock:
            self._status = replace(self._status, message=text)

    def check_once(self, *, allow_removals=False, cancel_event=None):
        with self._lock:
            if self._status.state == 'checking' or self.stop_event.is_set():
                return False
        try:
            with self.provider.operation():
                self.provider.assert_current()
                return self._check_owned(allow_removals=allow_removals, cancel_event=cancel_event)
        except (WeightBusyError, WeightConflictError) as error:
            self.last_failure = error
            with self._lock:
                if isinstance(error, WeightBusyError) and self._status.state == 'checking':
                    return False
                self._status = replace(
                    self._status,
                    state='busy' if isinstance(error, WeightBusyError) else 'conflict',
                    message=str(error),
                    next_check=max(self._status.next_check, self.clock() + 60),
                    version=self.provider.active.version,
                )
            # No ownership/no matching base: do not overwrite shared schedule,
            # cache or head, and do not count contention as a network failure.
            return False
        except Exception as error:
            self.last_failure = error
            with self._lock:
                self._status = replace(
                    self._status,
                    state='failed',
                    message='本机权重控制记录未通过，当前快照保留：' + str(error),
                    next_check=max(self._status.next_check, self.clock() + 300),
                    version=self.provider.active.version,
                    files=0,
                    requests=0,
                    attempts=(),
                )
            return False

    def _check_owned(self, *, allow_removals=False, cancel_event=None):
        token = cancel_event or self.stop_event
        self.last_failure = None
        with self._lock:
            if self._status.state == 'checking' or self.stop_event.is_set():
                return False
            self._status = replace(
                self._status,
                state='checking',
                operation='check',
                message='正在后台检查完整权重资源',
                attempts=(),
                files=0,
                requests=0,
                started_at=self.clock(),
            )
        try:
            result = self.client.fetch(
                self.provider.upstream, cancel_event=token, progress=self._progress
            )
            with self._lock:
                self._status = replace(
                    self._status,
                    source=result.snapshot.source,
                    attempts=result.attempts,
                    files=result.files,
                    requests=result.requests,
                    cache_hits=getattr(result, 'cache_hits', 0),
                )
            if token.is_set():
                raise DownloadCancelled()
            removed = tuple(
                key
                for key in result.removed
                if key.removeprefix('condition:').split('/')[0]
                not in self.provider.customized_characters
            )
            if removed and not allow_removals:
                state = 'deferred'
                message = (
                    '检测到模版/条件删减，旧快照保留；核对后可勾选允许删减再检查：'
                    + ', '.join(removed[:6])
                )
            else:
                # Disk IO/validation belongs to the worker/OS lease, not the
                # status or active-reference locks used by the GUI heartbeat.
                self.client.persist(result)
                if token.is_set():
                    raise DownloadCancelled()
                changed = self.provider.activate(result.snapshot, cancel_event=token)
                state = 'updated' if changed else 'current'
                message = '新权重已应用' if changed else '完整检查通过，权重内容未变'
                missing = getattr(result, 'metadata_missing', ())
                message += f'；角色属性 {len(result.snapshot.character_elements)} 个'
                if missing:
                    message += (
                        f'；{len(missing)} 个角色属性未更新，已有属性沿用缓存，未知属性不作适配推荐'
                    )
                if self.provider.customized_characters:
                    message += (
                        f'；保留 {len(self.provider.customized_characters)} 个角色的自定义权重'
                    )
            now = self.clock()
            with self._lock:
                finished = replace(
                    self._status,
                    state=state,
                    message=message,
                    last_checked=now,
                    last_success=now,
                    next_check=now
                    + (600 if getattr(result, 'metadata_missing', ()) else 86400)
                    + self.jitter(),
                    failures=0,
                    files=result.files,
                    requests=result.requests,
                    version=self.provider.active.version,
                    missing_properties=len(getattr(result, 'metadata_missing', ())),
                )
            try:
                self._save_state(finished)
            except OSError as error:
                finished = replace(
                    finished, message=finished.message + '；检查时间保存失败：' + str(error)
                )
            with self._lock:
                self._status = finished
            return state == 'updated'
        except (WeightBusyError, WeightConflictError):
            raise
        except (DownloadCancelled, OperationCancelled) as error:
            with self._lock:
                attempts = getattr(error, 'attempts', self._status.attempts)
                self._status = replace(
                    self._status,
                    state='stopped',
                    message='权重检查已取消；可用快照保留',
                    attempts=attempts,
                    requests=sum(row.requests for row in attempts)
                    if attempts
                    else self._status.requests,
                )
        except Exception as error:
            self.last_failure = error
            with self._lock:
                failures = min(20, self._status.failures + 1)
                now = self.clock()
                finished = replace(
                    self._status,
                    state='failed',
                    message='更新失败，继续使用原权重：' + str(error),
                    failures=failures,
                    last_checked=now,
                    next_check=now + min(21600, 300 * 2 ** (failures - 1)) + self.jitter(),
                    version=self.provider.active.version,
                    attempts=getattr(error, 'attempts', self._status.attempts),
                    requests=sum(
                        row.requests for row in getattr(error, 'attempts', self._status.attempts)
                    )
                    if getattr(error, 'attempts', self._status.attempts)
                    else self._status.requests,
                )
            try:
                self._save_state(finished)
            except OSError:
                pass
            with self._lock:
                self._status = finished
        return False

    def request_custom(self, char_id, variant=None, *, main=None, sub=None, expected_version):
        # Edits share the existing worker/lease with updates and local reload.
        from echo_workshop.resources.weights import _thaw

        with self._lock:
            if (
                self.stop_event.is_set()
                or self._status.state in ('checking', 'queued')
                or self._pending is not None
            ):
                return False
            self._pending = ('custom', char_id, variant, _thaw(main), _thaw(sub), expected_version)
            self._status = replace(
                self._status, state='queued', operation='custom', message='自定义权重正在保存'
            )
            self.start(auto_enabled=self._auto)
        self._wake.set()
        return True

    def _custom_once(self, request):
        _, char_id, variant, main, sub, version = request
        with self._lock:
            self._status = replace(
                self._status, state='checking', operation='custom', message='自定义权重正在保存'
            )
        try:
            if variant is None:
                self.provider.reset_custom_character(
                    char_id, expected_version=version, cancel_event=self.stop_event
                )
                message = '此角色已改用最新默认权重，将跟随更新'
            else:
                self.provider.set_custom_values(
                    char_id,
                    variant,
                    main,
                    sub,
                    expected_version=version,
                    cancel_event=self.stop_event,
                )
                message = '自定义权重已保存；更新时保留此角色的所有模版'
            state = 'customized'
        except OperationCancelled:
            state, message = 'stopped', '保存已取消；原权重保留'
        except Exception as error:
            state, message = 'failed', '自定义权重未保存：' + str(error)
        with self._lock:
            self._status = replace(
                self._status, state=state, message=message, version=self.provider.active.version
            )

    def request_reload(self):
        with self._lock:
            if (
                self.stop_event.is_set()
                or self._status.state in ('checking', 'queued')
                or self._pending is not None
            ):
                return False
            self._pending = 'reload'
            self._status = replace(
                self._status,
                state='queued',
                operation='reload',
                message='本机已保存权重的后台加载已排队',
            )
            self.start(auto_enabled=self._auto)
        self._wake.set()
        return True

    def reload_once(self):
        with self._lock:
            if self.stop_event.is_set() or self._status.state == 'checking':
                return False
            self._status = replace(
                self._status,
                state='checking',
                operation='reload',
                message='正在后台校验本机已保存权重',
                files=0,
                requests=0,
                attempts=(),
            )
        try:
            with self.provider.operation():
                changed = self.provider.reload(cancel_event=self.stop_event)
                # Refresh persisted scheduling from the owner, without writing it.
                saved = WeightUpdateService(
                    self.provider,
                    self.state_path.parent.parent,
                    client=self.client,
                    stop_event=self.stop_event,
                    clock=self.clock,
                    jitter=self.jitter,
                ).status
                with self._lock:
                    self._status = replace(
                        self._status,
                        state='reloaded',
                        message='已加载本机已保存权重；未发网络请求'
                        if changed
                        else '本机权重内容相同；已同步提交代次，未发网络请求',
                        version=self.provider.active.version,
                        last_checked=saved.last_checked,
                        last_success=saved.last_success,
                        next_check=saved.next_check,
                        failures=saved.failures,
                        source=saved.source,
                    )
                return changed
        except (DownloadCancelled, OperationCancelled):
            state = 'stopped'
            message = '后台加载已取消；本实例快照保留'
        except (WeightBusyError, WeightConflictError) as error:
            state = 'busy' if isinstance(error, WeightBusyError) else 'conflict'
            message = str(error)
        except Exception as error:
            state = 'failed'
            message = '本机权重加载未完成，当前快照保留：' + str(error)
        with self._lock:
            self._status = replace(
                self._status, state=state, message=message, version=self.provider.active.version
            )
        return False

    def _run(self):
        while not self.stop_event.is_set():
            with self._lock:
                requested = self._pending
                self._pending = None
                due = (
                    self.coordinator is None
                    and self._auto
                    and self.clock() >= self._status.next_check
                    and self._status.state not in ('checking', 'queued')
                )
                if due:
                    self._status = replace(
                        self._status,
                        state='queued',
                        operation='check',
                        message='低频权重检查已排队',
                    )
            if isinstance(requested, tuple) and requested[0] == 'custom':
                self._custom_once(requested)
            elif requested == 'reload':
                self.reload_once()
            elif requested is not None or due:
                self.check_once(allow_removals=bool(requested))
            self._wake.wait(30)
            self._wake.clear()

    def stop(self):
        self.stop_event.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.25)
