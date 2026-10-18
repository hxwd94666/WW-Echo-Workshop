# 解析声骸名称套装及装备角色元数据。
"""Same-frame, stat-anchored metadata reads, with original observations retained."""

from dataclasses import dataclass, replace
import json
import math

from echo_workshop.capture.viewport import EchoViewport


@dataclass(frozen=True)
class MetadataPass:
    analysis: object
    boxes: tuple
    original_boxes: tuple
    regions: tuple = ()
    region_boxes: tuple = ()
    issues: tuple = ()
    retained_fields: tuple = ()

    def raw_text(self):
        original = '\n'.join(str(b.name) for b in self.original_boxes)
        if not self.regions:
            return original
        # Both observations survive backup/export/history; no schema migration.
        parts = [original, '[echo-metadata-ocr-v1: original above; scoped observations below]']
        for region, boxes in zip(self.regions, self.region_boxes):
            parts.append(
                json.dumps(
                    dict(
                        region=region,
                        boxes=[
                            dict(
                                x=b.x,
                                y=b.y,
                                width=b.width,
                                height=b.height,
                                text=str(b.name),
                                confidence=getattr(b, 'confidence', None),
                            )
                            for b in boxes
                        ],
                    ),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        return '\n'.join(parts)


def metadata_regions(analysis, width, height):
    """Captured-pixel regions; never infer a layout or read expected fixture data."""
    layout = getattr(analysis, 'layout', '')
    rows = getattr(analysis, 'main_rows', ())
    if layout not in ('bag', 'display') or len(rows) != 2:
        return ()
    try:
        area = EchoViewport.checked(getattr(analysis, 'viewport', ()) or None, width, height)
        first_y = rows[0].y
        if type(first_y) not in (int, float) or not math.isfinite(first_y) or area.narrow:
            return ()
    except (ValueError, AttributeError):
        return ()
    dx, dy = area.offset('right')
    ref, h = area.reference_width, area.height
    ratios = (
        ((0.68, 0.08, 0.99, 0.32), (0.68, 0.77, 0.99, 0.90))
        if layout == 'bag'
        else ((0.76, 0.11, 0.99, 0.215), (0.76, 0.82, 0.99, 0.93))
    )
    regions = []
    for i, (x1, y1, x2, y2) in enumerate(ratios):
        left, top = round(dx + ref * x1), round(dy + h * y1)
        right, bottom = round(dx + ref * x2), round(dy + h * y2)
        if i == 0:
            bottom = min(bottom, math.floor(first_y))
        # The head needs positive space strictly above the stat rectangle.
        if (
            right <= left
            or bottom <= top
            or left < area.x
            or top < area.y
            or right > area.x + area.width
            or bottom > area.y + area.height
        ):
            return ()
        regions.append((left, top, right - left, bottom - top))
    # Reject layouts where even the first main row lies in/after the owner area.
    last = tuple(getattr(analysis, 'sub_rows', ())) or rows
    if getattr(last[-1], 'y', height) + getattr(last[-1], 'height', height) >= regions[1][1]:
        return ()
    return tuple(regions)


def _inside(box, region):
    x, y, w, h = region
    return x <= box.x < x + w and y <= box.y < y + h


def scan_ocr_regions(viewport, width, height):
    """Selected right panel plus sort controls; no grid portraits or center art.

    All coordinates remain in captured pixels, including wide/padded canvases.
    Enhancement/display pages still use the ordinary manual/scoring parser.
    """
    area = EchoViewport.checked(viewport, width, height)
    if area.narrow:
        return area.ocr_regions()
    result = []
    for side, (x1, y1, x2, y2) in (
        ('right', (0.665, 0.08, 0.995, 0.93)),
        ('left', (0.13, 0.51, 0.335, 0.96)),
    ):
        dx, dy = area.offset(side)
        x, y = round(dx + area.reference_width * x1), round(dy + area.height * y1)
        right, bottom = round(dx + area.reference_width * x2), round(dy + area.height * y2)
        result.append((x, y, right - x, bottom - y))
    return tuple(result)


def refine_echo_metadata(
    analysis, boxes, width, height, read_region, analyze, *, complete_only=False
):
    """Read two bounded ROIs on the caller's same frame, preserve stat geometry.

    A local reading is a candidate, not an oracle. Changed observations require
    review; incompatible grouping keeps the original analysis and marks failure.
    IO/OCR exceptions propagate to the task's existing interruption handling.
    """
    original = tuple(boxes)
    regions = metadata_regions(analysis, width, height)
    if not regions:
        return MetadataPass(analysis, original, original)
    if complete_only:
        from echo_workshop.capture.record_metadata import record_from_analysis

        candidate = record_from_analysis(
            analysis, original, '00000000-0000-4000-8000-000000000001', width, height
        )
        # Sonata comes from the icon later. Empty owner can mean unequipped.
        # Missing/ambiguous headers still take the existing scoped fallback.
        if (
            candidate is not None
            and candidate.name
            and candidate.level is not None
            and analysis.observed_cost is not None
            and not candidate.issues
            and not getattr(analysis, 'layout_provisional', False)
        ):
            return MetadataPass(analysis, original, original)
    observations = tuple(tuple(read_region(r)) for r in regions)
    scoped = tuple(b for r, seen in zip(regions, observations) for b in seen if _inside(b, r))
    merged = tuple(b for b in original if not any(_inside(b, r) for r in regions)) + scoped
    refined = analyze(merged)
    invariant = ('main_rows', 'sub_rows', 'layout', 'page_kind', 'viewport', 'layout_provisional')
    if any(getattr(refined, key, None) != getattr(analysis, key, None) for key in invariant):
        return MetadataPass(
            analysis, original, original, regions, observations, ('ocr:metadata_geometry_changed',)
        )
    # Compare candidate fields through the same parser, never a second catalog.
    from echo_workshop.capture.record_metadata import record_from_analysis

    profile = '00000000-0000-4000-8000-000000000001'
    before = record_from_analysis(analysis, original, profile, width, height)
    after = record_from_analysis(refined, merged, profile, width, height)
    issues = []
    if before is None or after is None:
        return MetadataPass(
            analysis, original, original, regions, observations, ('ocr:metadata_geometry_changed',)
        )
    for field in ('name', 'level', 'owner', 'sonata'):
        old, new = getattr(before, field), getattr(after, field)
        if old != new:
            kind = (
                'missing'
                if new in ('', None)
                else ('recovered' if old in ('', None) else 'disagreement')
            )
            issues.append('ocr:metadata_' + kind + ':' + field)
    old, new = analysis.observed_cost, refined.observed_cost
    if old != new:
        kind = 'missing' if new is None else ('recovered' if old is None else 'disagreement')
        issues.append('ocr:metadata_' + kind + ':cost')
    retained = []
    for field in ('name', 'level', 'owner', 'sonata'):
        old, new = getattr(before, field), getattr(after, field)
        if old not in ('', None) and new in ('', None):
            # Preserve an actual original observation, never invent a value;
            # the missing scoped read and its raw evidence still require review.
            retained.append((field, old))
    if analysis.observed_cost is not None and refined.observed_cost is None:
        # Re-use only the original head COST label/digits. Re-analyze with the
        # pinned scoring callback so a different inferred COST cannot mis-score.
        import re

        head = regions[0]
        cost_evidence = tuple(
            b
            for b in original
            if _inside(b, head)
            and (
                re.search(r'\bCOST\b', str(b.name), re.I) or str(b.name).strip() in ('1', '3', '4')
            )
        )
        checked = analyze(merged + cost_evidence)
        if checked.observed_cost == analysis.observed_cost and all(
            getattr(checked, key, None) == getattr(analysis, key, None) for key in invariant
        ):
            refined = checked
            merged += cost_evidence
    if issues:
        refined = replace(
            refined, summary=refined.summary + '\n元数据局部复核：观察差异已保留，收录需核对'
        )
    return MetadataPass(
        refined, merged, original, regions, observations, tuple(issues), tuple(retained)
    )


def record_with_metadata(
    observation, profile, width, height, *, icon=None, characters=None, frame=None
):
    from echo_workshop.capture.record_metadata import record_from_analysis

    record = record_from_analysis(observation.analysis, observation.boxes, profile, width, height)
    if record is None:
        return None
    record = replace(
        record,
        **dict(observation.retained_fields),
        raw_text=observation.raw_text(),
        issues=tuple(dict.fromkeys(record.issues + observation.issues)),
    )
    if icon is not None or characters is not None:
        record = attach_visual_metadata(
            record,
            observation.analysis,
            observation.boxes,
            width,
            height,
            icon=icon,
            characters=characters,
            frame=frame,
        )
    from echo_workshop.capture.game_status import attach_game_status

    return attach_game_status(record, observation.analysis, observation.boxes, width, height, frame)


def attach_visual_metadata(
    record, analysis, boxes, width, height, *, icon=None, characters=None, frame=None
):
    """Same-frame observations only. No role guessing or prior-item inheritance."""
    from echo_workshop.core.models import utc_now
    from echo_workshop.application.text import simplify_echo_text

    issues, evidence, changes = list(record.issues), {}, {}
    layout = getattr(analysis, 'layout', '')
    if layout not in ('bag', 'display'):
        return record
    if icon is not None:
        evidence['icon'] = dict(
            state=icon.state,
            id=icon.ident,
            name=icon.name,
            score=icon.score,
            runner_up=icon.runner_up,
            bounds=icon.bounds,
            version=icon.version,
        )
        if icon.state == 'matched':
            if record.sonata and simplify_echo_text(record.sonata) != icon.name:
                issues.append('ocr:sonata_icon_conflict')
            elif any(issue.startswith('ocr:conflicting_sonata') for issue in issues):
                # Conflicting written sets stay visible even if one icon wins.
                issues.append('ocr:sonata_icon_conflict')
            else:
                changes['sonata'] = icon.name
                # A matching icon independently validates this observed set
                # heading. Other missing/conflicting fields remain untouched.
                if record.sonata:
                    issues = [i for i in issues if i != 'ocr:sonata_needs_review']
    if characters is not None:
        observed = characters.resolve(record.owner)
        state, ident = observed.state, observed.character_id
        if state == 'unobserved':
            state = (
                'unequipped'
                if _observed_empty_owner(analysis, boxes, width, height, frame)
                else 'unknown'
            )
        if 'ocr:conflicting_owner' in issues or any(
            i.startswith('ocr:metadata_disagreement:owner') for i in issues
        ):
            state, ident = 'unknown', ''
        changes.update(owner_state=state, owner_character_id=ident)
        if record.owner or state == 'unequipped':
            changes['owner_updated_at'] = utc_now()
        evidence['owner'] = dict(
            state=state,
            raw_name=record.owner,
            name=observed.name,
            candidates=observed.character_ids,
        )
    raw = (
        record.raw_text
        + '\n[echo-visual-metadata-v1] '
        + json.dumps(evidence, ensure_ascii=False, allow_nan=False)
    )
    return replace(record, **changes, raw_text=raw, issues=tuple(dict.fromkeys(issues)))


def _observed_empty_owner(analysis, boxes, width, height, frame):
    """Negative evidence needs a visible footer, not an empty OCR result alone.

    Display's explicit Equip action or a smooth empty avatar slot with matching
    adjacent background can establish a candidate. CaptureSession still needs
    multiple fresh consistent frames before saving this state.
    """
    from echo_workshop.application.text import simplify_echo_text

    layout = getattr(analysis, 'layout', '')
    if getattr(analysis, 'layout_provisional', False):
        return False
    area = EchoViewport.checked(getattr(analysis, 'viewport', ()) or None, width, height)
    if area.narrow:
        return False
    dx, dy = area.offset('right')
    ref, h = area.reference_width, area.height
    viewed = area.boxes(boxes, 'right')
    footer = [
        simplify_echo_text(b.name).strip()
        for b in viewed
        if ref * 0.77 <= b.x <= ref * 0.99 and h * 0.90 <= b.y <= h * 0.98
    ]
    if layout == 'display' and '装配' in footer and '替换' not in footer:
        return True
    if not any(value in ('培养', '替换', '装配') for value in footer) or frame is None:
        return False
    import numpy as np

    ratios = (0.687, 0.807, 0.710, 0.842) if layout == 'bag' else (0.785, 0.870, 0.809, 0.908)
    x1, y1, x2, y2 = (
        round(dx + ref * ratios[0]),
        round(dy + h * ratios[1]),
        round(dx + ref * ratios[2]),
        round(dy + h * ratios[3]),
    )
    if not (area.x <= x1 < x2 <= area.x + area.width and area.y <= y1 < y2 <= area.y + area.height):
        return False
    slot = frame[y1:y2, x1:x2, :3].astype(float)
    neighbor = frame[y1:y2, x2 : min(area.x + area.width, x2 + (x2 - x1)), :3].astype(float)
    if not slot.size or not neighbor.size:
        return False
    # A present portrait is textured or differs from the panel background;
    # uncertain backgrounds remain unknown instead of clearing prior ownership.
    return bool(
        np.max(slot.std(axis=(0, 1))) < 4
        and np.max(neighbor.std(axis=(0, 1))) < 4
        and np.max(np.abs(slot.mean(axis=(0, 1)) - neighbor.mean(axis=(0, 1)))) < 6
    )
