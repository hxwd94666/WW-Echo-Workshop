# 接入宿主卸载并关闭后台资源。
"""旧版卸载兜底与跨线程窗口信号。"""

from functools import wraps
from importlib.util import find_spec
from PySide6.QtCore import QObject, Signal


def host_owns_imported_pages():
    # 2.0.17 和 3.x 的插件注册器接管了页面挂载与卸载通知。
    return find_spec('ok.core.plugins') is not None


def install_unload_cleanup():
    if host_owns_imported_pages():
        return False
    try:
        from ok.core.task_manager import TaskManager
    except ImportError:
        # The official on_destroy path still runs; only the fallback is absent.
        return False

    if getattr(TaskManager, '_echo_unload_cleanup', False):
        return True
    original = TaskManager.unload_task

    def unload(manager, task):
        registered = task in manager.task_map
        try:
            return original(manager, task)
        finally:
            # Opt-in marker set on our concrete tasks, not a class-name match
            # that could intercept a different plugin with a similar name.
            # A second cooperating unload can observe the map entry already
            # removed while first cleanup is still joining workers. Owned
            # settings tasks serialize on_destroy, so that caller also waits.
            if (registered or getattr(task, '_destroyed', False)) and getattr(
                task, '_echo_owned_cleanup', False
            ):
                task.on_destroy()

    TaskManager.unload_task = unload
    TaskManager._echo_unload_cleanup = True
    return True


class _WindowSignals(QObject):
    shutdown = Signal()
    shortcut = Signal(int, str, object)


def _window_lifetime_guard(method):
    @wraps(method)
    def open_window(task, *args, **kwargs):
        # Publish ownership before unload can close shared services. A click
        # during an existing unload must not wait on a worker join in Qt.
        if task._destroyed or not task._destroy_lock.acquire(blocking=False):
            return
        try:
            if not task._destroyed:
                return method(task, *args, **kwargs)
        finally:
            task._destroy_lock.release()

    return open_window
