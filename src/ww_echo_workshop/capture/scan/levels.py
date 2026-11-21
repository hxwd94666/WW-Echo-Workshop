# 解析扫描等级候选与筛选要求。
"""Only frozen grade blocks enter this worker; no game capture/input here."""

from dataclasses import dataclass
from ww_echo_workshop.capture.scan.pipeline import observed_level


@dataclass(frozen=True)
class GradeJob:
    generation: int
    page: int
    token: object
    crops: tuple
    selected: object = None  # Optional single header fallback for unreadable slot.


def crop_levels(frame, tiles, generation, page, token):
    """Read only measured cards, including partial rows at the bag bottom."""
    import numpy as np

    if not 1 <= len(tiles) <= 30:
        raise ValueError('Invalid number of grade positions')
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
    columns = min(6, count)
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


def freeze_selected_grade(frame, viewport, detail):
    """Copy the observed panel before trusting its selected-grade crop."""
    import numpy as np
    from ww_echo_workshop.capture.scan.pipeline import (
        _detail_observation,
        _same_detail_observation,
        bag_region,
    )

    owned = np.array(frame, copy=True)
    if not _same_detail_observation(detail, _detail_observation(owned, viewport)):
        return None
    x, y, w, h = bag_region(owned, viewport, (0.665, 0.15, 0.795, 0.235))
    # Keep only the header once verified; no full image waits in the grade queue.
    crop = owned[y : y + h, x : x + w, :3].copy()
    crop.flags.writeable = False
    return crop


class FixedFrameGate:
    """Two fresh stable detail observations; grade is bound to this batch."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.selection = None
        self.clear_samples()

    def clear_samples(self):
        self.key = None
        self.sample = None
        self.detail = None

    def arm(self, position, generation, before_token, at):
        self.selection = (position, generation, before_token, at)
        self.clear_samples()

    def armed_for(self, position, generation):
        return self.selection is not None and self.selection[:2] == (position, generation)

    def stable_pair(self, frame, viewport, token, at, position, generation, *, scope):
        """Verify two frames after the click, even before the grade is known."""
        import math
        from ww_echo_workshop.capture.scan.pipeline import (
            _detail_observation,
            _same_detail_observation,
        )

        if (
            not self.armed_for(position, generation)
            or token is None
            or token == self.selection[2]
            or not isinstance(at, (int, float))
            or not math.isfinite(at)
            or at <= self.selection[3]
        ):
            return None
        key = (tuple(viewport), position, generation, scope)
        if key != self.key:
            self.clear_samples()
            self.key = key
        detail = _detail_observation(frame, viewport)
        if not any(part.any() for part in detail):
            self.clear_samples()
            return None
        previous = self.sample
        old_detail = self.detail
        if previous is not None and previous[0] == token:
            return None
        self.sample = (token, at)
        self.detail = detail
        if (
            previous is None
            or not 0 < at - previous[1] <= 5
            or not _same_detail_observation(old_detail, detail)
        ):
            return None
        return previous

    def observe(
        self, frame, viewport, token, at, position, generation, level, grade_token, *, page_offset
    ):
        from ww_echo_workshop.capture.scan.pipeline import ScanProof, detail_signature

        previous = self.stable_pair(
            frame,
            viewport,
            token,
            at,
            position,
            generation,
            scope=('proof', level, grade_token, page_offset),
        )
        if previous is None or type(level) is not int:
            return None
        proof = ScanProof(
            position,
            generation,
            level,
            (previous[0], token),
            (previous[1], at),
            detail_signature(frame, viewport),
            (generation, page_offset, grade_token),
        )
        return proof if proof.valid() else None


def page_content(frame, tiles):
    """Tiny owned samples of card centers, away from the selection border."""
    import cv2

    samples = []
    for tile in tiles:
        x, y, w, h = tile.bounds
        crop = frame[y + h // 4 : y + h * 3 // 4, x + w // 4 : x + w * 3 // 4, :3]
        if not crop.size:
            return ()
        samples.append(cv2.resize(crop, (8, 8), interpolation=cv2.INTER_AREA))
    return tuple(samples)


def page_moved(before, after):
    import numpy as np

    return bool(
        before
        and len(before) == len(after)
        and sum(float(np.mean(np.abs(a.astype(np.int16) - b))) > 15 for a, b in zip(before, after))
        >= 3
    )


def selected_slot(frame, target, viewport):
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
