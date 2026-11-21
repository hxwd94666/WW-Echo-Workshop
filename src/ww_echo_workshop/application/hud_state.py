# 保存实时评分 HUD 的只读文本快照。
import threading
import time


# Longer than the normal full-frame OCR refresh, but bounded across disconnects.
LIVE_HUD_TTL_SECONDS = 6.0
HUD_POLICY_HEADER = '强化策略'
HUD_POLICY_BUSY = '计算中…'
HUD_COMPARISON_SEPARATOR_RGB = (196, 199, 204)


class HudState:
    def __init__(self):
        self._lock = threading.Lock()
        self._text = ''
        self._colours = ()
        self._rows = ()
        self._layout = ''
        self._viewport = ()
        self._frame_size = ()
        self._updated_at = 0.0
        self._visible = False

    def update(self, text, colours=(), *, layout='', viewport=None, frame_size=None):
        text = str(text or '')
        colours = tuple(colours or ())
        rows = []
        for index, line in enumerate(text.splitlines()):
            colour = colours[index] if index < len(colours) else None
            if isinstance(colour, (tuple, list)) and len(colour) == 1:
                colour = colour[0]
            rows.append((line, colour))
        with self._lock:
            self._text = text
            self._colours = colours
            self._rows = tuple(rows)
            self._layout = str(layout or '')
            self._viewport = tuple(viewport) if viewport else ()
            self._frame_size = tuple(frame_size) if frame_size else ()
            self._updated_at = time.monotonic()
            self._visible = bool(text)

    def hide(self):
        with self._lock:
            self._visible = False
            self._text = ''
            self._colours = ()
            self._rows = ()
            self._layout = ''
            self._viewport = ()
            self._frame_size = ()
            self._updated_at = time.monotonic()

    def snapshot(self):
        with self._lock:
            visible = self._visible and time.monotonic() - self._updated_at < LIVE_HUD_TTL_SECONDS
            return {
                'text': self._text if visible else '',
                'colours': self._colours if visible else (),
                'rows': self._rows if visible else (),
                'layout': self._layout if visible else '',
                'viewport': self._viewport if visible else (),
                'frame_size': self._frame_size if visible else (),
                'updated_at': self._updated_at,
                'visible': visible,
            }
