# 共用计算取消异常并报告明确阶段的应用作业进度。
from echo_workshop.core.cancellation import OperationCancelled, checkpoint

__all__ = ['OperationCancelled', 'checkpoint', 'report_progress']


def report_progress(callback, stage, done, total, cancel_event=None):
    checkpoint(cancel_event)
    if callback is not None:
        callback(stage, done, total)
    checkpoint(cancel_event)
