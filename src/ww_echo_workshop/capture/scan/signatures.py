# 比较可见图块与扫描暂停位置证据。


def _signature_distance(a, b):
    if not a or len(a) != len(b):
        return float('inf')
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _near_signature(a, b):
    return _signature_distance(a, b) <= 9


def _same_row(a, b, *, highlighted=()):
    return 0 < len(a) == len(b) and all(
        x.column == y.column
        and (x.column in highlighted or _near_signature(x.signature, y.signature))
        for x, y in zip(a, b)
    )


def _matching_rows(a, b, shift=0, *, target=None, stationary_noise=False):
    """Compare content anchors, excluding only known selection/hover glow.

    No list position is inferred from the excluded cells. At least three other
    cells must remain as strict image anchors, including for a scroll overlap.
    Small lists without enough anchors retain the original strict comparison.
    """
    old, new = a.rows(), b.rows()
    shared = min(len(old) - shift, len(new))
    if shared <= 0:
        return False
    old_highlights = {p for p in (a.selected, target) if p is not None}
    new_highlights = {p for p in (b.selected, target) if p is not None}
    pairs = [(old[shift + i], new[i]) for i in range(shared)]
    exclusions = [
        tuple(
            x.column
            for x, y in zip(left, right)
            if (x.row, x.column) in old_highlights or (y.row, y.column) in new_highlights
        )
        for left, right in pairs
    ]
    if sum(len(left) - len(skip) for (left, _), skip in zip(pairs, exclusions)) < 3:
        exclusions = [() for _ in pairs]
    if not stationary_noise:
        return all(
            _same_row(left, right, highlighted=skip)
            for (left, right), skip in zip(pairs, exclusions)
        )
    # Only a geometrically stationary page can use this consensus. Refitting
    # borders and game lighting slightly change thumbnail pixels, including
    # unselected cards. Do not let a single marginal RGB mismatch veto an
    # otherwise unchanged page. Keep >=87.5% strict anchors; every outlier must
    # still be close (<=18), with >=half of each usable row remaining strict.
    # This stationary consensus does not choose any scroll displacement.
    outliers, total = 0, 0
    for (left, right), skip in zip(pairs, exclusions):
        if (
            not left
            or len(left) != len(right)
            or any(x.column != y.column for x, y in zip(left, right))
        ):
            return False
        distances = [
            _signature_distance(x.signature, y.signature)
            for x, y in zip(left, right)
            if x.column not in skip
        ]
        if any(d > 18 for d in distances):
            return False
        strict = sum(d <= 9 for d in distances)
        if strict * 2 < len(distances):
            return False
        total += len(distances)
        outliers += len(distances) - strict
    return outliers <= total // 8


def _same_geometry(a, b):
    # Border/glow measurement jitters by a few pixels with selection. This
    # tolerance is much smaller than one wheel step at the same resolution.
    tolerance = max(2, round(a.viewport[3] * 0.004)) if len(a.viewport) == 4 else 2
    return len(a.tiles) == len(b.tiles) and all(
        (x.row, x.column) == (y.row, y.column)
        and max(abs(v - w) for v, w in zip(x.bounds, y.bounds)) <= tolerance
        for x, y in zip(a.tiles, b.tiles)
    )


def _scroll_overlap(a, b, shift):
    """Whole-row alignment plus bounded RGB error, not a single loose cell.

    Thumbnails move across the translucent list background, so exact per-cell
    <=9 brightness is not invariant under scrolling. Keep every usable anchor
    <=18, the mean <=13, and measure candidate separation in the caller.
    """
    old, new = a.rows(), b.rows()
    shared = min(len(old) - shift, len(new))
    pairs = [(old[shift + i], new[i]) for i in range(max(0, shared))]
    highlights = []
    distances = []
    translations = []
    geometry = True
    tolerance = max(3, round(a.viewport[3] * 0.006)) if len(a.viewport) == 4 else 3
    for left, right in pairs:
        if (
            not left
            or len(left) != len(right)
            or any(x.column != y.column for x, y in zip(left, right))
        ):
            return dict(
                mean=float('inf'), worst=float('inf'), anchors=0, geometry=False, excluded=[]
            )
        for x, y in zip(left, right):
            translations.append(x.bounds[1] - y.bounds[1])
            geometry &= all(abs(x.bounds[i] - y.bounds[i]) <= tolerance for i in (0, 2, 3))
            if (x.row, x.column) == a.selected or (y.row, y.column) == b.selected:
                highlights.append((x.row, x.column, y.row, y.column))
            else:
                distances.append(_signature_distance(x.signature, y.signature))
    if translations:
        geometry &= max(translations) - min(translations) <= tolerance
    if shift and len(old) > 1 and translations:
        pitch = old[1][0].bounds[1] - old[0][0].bounds[1]
        geometry &= (
            pitch > 0
            and (shift - 0.5) * pitch
            < sum(translations) / len(translations)
            < (shift + 0.5) * pitch
        )
    mean = sum(distances) / len(distances) if distances else float('inf')
    return dict(
        mean=mean,
        worst=max(distances, default=float('inf')),
        anchors=len(distances),
        geometry=bool(geometry),
        excluded=highlights,
        outliers=sum(d > 18 for d in distances),
    )


def _same_grid(a, b):
    return (
        a.context == b.context
        and a.viewport == b.viewport
        and len(a.rows()) == len(b.rows())
        and _same_geometry(a, b)
        and _matching_rows(a, b, stationary_noise=True)
        and a.selected == b.selected
        and (
            (a.scrollbar is None and b.scrollbar is None)
            or (
                a.scrollbar is not None
                and b.scrollbar is not None
                and abs(a.scrollbar - b.scrollbar) < 0.003
            )
        )
    )
