# 检测当前声骸展示布局和可用范围。
"""Captured-pixel content bounds and height-scaled, edge-anchored panel views."""

from dataclasses import dataclass
from types import SimpleNamespace


@dataclass(frozen=True)
class EchoViewport:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def checked(cls, value, frame_width, frame_height):
        if value is None:
            value = (0, 0, frame_width, frame_height)
        if (
            not isinstance(value, (tuple, list))
            or len(value) != 4
            or any(type(v) is not int for v in value)
        ):
            raise ValueError('Viewport must contain four integer pixel bounds')
        x, y, w, h = value
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > frame_width or y + h > frame_height:
            raise ValueError('Viewport must stay within the captured frame')
        return cls(x, y, w, h)

    @property
    def bounds(self):
        return (self.x, self.y, self.width, self.height)

    @property
    def wide(self):
        return self.width * 9 - self.height * 16 > self.height * 0.02

    @property
    def narrow(self):
        return self.height * 16 - self.width * 9 > self.height * 0.02

    @property
    def reference_width(self):
        return self.height * 16 / 9 if self.wide else self.width

    def offset(self, side):
        return (self.x + (self.width - self.reference_width if side == 'right' else 0), self.y)

    def live_regions(self, layout=''):
        """Fixed panels for the last known page; fall back to full-panel OCR."""
        if layout not in ('enhancement', 'single', 'display', 'tuning', 'comparison'):
            return self.ocr_regions()
        if layout == 'comparison':
            return self.ocr_regions()
        if layout in ('enhancement', 'tuning'):
            # The enhancement header/COST sits outside the left stat column;
            # cropping it made the score flip between current/reference.
            return self.ocr_regions()
        if layout in ('single', 'display', 'bag'):
            return (
                (
                    self.x + round(self.width * 0.74),
                    self.y + round(self.height * 0.12),
                    round(self.width * 0.25),
                    round(self.height * 0.36),
                ),
            )
        return self.ocr_regions()

    def ocr_regions(self):
        if not self.wide:
            return (self.bounds,)
        # Avoid wasting the detector's fixed long-edge budget on the expanded
        # center. Both panel sides/header/owner/effect regions stay included.
        panel_width = round(self.reference_width * 0.44)
        return (
            (self.x, self.y, panel_width, self.height),
            (self.x + self.width - panel_width, self.y, panel_width, self.height),
        )

    def boxes(self, boxes, side):
        dx, dy = self.offset(side)
        # Exclude text in the bars. Never scale the actual OCR glyph geometry.
        return [
            SimpleNamespace(x=b.x - dx, y=b.y - dy, width=b.width, height=b.height, name=b.name)
            for b in boxes
            if self.x <= b.x < self.x + self.width and self.y <= b.y < self.y + self.height
        ]


def detect_echo_viewport(frame):
    """Trim only full, near-black edge bands; blank captures return unknown.

    This is an image-array measurement, not a user screenshot modification.
    Dark/occluded content may also have uniform edges: non-full bounds remain
    provisional and require review. Evidence crops preserve original pixels.
    """
    import numpy as np  # Supplied by the host, not needed by portable core imports.

    if frame is None or frame.ndim != 3 or frame.shape[2] < 3:
        return None
    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        return None
    rgb = frame[:, :, :3]
    rows = np.flatnonzero(rgb.max(axis=(1, 2)) > 3)
    cols = np.flatnonzero(rgb.max(axis=(0, 2)) > 3)
    if not len(rows) or not len(cols):
        return None
    return (int(cols[0]), int(rows[0]), int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1))
