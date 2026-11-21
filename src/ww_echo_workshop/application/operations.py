# 共用计算取消异常并报告明确阶段的应用作业进度。
from ww_echo_workshop.core.cancellation import (
    OperationCancelled,
    checkpoint,
    report_progress,
)

__all__ = ['LinkedStop', 'OperationCancelled', 'checkpoint', 'report_progress']


class LinkedStop:
    """Check an operation's cancellation and its owning runtime's shutdown."""

    def __init__(self, local, shutdown):
        self._local, self._shutdown = local, shutdown

    def is_set(self):
        return self._shutdown.is_set() or (self._local is not None and self._local.is_set())
