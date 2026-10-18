# 协调控制记录修复的显式后台作业与取消生命周期。
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading

from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.storage.control_repair import ControlRepair, RepairPreview


@dataclass(frozen=True)
class RepairStatus:
    job_id: int = 0
    kind: str = ''
    state: str = 'idle'
    message: str = ''
    preview: object = None
    result: object = None
    error_type: str = ''

    @property
    def busy(self):
        return self.state in ('running', 'cancelling')


class _Cancellation:
    def __init__(self, local, external):
        self.local = local
        self.external = external

    def is_set(self):
        return self.local.is_set() or self.external.is_set()


class RepairService:
    """No Qt/task ownership: the UI confirms intent and renders immutable status."""

    def __init__(self, config_file, *, stop_event=None, compute=None):
        from echo_workshop.application.compute_jobs import ComputeJobs
        from echo_workshop.application.allocation import AllocationService

        self.compute = compute if compute is not None else ComputeJobs()
        self._owns_compute = compute is None
        try:
            self._backend = ControlRepair(
                config_file, plan_evaluator=AllocationService(None, compute=self.compute)
            )
        except BaseException:
            if self._owns_compute:
                self.compute.close()
            raise
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='echo-control-repair')
        self._lock = threading.RLock()
        self._future = None
        self._cancel = threading.Event()
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._closed = False
        self._status = RepairStatus()
        self._job = None

    @property
    def future(self):
        with self._lock:
            return self._future

    @property
    def status(self):
        with self._lock:
            return self._status

    @property
    def accepting(self):
        with self._lock:
            return not self._closed and not self._stop.is_set()

    def candidates(self):
        with self._lock:
            if self._closed or self._stop.is_set():
                raise ValueError('修复服务已关闭。')
        return tuple(self._backend.manager.candidates())

    def selection_issue(self):
        return self._backend.selection_issue()

    def invalidate_preview(self):
        with self._lock:
            if self._future is not None:
                raise ValueError('请先完成或取消当前修复作业。')
            self._status = RepairStatus(job_id=self._status.job_id)

    def request_preview(self, kind, target=''):
        return self._submit('preview', (kind, target))

    def request_apply(self, preview):
        if not isinstance(preview, RepairPreview):
            raise ValueError('请先预览并明确确认修复。')
        with self._lock:
            if preview != self._status.preview:
                raise ValueError('修复预览已变化，请重新预览并确认。')
            return self._submit('apply', (preview,))

    def _submit(self, kind, arguments):
        with self._lock:
            if self._closed or self._stop.is_set():
                raise ValueError('修复服务已关闭。')
            if self._future is not None:
                raise ValueError('已有修复作业，请等待完成或取消。')
            self._cancel = threading.Event()
            token = _Cancellation(self._cancel, self._stop)
            identifier = self._status.job_id + 1
            self._status = RepairStatus(
                identifier,
                kind,
                'running',
                '正在预览，当前数据不变。' if kind == 'preview' else '正在修复，请稍候。',
            )
            self._job = begin_job('control-repair')
            self._job.stage(kind)
            operation = self._backend.preview if kind == 'preview' else self._backend.apply
            self._future = self._pool.submit(operation, *arguments, cancel_event=token)
            return identifier

    def cancel(self):
        with self._lock:
            self._cancel.set()
            if self._future is not None:
                self._status = RepairStatus(
                    self._status.job_id,
                    self._status.kind,
                    'cancelling',
                    '已请求取消；已提交数据不撤销。',
                )

    def poll(self):
        with self._lock:
            if self._future is None or not self._future.done():
                return self._status
            future = self._future
            self._future = None
            previous = self._status
            try:
                result = future.result()
                preview = result if previous.kind == 'preview' else None
                self._status = RepairStatus(
                    previous.job_id,
                    previous.kind,
                    'completed',
                    '预览完成，核对后可确认修复。'
                    if preview is not None
                    else '已修复，请重启 OKWW。',
                    preview=preview,
                    result=None if preview is not None else result,
                )
                self._job.finish('completed')
            except OperationCancelled:
                self._status = RepairStatus(
                    previous.job_id, previous.kind, 'cancelled', '作业已取消，已提交数据保留。'
                )
                self._job.finish('cancelled')
            except Exception as error:
                self._status = RepairStatus(
                    previous.job_id,
                    previous.kind,
                    'failed',
                    '作业未完成：' + str(error),
                    error_type=type(error).__name__,
                )
                self._job.finish('failed', error=error)
            return self._status

    def close(self):
        """Seal submission and join outside the lock; no widget/event calls."""
        with self._lock:
            self._closed = True
            self._cancel.set()
        if self._owns_compute:
            self.compute.close()
        self._pool.shutdown(wait=True, cancel_futures=False)
        self.poll()
