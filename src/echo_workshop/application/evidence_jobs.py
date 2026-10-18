# 调度声骸截图证据访问作业。
"""Owned evidence reads; workers never hold a QWidget or emit a Qt signal."""

from concurrent.futures import ThreadPoolExecutor
import threading


class EvidenceViewJob:
    def __init__(self):
        self.cancelled = threading.Event()
        self.future = None

    def cancel(self):
        self.cancelled.set()
        self.future.cancel()


class EvidenceViewJobs:
    def __init__(self, read):
        self.read = read
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='echo-evidence')
        self._jobs = set()
        self.closed = False

    def submit(self, reference, decode):
        job = EvidenceViewJob()

        def load():
            if job.cancelled.is_set():
                return None
            data = self.read(reference)
            if job.cancelled.is_set():
                return None
            image = decode(data)
            return None if job.cancelled.is_set() else image

        with self._lock:
            if self.closed:
                raise ValueError('插件已关闭，请重新打开后查看原图。')
            self._jobs.add(job)
            job.future = self._pool.submit(load)
        # Register outside the lock: an already-complete future calls inline.
        job.future.add_done_callback(lambda _: self._finished(job))
        return job

    def _finished(self, job):
        with self._lock:
            self._jobs.discard(job)

    def close(self):
        with self._lock:
            self.closed = True
            jobs = tuple(self._jobs)
        # Future.cancel can invoke _finished inline; never hold _lock here.
        for job in jobs:
            job.cancel()
        self._pool.shutdown(wait=True, cancel_futures=True)
