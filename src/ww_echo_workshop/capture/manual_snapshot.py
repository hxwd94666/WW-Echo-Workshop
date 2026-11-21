# 用多张新鲜采样验证冻结声骸画面，一次解析后绑定同帧证据。
from dataclasses import dataclass
import hashlib
import math

from ww_echo_workshop.capture.evidence import evidence_bounds
from ww_echo_workshop.capture.metadata import metadata_regions
from ww_echo_workshop.capture.viewport import detect_echo_viewport


class ManualFrameBuffer:
    """Collect fresh frames from one viewport without depending on host state."""

    def __init__(self, request):
        self.request = request
        self.tokens, self.times, self.frames = [], [], []
        self.viewport = None

    def add(self, frame, token, acquired_at):
        if token is None or token == self.request.after_token or token in self.tokens:
            return False
        # OCR and evidence must use the same pixels from which we measured the viewport.
        frame = frame.copy()
        viewport = detect_echo_viewport(frame)
        if viewport is None or (
            self.frames and (viewport != self.viewport or frame.shape != self.frames[-1].shape)
        ):
            raise ValueError('采样画面无效或尺寸变化，请保持声骸页面后重试')
        self.viewport = viewport
        frame.flags.writeable = False
        self.frames.append(frame)
        self.tokens.append(token)
        self.times.append(acquired_at)
        return True

    def freeze(self):
        return FrozenManualSnapshot(
            self.request, tuple(self.frames), tuple(self.tokens), tuple(self.times), self.viewport
        )


def record_fields(record):
    return (
        record.cost,
        tuple((r.stat_name, r.value) for r in record.main_stats),
        tuple((r.stat_name, r.value) for r in record.sub_stats),
    )


def _panel_text_bands(frame, bounds, step):
    import cv2
    import numpy as np

    x, y, w, h = bounds
    gray = cv2.cvtColor(frame[y : y + h, x : x + w, :3], cv2.COLOR_BGR2GRAY)
    bright = (gray > 185).astype(np.uint8)
    # Reject tiny sparkles, not complete groups of text. Split the selected
    # panel vertically so a missing line is not diluted by unrelated rows.
    _, labels, stats, _ = cv2.connectedComponentsWithStats(bright, connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= 12
    keep[0] = False
    text = keep[labels]
    return tuple(text[start : start + step] for start in range(0, h, step))


@dataclass(frozen=True)
class ManualSnapshotProof:
    request: object
    tokens: tuple
    times: tuple
    viewport: tuple
    layout: str
    signature: str
    fields: tuple

    def valid(self, request, *, stable_frames=3, stable_seconds=0.6, max_frame_gap=5):
        return bool(
            self.request is request
            and self.layout in ('bag', 'display')
            and len(self.tokens) == len(self.times) >= stable_frames
            and all(t is not None and t != request.after_token for t in self.tokens)
            and len(set(self.tokens)) == len(self.tokens)
            and all(type(t) in (int, float) and math.isfinite(t) for t in self.times)
            and request.created_at <= self.times[0] <= self.times[-1] <= request.deadline
            and all(0 < b - a <= max_frame_gap for a, b in zip(self.times, self.times[1:]))
            and self.times[-1] - self.times[0] >= stable_seconds - 1e-6
            and len(self.signature) == 64
            and len(self.fields) == 3
        )


@dataclass(frozen=True)
class FrozenManualSnapshot:
    request: object
    frames: tuple
    tokens: tuple
    times: tuple
    viewport: tuple

    @property
    def frame(self):
        return self.frames[-1]

    def prove(self, metadata, record):
        """Validate both final OCR boxes and text visible only in earlier frames."""
        import cv2
        import numpy as np

        analysis = metadata.analysis
        if (
            record is None
            or analysis.layout not in ('bag', 'display')
            or len(analysis.main_rows) != 2
            or not self.frames
        ):
            return None
        expected = (
            analysis.cost,
            tuple((r.stat_name, r.value) for r in analysis.main_rows),
            tuple((r.stat_name, r.value) for r in analysis.sub_rows),
        )
        if record_fields(record) != expected:
            return None
        height, width = self.frame.shape[:2]
        try:
            panel = evidence_bounds(analysis, width, height)
        except ValueError:
            return None
        panel_x, panel_y, panel_w, panel_h = panel
        band_height = max(16, round(self.viewport[3] * 0.045))
        regions = metadata_regions(analysis, width, height)
        rows = analysis.main_rows + analysis.sub_rows
        rectangles = [(r.x, r.y, r.width, r.height) for r in rows]
        # Separate OCR boxes ensure a small changed digit is not diluted by the
        # full stat row; name/level/owner candidates are verified as well.
        for box in metadata.boxes:
            cx, cy = box.x + box.width / 2, box.y + box.height / 2
            in_row = any(r.x <= cx <= r.x + r.width and r.y <= cy <= r.y + r.height for r in rows)
            in_meta = any(x <= cx <= x + w and y <= cy <= y + h for x, y, w, h in regions)
            # Include the selected panel's Sonata/skill text too, but never the
            # neighbouring Echo or the animated model in the centre of the view.
            in_panel = panel_x <= cx < panel_x + panel_w and panel_y <= cy < panel_y + panel_h
            if in_row or in_meta or in_panel:
                rectangles.append((box.x - 2, box.y - 2, box.width + 4, box.height + 4))
        previous = None
        for frame in self.frames:
            if frame.shape != self.frame.shape or frame.flags.writeable:
                return None
            # OCR only runs on the final frame, so a row that disappeared
            # before freezing has no box. The bands cover those empty gaps.
            masks = list(_panel_text_bands(frame, panel, band_height))
            for x, y, w, h in rectangles:
                x1, y1 = max(0, round(x)), max(0, round(y))
                x2, y2 = min(width, round(x + w)), min(height, round(y + h))
                if x2 <= x1 or y2 <= y1:
                    return None
                masks.append(cv2.cvtColor(frame[y1:y2, x1:x2, :3], cv2.COLOR_BGR2GRAY) > 185)
            if previous is not None:
                for left, right in zip(previous, masks):
                    tolerance = max(6, round(min(int(left.sum()), int(right.sum())) * 0.025))
                    if np.count_nonzero(left != right) > tolerance:
                        return None
            previous = masks
        return ManualSnapshotProof(
            self.request,
            self.tokens,
            self.times,
            self.viewport,
            analysis.layout,
            hashlib.sha256(self.frame.tobytes()).hexdigest(),
            record_fields(record),
        )
