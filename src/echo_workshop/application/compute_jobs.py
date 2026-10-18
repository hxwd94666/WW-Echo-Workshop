# 为交互计算和搜索提供有界守护队列，不在宿主退出时等待计算线程。
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
import queue
import threading
import time

from echo_workshop.application.compute_protocol import ComputeFailure, ComputeWorker


class _Stop:
    def __init__(self, closed, requested):
        self.closed, self.requested = closed, requested

    def is_set(self):
        return self.closed.is_set() or (self.requested is not None and self.requested.is_set())


@dataclass
class _Ticket:
    future: Future
    lane: str
    op: str
    builder: object
    deadline: float
    cancel: object
    phase: str = 'queued'
    lock: object = field(default_factory=threading.Lock)


class ComputeJobs:
    def __init__(self, executable=None, *, fault_tests=False):
        self._executable, self._fault_tests = executable, fault_tests
        self._closed, self._reaped = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._workers, self._threads, self._running = {}, {}, {}
        self._queues = {lane: queue.Queue(maxsize=8) for lane in ('interactive', 'search')}
        self._slots = {lane: threading.BoundedSemaphore(8) for lane in self._queues}
        self._failures = {lane: 0 for lane in self._queues}
        self._stalled = set()
        self._tickets = {}
        self._watcher = None

    @staticmethod
    def _finish(future, *, value=None, error=None):
        try:
            if error is None:
                future.set_result(value)
            else:
                future.set_exception(error)
        except InvalidStateError:
            pass  # Concurrent close or preparation deadline already completed it.

    def submit(self, op, build_payload, *, lane='interactive', timeout=10, cancel_event=None):
        if lane not in self._queues:
            raise ValueError('未知计算队列')
        if type(timeout) not in (int, float) or not 0 < timeout <= 125:
            raise ValueError('计算通信截止时间须为 0–125 秒')
        with self._lock:
            if self._closed.is_set():
                raise ComputeFailure('closed', '计算服务已关闭')
            if lane in self._stalled or self._failures[lane] >= 2:
                raise ComputeFailure('restart_limit', '计算队列故障，需要重新打开插件后再计算')
            if not self._slots[lane].acquire(blocking=False):
                raise ComputeFailure('queue_limit', '待计算作业已达上限')
            future = Future()
            future.add_done_callback(lambda _: self._slots[lane].release())
            ticket = _Ticket(
                future, lane, op, build_payload, time.monotonic() + timeout, cancel_event
            )
            try:
                self._queues[lane].put_nowait(ticket)
            except queue.Full as error:
                future.cancel()
                raise ComputeFailure('queue_limit', '待计算作业队列已满') from error
            self._tickets[future] = ticket
            if self._watcher is None:
                self._watcher = threading.Thread(
                    target=self._watch, daemon=True, name='echo-compute-deadlines'
                )
                self._watcher.start()
            if lane not in self._threads:
                thread = threading.Thread(
                    target=self._run, args=(lane,), daemon=True, name=f'echo-compute-{lane}'
                )
                self._threads[lane] = thread
                thread.start()
            return future

    def _drain(self, lane, error=None):
        while True:
            try:
                future = self._queues[lane].get_nowait().future
            except queue.Empty:
                return
            if error is None:
                future.cancel()
            else:
                self._finish(future, error=error)

    def _watch(self):
        while not self._closed.wait(0.02):
            with self._lock:
                tickets = tuple(self._tickets.values())
            for ticket in tickets:
                if ticket.future.done():
                    with self._lock:
                        self._tickets.pop(ticket.future, None)
                    continue
                preparing = False
                error = None
                with ticket.lock:
                    if ticket.future.done():
                        continue
                    if ticket.cancel is not None and ticket.cancel.is_set():
                        error = ComputeFailure('cancelled', '计算作业已取消')
                    elif time.monotonic() >= ticket.deadline:
                        error = ComputeFailure('timeout', '计算作业超过截止时间')
                    if error is not None:
                        preparing = ticket.phase == 'preparing'
                        if preparing:
                            with self._lock:
                                self._stalled.add(ticket.lane)
                        # Feedback does not wait for uninterruptible process creation or reap.
                        # The lane still owns that call; no replacement thread is started here.
                        self._finish(ticket.future, error=error)
                if preparing:
                    self._drain(ticket.lane, error)

    def _run(self, lane):
        while not self._closed.is_set():
            try:
                ticket = self._queues[lane].get(timeout=0.05)
            except queue.Empty:
                continue
            future, op, builder = ticket.future, ticket.op, ticket.builder
            deadline, cancel = ticket.deadline, ticket.cancel
            with ticket.lock:
                if future.done() or not future.set_running_or_notify_cancel():
                    continue
                ticket.phase = 'preparing'
            stop, worker = _Stop(self._closed, cancel), None
            with self._lock:
                self._running[lane] = (future, 'preparing')
            try:
                if stop.is_set():
                    raise ComputeFailure('cancelled', '计算已取消')
                if deadline <= time.monotonic():
                    raise ComputeFailure('timeout', '排队超过截止时间')
                payload = builder()
                with ticket.lock:
                    if future.done():
                        raise ComputeFailure('cancelled', '输入准备任务已终止')
                    ticket.phase = 'computing'
                with self._lock:
                    self._running[lane] = (future, 'computing')
                    if self._closed.is_set():
                        raise ComputeFailure('closed', '计算服务已关闭')
                    if lane in self._stalled or deadline <= time.monotonic():
                        raise ComputeFailure('timeout', '输入准备超过截止时间')
                    if self._failures[lane] >= 2:
                        raise ComputeFailure('restart_limit', '计算队列故障达到重建上限')
                    worker = self._workers.get(lane)
                    if worker is None or worker._closed.is_set():
                        worker = ComputeWorker(
                            self._executable, lane=lane, fault_tests=self._fault_tests
                        )
                        self._workers[lane] = worker
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ComputeFailure('timeout', '启动计算前已超过截止时间')
                value = worker.call(op, payload, timeout=remaining, cancel_event=stop)
                with self._lock:
                    self._failures[lane] = 0
                self._finish(future, value=value)
            except BaseException as error:
                if (
                    worker is not None
                    and worker._closed.is_set()
                    and getattr(error, 'kind', '') not in ('cancelled', 'closed')
                ):
                    with self._lock:
                        self._failures[lane] += 1
                self._finish(future, error=error)
            finally:
                with self._lock:
                    self._running.pop(lane, None)
                    self._tickets.pop(future, None)
            if lane in self._stalled:
                return

    def close(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            workers = tuple(self._workers.values())
            outstanding = tuple(self._tickets)
            self._tickets.clear()
        for lane in self._queues:
            self._drain(lane)
        for future in outstanding:
            self._finish(future, error=ComputeFailure('closed', '计算服务已关闭'))

        def cleanup():
            try:
                for worker in workers:
                    worker.close()
            finally:
                self._reaped.set()

        threading.Thread(target=cleanup, daemon=True, name='echo-compute-close').start()

    def wait_closed(self, timeout=2):
        return self._reaped.wait(timeout)
