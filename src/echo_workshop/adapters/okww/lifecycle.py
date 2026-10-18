# 接入宿主卸载并关闭后台资源。
"""Narrow compatibility hook for hosts that omit on_destroy during unload."""

from functools import wraps
from PySide6.QtCore import QObject, Signal


def install_unload_cleanup():
    from ok.core.task_manager import TaskManager

    if getattr(TaskManager, '_echo_unload_cleanup', False):
        return
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


class _WindowSignals(QObject):
    shutdown = Signal()
    shortcut = Signal(int, str)


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
