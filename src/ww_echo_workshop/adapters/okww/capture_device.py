# 核对宿主窗口捕获与输入设备身份。
from dataclasses import dataclass

from ok import og
from ok.task.exceptions import CaptureException

_UNDECLARED_WINDOW = object()


def device_manager():
    return getattr(og, 'device_manager', None)


def _window_valid(window):
    return bool(
        window is not None
        and getattr(window, 'exists', False)
        and getattr(window, 'pos_valid', True)
    )


def _capture_matches_window(capture, window):
    if capture is None:
        return False
    bound = getattr(capture, 'hwnd_window', _UNDECLARED_WINDOW)
    # Non-window backends do not necessarily expose a binding. For a backend
    # that does, the manager and capture must agree even if handles match.
    return bound is _UNDECLARED_WINDOW or (bound is window and _window_valid(window))


def game_foreground(manager):
    window = getattr(manager, 'hwnd_window', None)
    if not _window_valid(window) or not getattr(window, 'visible', False):
        return False
    foreground = getattr(window, 'is_foreground', None)
    try:
        return bool(callable(foreground) and foreground())
    except OSError:
        return False


def _executor_uses_capture(manager, executor, capture):
    # OK Script can substitute a capture on the executor thread for a job.
    # A frame from that override is not evidence for the selected device.
    return (
        getattr(executor, 'device_manager', manager) is manager
        and getattr(executor, 'method', capture) is capture
    )


def capture_connected(manager, executor):
    capture = getattr(manager, 'capture_method', None)
    connected = getattr(capture, 'connected', None)
    try:
        return bool(
            not executor.paused
            and _capture_matches_window(capture, getattr(manager, 'hwnd_window', None))
            and _executor_uses_capture(manager, executor, capture)
            and callable(connected)
            and connected()
            and getattr(manager, 'capture_method', None) is capture
            and _executor_uses_capture(manager, executor, capture)
        )
    except (OSError, RuntimeError):
        return False


@dataclass(frozen=True)
class FrameSample:
    pixels: object
    token: object
    acquired_at: float


def frame_token(executor):
    # Both 2.0.x and 3.0.6 update this on successful executor.next_frame().
    # A passive read has no executor token and is never eligible for intake.
    return getattr(executor, '_last_frame_time', None)


def _valid_pixels(frame):
    # Host-side WGC_HDR already converts its FP16 surface before get_frame()
    # returns. Use the same unmodified 8-bit BGR pixels for OCR and evidence.
    return (
        getattr(frame, 'ndim', None) == 3
        and frame.shape[2] == 3
        and frame.shape[0] > 0
        and frame.shape[1] > 0
        and str(frame.dtype) == 'uint8'
    )


def read_task_sample(task, clock, get_manager, *, identify=None):
    executor = task.executor
    manager = get_manager()
    capture = getattr(manager, 'capture_method', None)
    identify = identify or capture_source_identity

    def same_owner():
        return (
            get_manager() is manager
            and getattr(manager, 'capture_method', None) is capture
            and _capture_matches_window(capture, getattr(manager, 'hwnd_window', None))
            and _executor_uses_capture(manager, executor, capture)
        )

    source = identify(manager)
    if source is None or not same_owner():
        return None
    before = frame_token(executor)
    # The task wrapper hard-codes the default six-second retry. A fresh
    # intake acquisition must return to the session guard even without pixels.
    try:
        pixels = executor.next_frame(time_out=0.5)
    except CaptureException:
        return None
    after = frame_token(executor)
    if (
        not _valid_pixels(pixels)
        or after is None
        or after == before
        or not same_owner()
        or identify(manager) != source
    ):
        return None
    return FrameSample(pixels, after, clock())


def read_capture_frame(manager):
    """Read a fresh host capture frame; never use the cached frame property."""
    capture = getattr(manager, 'capture_method', None)
    window = getattr(manager, 'hwnd_window', None)
    if capture is None or not _window_valid(window):
        return None
    get_frame = getattr(capture, 'get_frame', None)
    if not callable(get_frame):
        return None
    try:
        # Build the identity from the same objects that will supply pixels,
        # not from a separate read that might observe an already newer backend.
        identity = _capture_identity(capture, window)
        if identity is None or capture_source_identity(manager) != identity:
            return None
        frame = get_frame()
        return (
            frame
            if _valid_pixels(frame)
            and capture_source_identity(manager) == identity
            and _window_valid(window)
            else None
        )
    except Exception:
        return None


def capture_source_identity(manager):
    return _capture_identity(
        getattr(manager, 'capture_method', None), getattr(manager, 'hwnd_window', None)
    )


def _capture_identity(capture, window):
    if not _capture_matches_window(capture, window):
        return None
    return (
        id(capture),
        id(window),
        getattr(window, 'hwnd', None),
        getattr(window, 'top_hwnd', None),
        # Same handles can still start capturing a different client region
        # or child-window family. This signature exists in both host lines.
        getattr(window, 'capture_target_signature', None),
    )


def foreground_scan_supported(manager):
    """Recognize the existing native foreground coordinate path only.

    This is not a background allowlist. HDR and browser subclasses are not
    assumed equivalent to the SDR window capture they inherit from.
    """
    try:
        from ok.device.capture_methods.windows_graphics import WindowsGraphicsCaptureMethod
        from ok.device.capture_methods.bitblt import BitBltCaptureMethod
    except ImportError:
        return False
    window = getattr(manager, 'hwnd_window', None)
    capture = getattr(manager, 'capture_method', None)
    return bool(
        _window_valid(window)
        and type(capture) in (WindowsGraphicsCaptureMethod, BitBltCaptureMethod)
        and getattr(capture, 'hwnd_window', None) is window
        and callable(getattr(capture, 'get_abs_cords', None))
    )


def scan_device_identity(manager):
    window = getattr(manager, 'hwnd_window', None)
    interaction = getattr(manager, 'interaction', None)
    root = getattr(window, 'hwnd', None)
    top = getattr(window, 'top_hwnd', None)
    children = tuple(sorted({row[0] for row in getattr(window, 'hwnds', ()) if row}))
    # The host selects different child render windows based on each point.
    # Freeze the allowed window family, not its last dynamically routed child.
    target = getattr(interaction, '_dynamic_target_hwnd', 0)
    foreign_target = target if target and target not in (root, top, *children) else None
    return (
        _capture_identity(getattr(manager, 'capture_method', None), window),
        id(interaction),
        children,
        foreign_target,
        tuple(
            getattr(window, field, None)
            for field in ('x', 'y', 'width', 'height', 'real_x_offset', 'real_y_offset')
        ),
    )
