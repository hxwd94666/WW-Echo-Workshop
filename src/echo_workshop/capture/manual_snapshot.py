# 用多张新鲜采样验证冻结声骸画面，一次解析后绑定同帧证据。
from dataclasses import dataclass
import hashlib
import math

from echo_workshop.capture.metadata import metadata_regions


def record_fields(record):
    return (
        record.cost,
        tuple((r.stat_name, r.value) for r in record.main_stats),
        tuple((r.stat_name, r.value) for r in record.sub_stats),
    )


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
        """Check each recognized text/value patch, never the animated Echo model."""
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
        regions = metadata_regions(analysis, width, height)
        rows = analysis.main_rows + analysis.sub_rows
        rectangles = [(r.x, r.y, r.width, r.height) for r in rows]
        panel_left = min(r.x for r in rows)
        # Separate OCR boxes ensure a small changed digit is not diluted by the
        # full stat row; name/level/owner candidates are verified as well.
        for box in metadata.boxes:
            cx, cy = box.x + box.width / 2, box.y + box.height / 2
            in_row = any(r.x <= cx <= r.x + r.width and r.y <= cy <= r.y + r.height for r in rows)
            in_meta = any(x <= cx <= x + w and y <= cy <= y + h for x, y, w, h in regions)
            # Include the selected panel's Sonata/skill text too, but never the
            # neighbouring Echo or the animated model in the centre of the view.
            in_panel = panel_left <= cx <= width and height * 0.08 <= cy <= height * 0.95
            if in_row or in_meta or in_panel:
                rectangles.append((box.x - 2, box.y - 2, box.width + 4, box.height + 4))
        previous = None
        for frame in self.frames:
            if frame.shape != self.frame.shape or frame.flags.writeable:
                return None
            masks = []
            for x, y, w, h in rectangles:
                x1, y1 = max(0, round(x)), max(0, round(y))
                x2, y2 = min(width, round(x + w)), min(height, round(y + h))
                if x2 <= x1 or y2 <= y1:
                    return None
                import cv2

                masks.append(cv2.cvtColor(frame[y1:y2, x1:x2, :3], cv2.COLOR_BGR2GRAY) > 185)
            if previous is not None:
                import numpy as np

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
