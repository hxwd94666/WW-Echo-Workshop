# 测量背包可见网格、选框与排序方向。
from dataclasses import replace
from echo_workshop.capture.viewport import EchoViewport
from .models import BagGrid, ScanTile


class BagGridDetector:
    """Locate six-column bag cards from measured borders in captured pixels.

    Scale derives from the current content height. Incomplete edge rows are
    excluded, and every inferred cell still needs both measured side borders.
    This detector alone does not establish that the current page is the bag.
    """

    def __init__(self):
        self._geometry = None
        self._last_grid = None

    def observe_cached(self, frame, viewport, *, context=()):
        """Measure fresh small border/rail patches against this batch's lattice.

        No contour fitting in the normal path. A missing/translated border or
        rail change returns unknown so the caller performs a full measurement.
        Cached coordinates never substitute for current-frame evidence.
        """
        import cv2
        import numpy as np

        previous = self._last_grid
        if previous is None or len(previous.tiles) != 18:
            return None
        area = EchoViewport.checked(viewport, frame.shape[1], frame.shape[0])
        if area.bounds != previous.viewport or tuple(context) != previous.context:
            return None
        h, w = area.height, area.reference_width
        pad = max(3, int(h * 0.006 + 0.999))
        image = frame[area.y : area.y + h, area.x : area.x + round(w), :3]
        rail_x = round(w * 0.642)
        rail = image[round(h * 0.12) : round(h * 0.85), rail_x - pad : rail_x + pad + 1]
        bright = (rail.min(axis=2) > 170).sum(axis=1) >= max(2, pad // 2)
        indices = np.flatnonzero(bright)
        runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1) if len(indices) else []
        knobs = [r for r in runs if 0.01 * h <= len(r) <= 0.045 * h]
        scrollbar = (round(h * 0.12) + float(np.mean(knobs[0]))) / h if len(knobs) == 1 else None
        bottom = len(knobs) == 1 and round(h * 0.12) + int(knobs[0][-1]) >= h * 0.839
        if (
            bottom != previous.bottom_confirmed
            or (scrollbar is None) != (previous.scrollbar is None)
            or (scrollbar is not None and abs(scrollbar - previous.scrollbar) >= 0.003)
        ):
            return None
        selected = []
        tiles = []
        top_shifts = {row: [] for row in range(3)}
        # Side patches are small, but include enough context for Canny. Gold
        # is checked on both sides so rarity stripes cannot imply selection.
        for tile in previous.tiles:
            ax, ay, cw, ch = tile.bounds
            x, y = ax - area.x, ay - area.y
            is_selected = True
            for edge_x in (x, x + cw):
                patch = image[y + pad : y + ch - pad, edge_x - 2 * pad : edge_x + 2 * pad + 1]
                if patch.shape[:2] != (ch - 2 * pad, 4 * pad + 1):
                    return None
                edge = cv2.Canny(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), 15, 45)
                if (edge[:, pad : 3 * pad + 1].max(axis=1) > 0).mean() <= 0.55:
                    return None
                gold = cv2.inRange(
                    cv2.cvtColor(patch, cv2.COLOR_BGR2HSV), (15, 60, 160), (40, 255, 255)
                )
                is_selected = is_selected and (gold.max(axis=1) > 0).mean() > 0.60
            if is_selected:
                selected.append((tile.row, tile.column))
            else:
                # Central top edge excludes portrait corners and selected glow.
                top = image[
                    y - 2 * pad : y + 2 * pad + 1, x + round(cw * 0.2) : x + round(cw * 0.8)
                ]
                if top.shape[0] != 4 * pad + 1:
                    return None
                edges = cv2.Canny(cv2.cvtColor(top, cv2.COLOR_BGR2GRAY), 15, 45)
                strengths = (edges > 0).mean(axis=1)
                hits = np.flatnonzero(strengths > 0.70)
                if len(hits):
                    shift = float(hits[np.argmin(abs(hits - 2 * pad))] - 2 * pad)
                    if abs(shift) > max(2, round(h * 0.004)):
                        return None
                    top_shifts[tile.row].append(shift)
            body = image[
                y + round(ch * 0.18) : y + round(ch * 0.65),
                x + round(cw * 0.2) : x + round(cw * 0.8),
            ]
            signature = tuple(
                int(v) for v in cv2.resize(body, (8, 8), interpolation=cv2.INTER_AREA).ravel()
            )
            tiles.append(replace(tile, signature=signature))
        if any(len(shifts) < 4 for shifts in top_shifts.values()):
            return None
        grid = replace(
            previous,
            tiles=tuple(tiles),
            selected=selected[0] if len(selected) == 1 else None,
            scrollbar=scrollbar,
            bottom_confirmed=bottom,
        )
        self._last_grid = grid
        return grid

    def detect(self, frame, viewport=None, *, context=()):
        import cv2
        import numpy as np

        self._last_grid = None
        if frame is None or frame.ndim != 3 or frame.shape[2] < 3:
            return None
        fh, fw = frame.shape[:2]
        area = EchoViewport.checked(viewport, fw, fh)
        if area.narrow or area.height < 400:
            return None
        h, w = area.height, area.reference_width
        image = frame[area.y : area.y + h, area.x : area.x + round(w), :3]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        pad = max(3, int(h * 0.006 + 0.999))
        rail_x = round(w * 0.642)
        rail = image[round(h * 0.12) : round(h * 0.85), rail_x - pad : rail_x + pad + 1]
        bright = (rail.min(axis=2) > 170).sum(axis=1) >= max(2, pad // 2)
        indices = np.flatnonzero(bright)
        runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1) if len(indices) else []
        knobs = [r for r in runs if 0.01 * h <= len(r) <= 0.045 * h]
        scrollbar = (round(h * 0.12) + float(np.mean(knobs[0]))) / h if len(knobs) == 1 else None
        bottom = len(knobs) == 1 and round(h * 0.12) + int(knobs[0][-1]) >= h * 0.839
        contrast = (gray[:, rail_x - pad * 3].astype(float) + gray[:, rail_x + pad * 3]) / 2 - gray[
            :, rail_x
        ]
        rail_visible = float((contrast[round(h * 0.13) : round(h * 0.83)] > 10).mean()) > 0.15
        edges = cv2.Canny(gray, 15, 45)
        rects = []
        for candidate in (
            cv2.Canny(gray, 30, 90),
            cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)),
        ):
            contours, _ = cv2.findContours(candidate, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            rects.extend(cv2.boundingRect(c) for c in contours)
        rects = [
            (x, y, cw, ch)
            for x, y, cw, ch in rects
            if 0.08 * w < x < 0.635 * w
            and 0.11 * h < y
            and y + ch < 0.85 * h
            and 0.125 * h < cw < 0.155 * h
            and 1.17 < ch / cw < 1.31
        ]

        def clusters(values, tolerance):
            result = []
            for value in sorted(values):
                if result and abs(value - float(np.median(result[-1]))) <= tolerance:
                    result[-1].append(value)
                else:
                    result.append([value])
            return result

        xs = clusters([x + cw / 2 for x, y, cw, ch in rects], h * 0.013)
        xs = [float(np.median(c)) for c in xs]
        cached = self._geometry
        if cached is not None and cached[0] != (area.bounds, tuple(context)):
            cached = None
        if len(xs) != 6:
            if not rects:
                return None
            if cached is None:
                if rail_visible or scrollbar is not None or abs(min(xs) - w * 0.13) > h * 0.02:
                    return None
                xs = [min(xs) + col * h * 0.1633 for col in range(6)]
            else:
                xs = cached[1]
        if cached is not None:
            # Refitting six centres on every selected border also moves the
            # signature crops. Preserve calibrated spacing, but retain measured
            # whole-list translation instead of concealing an external move.
            dx = float(np.median([x - old for x, old in zip(xs, cached[1])]))
            xs = [x + (dx if abs(dx) > h * 0.004 else 0.0) for x in cached[1]]
        pitch_x = float(np.median(np.diff(xs)))
        if max(abs(v - pitch_x) for v in np.diff(xs)) > h * 0.01:
            return None
        ys = clusters([y for x, y, cw, ch in rects], h * 0.012)
        anchors = []
        for cluster in ys:
            center_y = float(np.median(cluster))
            columns = {
                min(range(6), key=lambda i: abs(xs[i] - x - cw / 2))
                for x, y, cw, ch in rects
                if abs(y - center_y) <= h * 0.012
            }
            if len(columns) >= 4:
                anchors.append(center_y)
        if len(anchors) < 2:
            if not rects:
                return None
            if cached is None and (rail_visible or scrollbar is not None):
                return None
            anchors = [float(np.median(ys[0]))]
            pitch_y = cached[2] if cached is not None else h * 0.196
        elif cached is not None:
            pitch_y = cached[2]
        else:
            pitch_y = float(np.median(np.diff(anchors)))
        if not 0.18 * h < pitch_y < 0.22 * h:
            return None
        first_y = anchors[0]
        if cached is not None:
            # Border estimates can differ by row. Re-estimating their pitch
            # accumulates that error at row 3. Measure only the scroll phase
            # against the existing lattice, using all available row anchors.
            reference = cached[5]
            dy = float(
                np.median(
                    [
                        anchor - reference - round((anchor - reference) / pitch_y) * pitch_y
                        for anchor in anchors
                    ]
                )
            )
            first_y = reference + (dy if abs(dy) > h * 0.004 else 0.0)
        while first_y - pitch_y >= 0.12 * h:
            first_y -= pitch_y
        cw = int(round(np.median([r[2] for r in rects]))) if cached is None else cached[3]
        ch = int(round(np.median([r[3] for r in rects]))) if cached is None else cached[4]
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        gold = cv2.inRange(hsv, (15, 60, 160), (40, 255, 255))
        tiles = []
        selected = []
        partial = False
        for row in range(5):
            y = round(first_y + row * pitch_y)
            if y < 0.12 * h or y + ch > 0.845 * h:
                if 0.12 * h <= y < 0.81 * h:
                    for center in xs:
                        x = round(center - cw / 2)
                        strips = [
                            edges[y + pad : round(h * 0.835), edge - pad : edge + pad + 1]
                            for edge in (x, x + cw)
                        ]
                        if all(s.size and (s.max(axis=1) > 0).mean() > 0.55 for s in strips):
                            partial = True
                continue
            for col, center in enumerate(xs):
                x = round(center - cw / 2)

                def edge_fraction(mask, edge_x, margin):
                    strip = mask[
                        y + pad : y + ch - pad, max(0, edge_x - margin) : edge_x + margin + 1
                    ]
                    return float((strip.max(axis=1) > 0).mean()) if strip.size else 0.0

                if min(edge_fraction(edges, x, pad), edge_fraction(edges, x + cw, pad)) < 0.55:
                    continue
                # Ignore portrait, lock, COST and rarity strips for overlap.
                body = image[
                    y + round(ch * 0.18) : y + round(ch * 0.65),
                    x + round(cw * 0.2) : x + round(cw * 0.8),
                ]
                small = cv2.resize(body, (8, 8), interpolation=cv2.INTER_AREA)
                signature = tuple(int(v) for v in small.ravel())
                tiles.append(ScanTile(row, col, (area.x + x, area.y + y, cw, ch), signature))
                if (
                    min(edge_fraction(gold, x, pad * 2), edge_fraction(gold, x + cw, pad * 2))
                    > 0.60
                ):
                    selected.append((row, col))
        if not tiles:
            return None
        rows = sorted({t.row for t in tiles})
        for row in rows:
            columns = [t.column for t in tiles if t.row == row]
            if columns != list(range(len(columns))) or (row != rows[-1] and len(columns) != 6):
                return None  # Missing internal tile is not an empty inventory slot.
        # Local row indices count visible full rows, not hypothetical empty rows
        # above the first measured border. Keep overlap offset arithmetic exact.
        row_map = {row: index for index, row in enumerate(rows)}
        tiles = [replace(t, row=row_map[t.row]) for t in tiles]
        selected = [(row_map[row], col) for row, col in selected]
        self._geometry = ((area.bounds, tuple(context)), tuple(xs), pitch_y, cw, ch, first_y)
        single_page = scrollbar is None and not rail_visible and not partial
        grid = BagGrid(
            tuple(tiles),
            selected[0] if len(selected) == 1 else None,
            scrollbar,
            tuple(context),
            area.bounds,
            bottom,
            single_page,
        )
        self._last_grid = grid
        return grid


def bag_sort_direction(frame, viewport):
    """Read the two arrow strokes, not the circular button/neighboring text.

    Unknown appearance is deliberately not a descending confirmation. The ROI
    follows the captured content height and left-anchored reference width.
    """
    import numpy as np

    area = EchoViewport.checked(viewport, frame.shape[1], frame.shape[0])

    def brightness(x1, y1, x2, y2):
        crop = frame[
            area.y + round(area.height * y1) : area.y + round(area.height * y2),
            area.x + round(area.reference_width * x1) : area.x + round(area.reference_width * x2),
            :3,
        ]
        return float(np.percentile(crop.mean(axis=2), 90)) if crop.size else 0

    up = brightness(0.340, 0.902, 0.347, 0.917)
    down = brightness(0.350, 0.920, 0.357, 0.935)
    if down - up >= 35 and down >= 165 and up < 145:
        return 'descending'
    if up - down >= 35 and up >= 165 and down < 145:
        return 'ascending'
    return 'unknown'
