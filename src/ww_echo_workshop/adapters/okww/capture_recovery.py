# 监测宿主截图恢复与识别中断。
"""Reconnect an active OK Script capture after the game process is restarted."""

import threading
import time

from ok.device.capture_methods.base import BaseWindowsCaptureMethod
from ok.util.logger import Logger


logger = Logger.get_logger(__name__)


def _desktop_selection(manager):
    preferred = manager.get_preferred_device()
    key = getattr(manager, 'config', {}).get('preferred', '')
    if preferred is None:
        # The old pc_<hwnd> entry may already have disappeared during a restart.
        if isinstance(key, str) and (key == 'pc' or key.startswith('pc_')):
            return key, None, False
    elif preferred.get('device') == 'windows':
        return key, preferred.get('real_hwnd'), True
    return None


class CaptureRecoveryMonitor:
    def __init__(
        self,
        device_manager,
        exit_event,
        interval=1.0,
        retry_delay=5.0,
        stale_frame_delay=10.0,
        *,
        owner_task=None,
        allowed=None,
    ):
        self.device_manager = device_manager
        self.exit_event = exit_event
        self.interval = interval
        self.retry_delay = retry_delay
        self.stale_frame_delay = stale_frame_delay
        self.owner_task = owner_task
        self.allowed = allowed
        self._score_frame_time = 0.0
        self._last_attempt = float('-inf')
        self._last_hwnd = 0
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name='EchoCaptureRecovery',
                daemon=True,
            )
            self._thread.start()

    def stop(self):
        self._stop_event.set()

    def record_frame(self):
        """Record a frame read by our own loop, not only by the host executor."""
        self._score_frame_time = time.time()

    def _run(self):
        while not self.exit_event.is_set() and not self._stop_event.wait(self.interval):
            try:
                self.poll()
            except Exception as error:
                logger.error('Echo capture recovery check failed', error)

    def _may_refresh(self, executor):
        if (
            executor is None
            or (self.owner_task is not None and self.owner_task.executor is not executor)
            or self.exit_event.is_set()
            or self._stop_event.is_set()
            or executor.paused
            or getattr(executor, '_exclusive_owner', None) is not None
            or getattr(executor, 'onetime_task_queue', ())
        ):
            return False
        current = getattr(executor, 'current_task', None)
        return bool(
            (current is None or current is self.owner_task)
            and (self.allowed is None or self.allowed())
        )

    def poll(self):
        """Refresh a started capture if its target or latest frame is stale."""
        manager = self.device_manager
        if manager is None or self.exit_event.is_set() or self._stop_event.is_set():
            return False

        window = getattr(manager, 'hwnd_window', None)
        hwnd = getattr(window, 'hwnd', 0) if getattr(window, 'exists', False) else 0
        if not hwnd:
            self._last_hwnd = 0
            self._last_attempt = float('-inf')
            return False
        if not getattr(window, 'pos_valid', True):
            return False

        executor = getattr(manager, 'executor', None)
        capture = getattr(manager, 'capture_method', None)
        if not self._may_refresh(executor):
            return False
        if capture is not None and not isinstance(capture, BaseWindowsCaptureMethod):
            return False
        choice = _desktop_selection(manager)
        if choice is None:
            return False
        signature = getattr(window, 'capture_target_signature', None)
        now = time.monotonic()
        hwnd_changed = self._last_hwnd not in (0, hwnd)
        wrong_selection = not choice[2] or choice[1] not in (None, 0, hwnd)
        connected = capture is not None and capture.connected()
        last_frame = max(getattr(executor, '_last_frame_time', 0), self._score_frame_time)
        stale_frame = (
            last_frame > 0
            and time.time() - last_frame >= self.stale_frame_delay
            and getattr(window, 'visible', True)
        )
        if not (hwnd_changed or wrong_selection or not connected or stale_frame):
            self._last_hwnd = hwnd
            return False
        if now - self._last_attempt < self.retry_delay:
            return False
        if (
            getattr(manager, 'executor', None) is not executor
            or getattr(manager, 'capture_method', None) is not capture
            or getattr(manager, 'hwnd_window', None) is not window
            or getattr(window, 'hwnd', None) != hwnd
            or getattr(window, 'capture_target_signature', None) != signature
            or _desktop_selection(manager) != choice
            or not self._may_refresh(executor)
        ):
            return False
        self._last_attempt, self._last_hwnd = now, hwnd
        reason = (
            'new game HWND'
            if hwnd_changed
            else 'stale selected HWND'
            if wrong_selection
            else 'capture disconnected'
            if not connected
            else 'no recent frames'
        )
        logger.info(f'Refreshing Echo capture for HWND {hwnd}: {reason}')
        # BitBlt.connected() only checks that the *window* exists, so it can
        # report True while its old GDI context returns no frames. Release it
        # before the normal device refresh, which also updates preferred
        # pc_<hwnd> and the overlay source through OK Script's device signal.
        if capture is not None:
            try:
                capture.close()
            except Exception as error:
                logger.error('Failed to close stale Echo capture', error)
        if not self._may_refresh(executor):
            return False
        manager.refresh()
        return True
