# 保存实时评分 HUD 的只读文本快照。
import threading
import time


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
            return {
                'text': self._text,
                'colours': self._colours,
                'rows': self._rows,
                'layout': self._layout,
                'viewport': self._viewport,
                'frame_size': self._frame_size,
                'updated_at': self._updated_at,
                'visible': self._visible,
            }
