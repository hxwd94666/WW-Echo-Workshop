# 定义纯计算及作业共用的协作取消标记。
class OperationCancelled(ValueError):
    """Cancellation is not a failed data commit."""


class ComparisonCancelled(Exception):
    """A snapshot-pinned scoring batch was cancelled."""


def checkpoint(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled('作业已取消；未完成部分不作为成功结果，已提交数据不撤销')
