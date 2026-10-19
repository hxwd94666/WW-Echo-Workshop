# 为编辑器保存提供后台原生质量校验，避免 GUI 线程同步等待计算。
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityCheck:
    future: object
    records: tuple


class QualityCheckService:
    def __init__(self, quality):
        self._quality = quality
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='echo-quality-check')

    def start(self, records, *, cancel_event=None):
        records = tuple(records)
        future = self._pool.submit(self._quality, records, cancel_event=cancel_event)
        return QualityCheck(future, records)

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
