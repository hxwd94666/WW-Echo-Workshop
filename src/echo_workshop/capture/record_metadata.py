# 从选中面板的OCR文本生成声骸记录与元数据候选。
import re

from echo_workshop.core.models import EchoRecord, StatValue, utc_now
from echo_workshop.application.text import simplify_echo_text
from echo_workshop.capture.viewport import EchoViewport


def record_from_analysis(analysis, boxes, profile_id, width, height):
    if not analysis.main_rows or not analysis.page_kind:
        return None
    if getattr(analysis, 'layout', '') not in ('', 'bag', 'display', 'enhancement'):
        return None
    raw_text = '\n'.join(str(box.name) for box in boxes)
    normalized = [(box, simplify_echo_text(box.name).strip()) for box in boxes]
    bounds = getattr(analysis, 'viewport', ())
    if bounds:
        area = EchoViewport.checked(bounds, width, height)
        # Metadata uses the same selected edge/content origin as stat rows;
        # raw_text coordinates remain in the captured canvas; saved evidence
        # carries its separate selected-panel crop bounds.
        side = 'left' if analysis.layout == 'enhancement' else 'right'
        viewed = area.boxes(boxes, side)
        normalized = [(b, simplify_echo_text(b.name).strip()) for b in viewed]
        width, height = area.reference_width, area.height
    text = '\n'.join(value for _, value in normalized)
    name_match = re.search(r'(?:声骸名称|Echo Name)\s*[:：]\s*([^\n]+)', text, re.I)
    set_match = re.search(r'(?:套装|合鸣效果|Sonata Effect)\s*[:：]\s*([^\n]+)', text, re.I)
    level_match = re.search(r'(?:等级|\bLv\.?|\bLevel)\s*[:：]?\s*(\d{1,2})\b', text, re.I)
    # Modern analyses distinguish observed detail COST from legacy overlay
    # estimates. Preserve compatibility for callers without panel metadata.
    explicit_cost = (
        analysis.observed_cost is not None
        if hasattr(analysis, 'observed_cost')
        else any(re.search(r'\bCOST\s*[:：]?\s*([134])\b', value, re.I) for _, value in normalized)
    )
    # Header-region fallbacks are candidates, not facts. Without a maintained
    # name/set catalog they need screenshot confirmation before recommendation.
    name = name_match[1].strip() if name_match else ''
    issues = []
    layout = getattr(analysis, 'layout', '')
    regions = {
        'bag': (0.68, 0.99, 0.09, 0.25),
        'display': (0.76, 0.99, 0.12, 0.16),
        'enhancement': (0.09, 0.38, 0.11, 0.23),
    }
    header_boxes = []
    if layout in regions:
        x1, x2, y1, y2 = regions[layout]
        header_boxes = [
            (box, value)
            for box, value in normalized
            if width * x1 <= box.x <= width * x2 and height * y1 <= box.y <= height * y2
        ]
    if not name:
        candidates = (
            header_boxes
            if layout
            else [
                (box, value)
                for box, value in normalized
                if box.x >= width * 0.76 and box.y < height * 0.16
            ]
        )
        headers = []
        for box, value in candidates:
            # +25 on the same OCR box is a level suffix, not part of a name.
            value = re.sub(r'\s*[+＋]\d{1,2}.*$', '', value).strip()
            if (
                not value
                or value == '声骸'
                or re.search(
                    r'COST|等级|Lv|\d|声骸(?:技能|强化|名称)|强化|调谐|简述|持有数量|MAX',
                    value,
                    re.I,
                )
            ):
                continue
            chinese = re.match(r'[\u4e00-\u9fff·・]+', value)
            candidate = chinese[0] if chinese else value
            if len(candidate) > 1 and any(char.isalpha() for char in candidate):
                headers.append(candidate)
        if len(headers) == 1:
            name = headers[0]
            issues.append('ocr:header_name_needs_review')
    sonata = set_match[1].strip() if set_match else ''
    level = int(level_match[1]) if level_match and 0 <= int(level_match[1]) <= 25 else None
    if level is None and header_boxes:
        levels = set()
        for _, value in header_boxes:
            if '强化至' in value:
                continue
            match = re.search(r'[+＋](\d{1,2})(?!\d)', value)
            if match and 0 <= int(match[1]) <= 25:
                levels.add(int(match[1]))
        if len(levels) == 1:
            level = levels.pop()
        elif len(levels) > 1:
            issues.append('ocr:conflicting_header_level')
    owner_match = re.search(r'([^\s\n]+?)装配中', text)
    owner = owner_match[1] if owner_match else ''
    panel_text = text
    if layout in regions:
        # Modern panel analyses never inherit a global explicit label from the
        # grid, skill prose or the other side. Legacy callers retain their old
        # labelled-text contract until they supply selected-panel geometry.
        name, sonata, level, owner, issues, panel_text = _panel_metadata(
            normalized, header_boxes, layout, width, height
        )
    if not explicit_cost:
        # Split COST labels/numbers are supported by the score overlay, but
        # inventory must not quietly trust its nearest-value COST heuristic.
        issues.append('ocr:cost_needs_review')
    if getattr(analysis, 'layout_provisional', False):
        issues.append('ocr:layout_needs_review')
    if any(
        str(row.value_text).lstrip().startswith(('.', '·', '。'))
        for row in analysis.main_rows + analysis.sub_rows
    ):
        issues.append('ocr:ambiguous_value')
    main = tuple(StatValue(row.stat_name, row.value, row.value_text) for row in analysis.main_rows)
    sub = tuple(StatValue(row.stat_name, row.value, row.value_text) for row in analysis.sub_rows)
    # Existing overlay truncates at 7 rows. A declared +25/all-tuned panel with
    # missing rolls must not look like a deliberately partially tuned item.
    if '已完成全部调谐' in panel_text and len(sub) != 5:
        issues.append('ocr:missing_tuned_rows')
    return EchoRecord(
        profile_id,
        analysis.cost,
        main,
        sub,
        name=name,
        sonata=sonata,
        level=level,
        raw_text=raw_text,
        issues=tuple(issues),
        owner=owner,
        owner_updated_at=utc_now() if owner else '',
    )


def _header_name_candidate(text):
    """Selected-title text only; digits can belong to an Echo (岩蛛S4型).

    A one-character Han title (角) is valid, unlike Z/C key hints. Do not
    truncate alphanumeric names or turn numeric/UI headers into candidates.
    """
    value = re.sub(r'\s*[+＋]\d{1,3}.*$', '', text).strip()
    if (
        not value
        or value == '声骸'
        or re.search(
            r'COST|等级|\bLv\b|\bLevel\b|声骸(?:技能|强化|名称)|强化|调谐|简述|持有数量|MAX|套装|合鸣效果|Sonata Effect',
            value,
            re.I,
        )
    ):
        return ''
    if not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9·・\s]+', value):
        return ''
    if re.search(r'[\u4e00-\u9fff]', value):
        return value
    return (
        value
        if len(value) > 1 and any(c.isalpha() for c in value) and value not in ('ZC', 'CZ')
        else ''
    )


def _panel_metadata(normalized, headers, layout, width, height):
    """Read candidates only within the selected panel; conflicts stay unknown."""
    regions = {
        'bag': (0.68, 0.99, 0.09, 0.90),
        'display': (0.76, 0.99, 0.12, 0.92),
        'enhancement': (0.09, 0.38, 0.11, 0.90),
    }
    x1, x2, y1, y2 = regions[layout]
    panel = [
        (b, t)
        for b, t in normalized
        if width * x1 <= b.x <= width * x2 and height * y1 <= b.y <= height * y2
    ]
    issues = []

    def single(values, field, empty=''):
        values = set(values)
        if len(values) > 1:
            issues.append('ocr:conflicting_' + field)
            return empty
        return next(iter(values), empty)

    names, explicit_names, levels = [], [], []
    invalid_level = False
    for _, text in headers:
        explicit = re.fullmatch(r'(?:声骸名称|Echo Name)\s*[:：]\s*(.+)', text, re.I)
        if explicit:
            explicit_names.append(explicit[1].strip())
            continue
        for match in re.finditer(
            r'(?:[+＋]|等级\s*[:：]?\s*|\bLv\.?\s*[:：]?\s*|\bLevel\s*[:：]?\s*)(\d{1,3})(?!\d)',
            text,
            re.I,
        ):
            value = int(match[1])
            if 0 <= value <= 25:
                levels.append(value)
            else:
                invalid_level = True
        candidate = _header_name_candidate(text)
        if candidate:
            names.append(candidate)
    name = single(names + explicit_names, 'header_name')
    if name and not explicit_names and layout == 'enhancement':
        issues.append('ocr:header_name_needs_review')
    level = single(levels, 'header_level', None)
    if invalid_level:
        level = None
        issues.append('ocr:invalid_header_level')

    explicit_sets = [
        match[1].strip()
        for _, text in panel
        if (match := re.fullmatch(r'(?:套装|合鸣效果|Sonata Effect)\s*[:：]\s*(.+)', text, re.I))
    ]
    sonata = single(explicit_sets, 'sonata')
    if not explicit_sets and layout == 'display':
        # Display pages show effect names with (n/2), (n/3), or (n/5).
        # Require the heading AND a paired count, never take a prose sentence
        # or guess a set from the Echo's name/icon/another screenshot.
        headings = [b for b, t in panel if t in ('合鸣效果', 'Sonata Effect')]
        if len(headings) == 1:
            heading = headings[0]
            candidates = []
            for box, text in panel:
                if not (heading.y + heading.height * 0.7 <= box.y <= heading.y + height * 0.16):
                    continue
                match = re.fullmatch(
                    r'([\u4e00-\u9fff·・]{2,32})\s*(?:[（(]\s*\d{1,2}/\d{1,2}\s*[)）])?', text
                )
                if not match:
                    continue
                inline_count = re.search(r'[（(]\s*\d{1,2}/\d{1,2}\s*[)）]', text)
                paired_count = any(
                    len(t) <= 16
                    and re.search(r'[（(]\s*\d{1,2}/\d{1,2}\s*[)）]', t)
                    and other.x >= box.x + box.width * 0.5
                    and abs(other.y + other.height / 2 - box.y - box.height / 2)
                    <= max(9, min(other.height, box.height) * 0.7)
                    for other, t in panel
                )
                if inline_count or paired_count:
                    candidates.append(match[1])
            sonata = single(candidates, 'sonata')
            if sonata:
                issues.append('ocr:sonata_needs_review')
        elif headings:
            issues.append('ocr:conflicting_sonata_heading')

    owners = []
    owner_y = {'bag': (0.77, 0.88), 'display': (0.82, 0.92)}.get(layout)
    if owner_y:
        footer = sorted(
            (
                (box, text)
                for box, text in panel
                if height * owner_y[0] <= box.y <= height * owner_y[1]
            ),
            key=lambda pair: pair[0].x,
        )
        candidates = []
        for i, (box, text) in enumerate(footer):
            joined = text
            candidates.append(joined)
            right = box.x + box.width
            for other, piece in footer[i + 1 :]:
                if (
                    abs(other.y + other.height / 2 - box.y - box.height / 2)
                    > max(box.height, other.height) * 0.6
                    or not -max(box.height, other.height)
                    <= other.x - right
                    <= max(box.height, other.height) * 2
                ):
                    break
                joined += piece
                right = other.x + other.width
                candidates.append(joined)
        matches = [
            re.fullmatch(r'([^:：\n]{1,64}?)装配中', re.sub(r'\s+', '', text))
            for text in candidates
        ]
        owners = [match[1] for match in matches if match]
        # Suffix fragments of the same observed name are not conflicting owners.
        owners = [
            name
            for name in owners
            if not any(name != other and other.endswith(name) for other in owners)
        ]
    owner = single(owners, 'owner')
    return name, sonata, level, owner, issues, '\n'.join(t for _, t in panel)
