# 承载手动收录冻结帧的后台解析队列。
import queue
import time

from echo_workshop.core.cancellation import OperationCancelled


class ManualWorkerMixin:
    def _enqueue_manual(self, snapshot, request, identity, settings, runtime):
        try:
            self._manual_queue.put_nowait((snapshot, request, identity, settings, runtime))
        except queue.Full:
            return False
        self._manual_pending.append(request)
        runtime.capture._queued_requests.append(request)
        self._manual_total_captures += 1
        self.log_info(
            f'手动收录已冻结入队：截图总计={self._manual_total_captures}，'
            f'队列={self._manual_queue.qsize()}'
        )
        return True

    def _manual_worker(self):
        from .manual_capture import parse_manual_snapshot

        while not self._manual_stop.is_set():
            try:
                snapshot, request, identity, settings, runtime = self._manual_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._manual_parsing = True
                started = time.monotonic()
                self.log_info('后台收录解析开始')
                parse_manual_snapshot(self, settings, runtime, request, snapshot, identity)
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
                self._manual_pending = [
                    pending for pending in self._manual_pending if pending is not request
                ]
                runtime.capture._queued_requests = [
                    queued for queued in runtime.capture._queued_requests if queued is not request
                ]
                self._manual_queue.task_done()
