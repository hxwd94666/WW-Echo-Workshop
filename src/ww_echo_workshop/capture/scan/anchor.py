# 检测声骸卡片锚点和滚动位移。
"""Read the last selected card's bottom edge, including a clipped card.

Only captured pixels and the current six-column lattice are used. No thumbnail
similarity, item names or wheel counts establish a row identity here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldBoundary:
    column: int
    row: int  # -1: selected card is just above the first complete row.
    bottom: int
    x: int
    width: int

    def agrees(self, other, height):
        return (
            other is not None
            and (self.column, self.row) == (other.column, other.row)
            and abs(self.bottom - other.bottom) <= max(2, round(height * 0.004))
            and abs(self.x - other.x) <= max(2, round(height * 0.004))
        )


def gold_boundary(frame, grid, anchor):
    """Find one bottom border at the frozen column and a measured row boundary.

    Rarity separators are inside the card, not at its bottom. A wide gold line
    alone is insufficient: both corner regions, phase and column must agree.
    The selected card may be clipped; it never becomes a scan candidate.
    """
    import cv2
    import numpy as np

    if grid is None or not grid.tiles or anchor is None or len(grid.viewport) != 4:
        return None
    vx, vy, vw, vh = grid.viewport
    rows = grid.rows()
    if len(rows) < 2 or any(len(row) != 6 for row in rows[:-1]):
        return None
    first = next((t for t in rows[0] if t.column == anchor.column), None)
    if first is None:
        return None
    x, first_y, width, height = first.bounds
    pitch = rows[1][0].bounds[1] - rows[0][0].bounds[1]
    tolerance = max(3, round(vh * 0.008))
    if (
        not 0.18 * vh < pitch < 0.22 * vh
        or abs(x - anchor.bounds[0]) > tolerance
        or abs(width - anchor.bounds[2]) > tolerance
    ):
        return None
    if grid.selected is not None and grid.selected[1] != anchor.column:
        return None
    pad = max(3, round(vh * 0.006))
    x0 = max(vx, x - pad * 2)
    x1 = min(vx + vw, x + width + pad * 2 + 1)
    y0 = max(vy, vy + round(vh * 0.095))
    y1 = min(vy + vh, vy + round(vh * 0.845))
    image = frame[y0:y1, x0:x1, :3]
    if image.size == 0:
        return None
    # Clipped anti-aliased bottoms in actual captures have V~133..149/S~51,
    # much dimmer than full side borders. Row phase/width/corners below remain
    # mandatory; this colour range alone never establishes an anchor.
    mask = cv2.inRange(cv2.cvtColor(image, cv2.COLOR_BGR2HSV), (15, 30, 115), (45, 255, 255)) > 0
    inner = mask[:, x - x0 + round(width * 0.12) : x - x0 + round(width * 0.88)]
    if inner.shape[1] < width * 0.7:
        return None
    strengths = inner.mean(axis=1)
    hits = np.flatnonzero(strengths >= 0.65)
    bands = np.split(hits, np.flatnonzero(np.diff(hits) > 1) + 1) if len(hits) else []
    found = []
    for band in bands:
        if not len(band) or len(band) > pad * 3:
            continue
        bottom = y0 + int(round(float(np.median(band))))
        row = round((bottom - first_y - height) / pitch)
        if (
            row < -1
            or row >= len(rows)
            or abs(bottom - (first_y + row * pitch + height)) > tolerance
        ):
            continue
        if grid.selected is not None and grid.selected != (row, anchor.column):
            continue
        top = max(0, int(band[0]) - pad * 2)
        end = min(mask.shape[0], int(band[-1]) + pad + 1)
        corners = [
            mask[top:end, max(0, edge - x0 - pad * 2) : min(mask.shape[1], edge - x0 + pad * 2 + 1)]
            for edge in (x, x + width)
        ]
        if not all(c.size and c.any() for c in corners):
            continue
        found.append(GoldBoundary(anchor.column, row, bottom, x, width))
    return found[0] if len(found) == 1 else None
