# 为后台线程提供有界回收，避免宿主关闭被永久阻塞。
import threading


def join_bounded(thread, timeout, *, name='background'):
    """Join a background owner within a finite budget; never block the UI forever."""
    if thread is None or thread is threading.current_thread():
        return True
    thread.join(max(0.0, float(timeout)))
    return not thread.is_alive()
