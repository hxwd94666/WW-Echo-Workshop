# 后台清理失去库存引用的截图，并接收用户确认后的全量截图清理请求。
from dataclasses import dataclass, replace
import logging
import threading
import time

from echo_workshop.application.operations import OperationCancelled
from echo_workshop.application.shutdown import join_bounded


@dataclass(frozen=True)
class CleanupStatus:
    state: str = 'idle'
    message: str = ''


class ScreenshotCleanupService:
    def __init__(self, repository, evidence, stop_event, *, interval=2):
        self.repository, self.evidence, self.stop_event = repository, evidence, stop_event
        self.interval = interval
        self._lock, self._wake = threading.Lock(), threading.Event()
        self._status, self._manual, self._running = CleanupStatus(), False, False
        self._revision, self._next_sweep = None, 0
        self._thread = None

    @property
    def status(self):
        with self._lock:
            return self._status

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name='echo-screenshot-cleanup', daemon=True
        )
        self._thread.start()

    def request_all(self):
        with self._lock:
            if self.stop_event.is_set() or self._manual or self._running:
                return False
            self._manual = True
            self._status = CleanupStatus('queued', '正在清理全部声骸截图…')
        self._wake.set()
        return True

    def notify(self):
        self._wake.set()

    def _run(self):
        while not self.stop_event.is_set():
            self._wake.clear()
            with self._lock:
                manual, self._manual = self._manual, False
            revision = self.repository.try_revision()
            if manual or (
                revision is not None
                and (revision != self._revision or time.monotonic() >= self._next_sweep)
            ):
                with self._lock:
                    self._running = True
                    if manual:
                        self._status = replace(self._status, state='running')
                try:
                    result = self.repository.clean_screenshots(
                        self.evidence, remove_all=manual, cancel_event=self.stop_event
                    )
                    self._revision = result['revision']
                    self._next_sweep = time.monotonic() + (30 if result['failed'] else 300)
                    if manual:
                        message = f'已清理 {result["removed"]} 张截图；声骸数据与配装保留。'
                        if result['failed']:
                            message += f' {result["failed"]} 张正被占用，请稍后重试。'
                        with self._lock:
                            self._status = CleanupStatus('completed', message)
                except OperationCancelled:
                    return
                except (OSError, ValueError) as error:
                    # Contention is retried; no live data is rewritten to hide a cleanup failure.
                    self._revision, self._next_sweep = revision, time.monotonic() + 30
                    if manual:
                        with self._lock:
                            self._status = CleanupStatus('failed', '截图清理未完成，请稍后重试。')
                    logging.getLogger(__name__).warning('screenshot cleanup deferred: %s', error)
                finally:
                    with self._lock:
                        self._running = False
            self._wake.wait(self.interval)

    def close(self):
        self._wake.set()
        if self._thread is not None:
            join_bounded(self._thread, 2.0, name='screenshot-cleanup')
