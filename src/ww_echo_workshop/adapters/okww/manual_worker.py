# 承载手动收录冻结帧的后台解析队列。
import queue
import threading
import time

from ww_echo_workshop.core.cancellation import OperationCancelled


class ManualWorkerMixin:
    def _enqueue_manual(self, snapshot, request, identity, settings, runtime, frozen_settings):
        with self._manual_lock:
            if self._manual_stop.is_set() or request.cancelled.is_set():
                return False
            entry = (snapshot, request, identity, settings, runtime, frozen_settings)
            try:
                self._manual_queue.put_nowait(entry)
            except queue.Full:
                # 已取消的等待项不应挡住重试；正在解析的项仍由工作线程收尾。
                try:
                    waiting = self._manual_queue.get_nowait()
                except queue.Empty:
                    pass  # 工作线程已取走等待项。
                else:
                    try:
                        if not waiting[1].cancelled.is_set():
                            self._manual_queue.put_nowait(waiting)
                            return False
                    finally:
                        self._manual_queue.task_done()
                self._manual_queue.put_nowait(entry)
            if self._manual_thread is None:
                self._manual_thread = threading.Thread(
                    target=self._manual_worker, name='EchoManualParse', daemon=True
                )
                self._manual_thread.start()
        self._manual_total_captures += 1
        self.log_info(
            f'手动收录已冻结入队：截图总计={self._manual_total_captures}，'
            f'队列={self._manual_queue.qsize()}'
        )
        return True

    def _close_manual_worker(self):
        self._manual_stop.set()
        with self._manual_lock:
            while True:
                try:
                    _, request, *_ = self._manual_queue.get_nowait()
                except queue.Empty:
                    break
                request.cancelled.set()
                self._manual_queue.task_done()
            worker = self._manual_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=0.25)

    def _manual_worker(self):
        from .manual_capture import parse_manual_snapshot

        while not self._manual_stop.is_set():
            try:
                snapshot, request, identity, settings, runtime, frozen_settings = (
                    self._manual_queue.get(timeout=0.2)
                )
            except queue.Empty:
                continue
            try:
                if request.cancelled.is_set() or self._manual_stop.is_set():
                    continue
                self._manual_parsing = True
                started = time.monotonic()
                self.log_info('后台收录解析开始')
                parse_manual_snapshot(
                    self, settings, runtime, request, snapshot, identity, frozen_settings
                )
                self.log_info(f'后台收录解析结束：{time.monotonic() - started:.2f}s')
            except OperationCancelled:
                runtime.capture.cancel_once(
                    '本次收录已取消',
                    diagnostic_state='cancelled',
                    summary_code='capture_cancelled',
                    request=request,
                )
            except Exception as error:
                runtime.capture.fail_manual('recognizing', error, request=request)
                self.log_warning(f'后台收录解析失败：{error}')
            finally:
                self._manual_parsing = False
                self._manual_queue.task_done()
