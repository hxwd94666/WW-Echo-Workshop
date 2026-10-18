# 为冻结库存视图串行调度最新一次原生筛选，关闭仅发取消不等待界面线程。
from dataclasses import dataclass
import threading

from echo_workshop.core.cancellation import OperationCancelled, checkpoint
from echo_workshop.application.compute_bridge import require_background


@dataclass(frozen=True)
class ViewMetadata:
    search_text: str
    reservation: str = ''

    def __post_init__(self):
        if not isinstance(self.search_text, str) or not isinstance(self.reservation, str):
            raise ValueError('视图检索/预留元数据须为文本')


@dataclass(frozen=True)
class ViewStatus:
    generation: int = 0
    state: str = 'idle'
    context: tuple = ()
    indices: tuple = ()
    message: str = ''


class _Stop:
    def __init__(self, local, runtime):
        self.local, self.runtime = local, runtime

    def is_set(self):
        return self.local.is_set() or (self.runtime is not None and self.runtime.is_set())


class InventoryViewJob:
    def __init__(self, *, queries, stop_event=None):
        self.queries, self.stop_event = queries, stop_event
        self._lock = threading.Lock()
        self._pending = self._cancel = self._thread = None
        self._closed = False
        self._stopped = threading.Event()
        self._stopped.set()
        self._status = ViewStatus()

    @property
    def status(self):
        with self._lock:
            return self._status

    def request(self, rows, filters, *, text='', context=()):
        if not isinstance(context, tuple):
            raise ValueError('视图上下文须为冻结元组')
        rows = tuple(rows)
        if len(rows) > 20000:
            raise ValueError('视图超过单批规模限额')
        with self._lock:
            if self._closed:
                raise ValueError('库存视图作业已关闭')
            if self._cancel is not None:
                self._cancel.set()
            generation = self._status.generation + 1
            self._pending = generation, context, rows, filters, text
            self._status = ViewStatus(generation, 'running', context)
            if self._thread is None:
                self._stopped.clear()
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name='echo-inventory-view'
                )
                self._thread.start()
            return generation

    def _run(self):
        while True:
            with self._lock:
                if self._closed or self._pending is None:
                    self._thread = None
                    self._stopped.set()
                    return
                generation, context, rows, filters, text = self._pending
                self._pending = None
                local = self._cancel = threading.Event()
            cancel = _Stop(local, self.stop_event)
            indices, state, message = (), 'ready', ''
            try:
                checkpoint(cancel)
                indices = self.queries.filter_view(rows, filters, text=text, cancel_event=cancel)
                checkpoint(cancel)
            except OperationCancelled:
                state, message = 'cancelled', '库存视图已取消，未发布部分行'
            except Exception as error:
                state, message = 'failed', str(error)
            with self._lock:
                self._cancel = None
                if not self._closed and generation == self._status.generation:
                    self._status = ViewStatus(generation, state, context, indices, message)

    def cancel(self):
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()
            self._pending = None
            if not self._closed:
                self._status = ViewStatus(self._status.generation + 1, 'cancelled')

    def close(self):
        with self._lock:
            self._closed = True
            self._pending = None
            if self._cancel is not None:
                self._cancel.set()
            self._status = ViewStatus(self._status.generation + 1, 'closed')

    def wait_closed(self, timeout=2):
        require_background()
        return self._stopped.wait(timeout)
