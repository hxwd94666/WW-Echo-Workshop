# 解析扫描等级候选与筛选要求。
"""Only frozen grade blocks enter this worker; no game capture/input here."""

from dataclasses import dataclass
from echo_workshop.capture.scan.pipeline import observed_level


def wheel_count(batch):
    """One-based scroll number: five 24s, one 25; no inferred displacement."""
    if type(batch) is not int or batch < 1:
        raise ValueError('Positive scroll batch required')
    return 25 if batch % 6 == 0 else 24


@dataclass(frozen=True)
class GradeJob:
    generation: int
    page: int
    token: object
    crops: tuple
    selected: object = None  # Optional single header fallback for unreadable slot.


def crop_levels(frame, tiles, generation, page, token):
    """Exactly eighteen bottom-right +N patches, not a full-page OCR image."""
    import numpy as np

    if len(tiles) != 18:
        raise ValueError('Expected eighteen grade positions')
    crops = []
    for tile in tiles:
        x, y, w, h = tile.bounds
        # Keep only +N text, excluding COST, icon, portraits and the model.
        patch = frame[
            y + round(h * 0.79) : y + h - round(h * 0.01),
            x + round(w * 0.49) : x + w - round(w * 0.01),
            :3,
        ]
        if patch.size == 0:
            raise ValueError('Missing grade crop')
        patch = np.array(patch, copy=True)
        patch.flags.writeable = False
        crops.append(patch)
    return GradeJob(generation, page, token, tuple(crops))


def read_levels(job, read):
    """Pack separated blocks once; map OCR by block, retry only unknown blocks."""
    import numpy as np
    from copy import copy

    count = len(job.crops)
    columns = 6 if count == 18 else 1
    gap = 16
    w = max(c.shape[1] for c in job.crops)
    h = max(c.shape[0] for c in job.crops)
    packed = np.zeros(
        (((count + columns - 1) // columns) * (h + gap), columns * (w + gap), 3), np.uint8
    )
    bounds = []
    for i, crop in enumerate(job.crops):
        x = (i % columns) * (w + gap)
        y = (i // columns) * (h + gap)
        ch, cw = crop.shape[:2]
        packed[y : y + ch, x : x + cw] = crop
        bounds.append((x, y, cw, ch))
    packed.flags.writeable = False
    boxes = read(packed)
    values = []
    for i, (x, y, cw, ch) in enumerate(bounds):
        local = []
        for box in boxes:
            if (
                x <= box.x
                and box.x + box.width <= x + cw
                and y <= box.y
                and box.y + box.height <= y + ch
            ):
                mapped = copy(box)
                mapped.x -= x
                mapped.y -= y
                local.append(mapped)
        level = observed_level(local)
        if level is None:
            level = observed_level(read(job.crops[i]))
        values.append(level)
    return tuple(values)


class FixedFrameGate:
    """Two fresh stable detail observations; grade is bound to this batch."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.key = None
        self.sample = None
        self.detail = None

    def observe(self, frame, viewport, token, at, position, generation, level, grade_token):
        import math
        from echo_workshop.capture.scan.pipeline import (
            _detail_observation,
            _same_detail_observation,
            detail_signature,
            ScanProof,
        )

        if token is None or not isinstance(at, (int, float)) or not math.isfinite(at):
            return None
        key = (tuple(viewport), position, generation, level, grade_token)
        if key != self.key:
            self.reset()
            self.key = key
        detail = _detail_observation(frame, viewport)
        previous = self.sample
        old_detail = self.detail
        if previous is not None and previous[0] == token:
            return None
        self.sample = (token, at)
        self.detail = detail
        if (
            type(level) is not int
            or previous is None
            or not 0 < at - previous[1] <= 5
            or not _same_detail_observation(old_detail, detail)
        ):
            return None
        proof = ScanProof(
            position,
            generation,
            level,
            (previous[0], token),
            (previous[1], at),
            detail_signature(frame, viewport),
            (grade_token,),
            (previous[0], token),
            (generation, position[0] // 3, grade_token),
        )
        return proof if proof.valid() else None


def selected_slot(frame, tiles, target, viewport):
    """Only check requested selection response at fixed points; never refit coordinates."""
    if target is None:
        return None
    import cv2
    import numpy as np

    x, y, w, h = target.bounds
    pad = max(5, round(viewport[3] * 0.012))
    for edge in (x, x + w):
        patch = frame[
            y + round(h * 0.18) : y + round(h * 0.82), max(0, edge - pad) : edge + pad + 1, :3
        ]
        if not patch.size:
            return None
        mask = cv2.inRange(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV), (15, 40, 125), (45, 255, 255))
        if float(np.mean(mask.max(axis=1) > 0)) < 0.45:
            return None
    return target.row, target.column


def list_bottom(frame, viewport):
    """End-of-list marker only. No movement/geometry/overlap rejection."""
    from echo_workshop.capture.viewport import EchoViewport
    import numpy as np

    area = EchoViewport.checked(viewport, frame.shape[1], frame.shape[0])
    x = area.x + round(area.reference_width * 0.642)
    y = area.y + round(area.height * 0.834)
    patch = frame[y : area.y + round(area.height * 0.85), x - 4 : x + 5, :3]
    return bool(patch.size and np.any((patch.min(axis=2) > 170).sum(axis=1) >= 2))
