# 核对宿主窗口捕获与输入设备身份。


def capture_source_identity(manager):
    window = getattr(manager, 'hwnd_window', None)
    return (
        id(getattr(manager, 'capture_method', None)),
        getattr(window, 'hwnd', None),
        getattr(window, 'top_hwnd', None),
    )


def _window_capture_available(manager):
    """A paused foreground-input task may still observe its window capture."""
    if manager is None:
        return False
    try:
        from ok.device.capture_methods.windows_graphics import WindowsGraphicsCaptureMethod
        from ok.device.capture_methods.bitblt import BitBltCaptureMethod
    except ImportError:
        return False
    window = getattr(manager, 'hwnd_window', None)
    capture = getattr(manager, 'capture_method', None)
    return bool(
        window is not None
        and type(capture) in (WindowsGraphicsCaptureMethod, BitBltCaptureMethod)
        and getattr(capture, 'hwnd_window', None) is window
    )


def background_scan_available(manager):
    """Recognize actual host implementations, not configuration/display names.

    Exact types intentionally exclude foreground subclasses and custom backends.
    This permits background input; visual acknowledgements still prove response.
    """
    if not _window_capture_available(manager):
        return False
    try:
        from ok.device.interaction_methods.post_message import PostMessageInteraction
    except ImportError:
        return False
    window = manager.hwnd_window
    interaction = getattr(manager, 'interaction', None)
    return bool(
        type(interaction) is PostMessageInteraction
        and getattr(interaction, 'hwnd_window', None) is window
    )


def scan_device_identity(manager):
    window = getattr(manager, 'hwnd_window', None)
    interaction = getattr(manager, 'interaction', None)
    root = getattr(window, 'hwnd', None)
    top = getattr(window, 'top_hwnd', None)
    children = tuple(row[0] for row in getattr(window, 'hwnds', ()) if row)
    # The host selects different child render windows based on each point.
    # Freeze the allowed window family, not its last dynamically routed child.
    target = getattr(interaction, '_dynamic_target_hwnd', 0)
    foreign_target = target if target and target not in (root, top, *children) else None
    return (
        id(getattr(manager, 'capture_method', None)),
        getattr(window, 'hwnd', None),
        id(interaction),
        top,
        children,
        foreign_target,
    )
