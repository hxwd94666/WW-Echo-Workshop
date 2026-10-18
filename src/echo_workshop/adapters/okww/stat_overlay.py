# 向宿主浮层绘制声骸评分和词条贡献。
"""OCR-row grouping and native painting for the Echo stat debug overlay."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from types import SimpleNamespace

from echo_workshop.application.text import simplify_echo_text
from echo_workshop.ui.widgets.rating_style import rating_colour
from echo_workshop.capture.viewport import EchoViewport
from echo_workshop.adapters.okww.score_painter import EchoStatBoxPainter  # noqa: F401


ECHO_STAT_PAINTER_KEY = 'echo-stat-boxes'
TIER_TEXT_COLOR = (80, 185, 255)
LOWEST_TIER_TEXT_COLOR = (80, 235, 130)
HIGHEST_TIER_TEXT_COLOR = (255, 75, 75)
_STAT_TEXT = re.compile(
    r'攻击|生命|防御|暴击|共鸣效率|伤害加成|治疗效果|ATK|HP|DEF|Crit|Energy|DMG|Heal',
    re.IGNORECASE,
)
_VALUE_TEXT = re.compile(r'^\s*(?:[+＋]?\d+(?:[.,]\d+)?|[.·。]\d{2,4})\s*[%％]?\s*$')


@dataclass(frozen=True)
class StatRectangle:
    x: int
    y: int
    width: int
    height: int
    color: tuple[int, int, int]
    tier_x: int = 0
    tier_y: int = 0


@dataclass(frozen=True)
class RecognizedStatRow:
    x: int
    y: int
    width: int
    height: int
    stat_name: str
    value: float
    value_text: str
    tier_x: int
    tier_y: int

    def rectangle(self, color):
        return StatRectangle(
            self.x, self.y, self.width, self.height, color, self.tier_x, self.tier_y
        )


@dataclass(frozen=True)
class EchoStatAnalysis:
    rectangles: tuple[StatRectangle, ...]
    row_scores: tuple[float, ...]
    summary: str
    tier_labels: tuple[str, ...] = ()
    tier_colors: tuple[tuple[int, int, int], ...] = ()
    selected_template: str = ''
    main_rows: tuple[RecognizedStatRow, ...] = ()
    sub_rows: tuple[RecognizedStatRow, ...] = ()
    cost: int = 0
    page_kind: str = ''
    selected_ids: tuple[str, ...] = ()
    layout: str = ''
    observed_cost: int | None = None
    viewport: tuple[int, ...] = ()
    layout_provisional: bool = False
    comparison: bool = False
    current_score: float | None = None
    potential_score: float | None = None
    score_text: str = ''
    potential_text: str = ''
    rating_color: tuple[int, int, int] | None = None


def _rating_rgb(rating):
    if rating is None:
        return None
    value = rating_colour(rating, True)
    if not value.startswith('#'):
        return None
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def analyze_echo_stats(
    ocr_boxes,
    screen_width,
    screen_height,
    template_name,
    auto_match=False,
    template_resolver=None,
    viewport=None,
    comparison_side='right',
    *,
    observation_service,
    weight_version,
    cancel_event=None,
    layout_hint='',
    comparison_hint=False,
    cost_lookup=None,
):
    """Recognize one Echo panel and calculate its row and total scores."""
    if not screen_width or not screen_height:
        return EchoStatAnalysis((), (), '')
    try:
        area = EchoViewport.checked(viewport, screen_width, screen_height)
    except ValueError:
        return EchoStatAnalysis((), (), '声骸布局待确认：内容区域越界或无效')
    if area.narrow:
        return EchoStatAnalysis((), (), '声骸布局待确认：内容区域过窄，等待有效页面')
    provisional = area.wide or area.bounds != (0, 0, screen_width, screen_height)
    screen_width, screen_height = area.reference_width, area.height

    # Keep the OCR geometry unchanged. Only labels are normalized; OKWW's
    # auto_simplify depends on the application locale, not the game's locale.
    ocr_boxes = [
        SimpleNamespace(
            x=box.x,
            y=box.y,
            width=box.width,
            height=box.height,
            name=simplify_echo_text(box.name),
        )
        for box in ocr_boxes
    ]
    ocr_boxes = [
        b
        for b in ocr_boxes
        if area.x <= b.x < area.x + area.width and area.y <= b.y < area.y + area.height
    ]
    left_boxes, right_boxes = area.boxes(ocr_boxes, 'left'), area.boxes(ocr_boxes, 'right')

    left_rows = _find_ocr_rows(
        left_boxes,
        screen_width * 0.09,
        screen_width * 0.38,
        screen_height * 0.20,
        screen_height * 0.54,
    )
    # Comparison has two independent stat columns. Never let values, COST or
    # owners from the neighbouring column leak into the selected right panel.
    headings = [
        b
        for b in right_boxes
        if str(b.name).strip() in ('声骸技能', 'Echo Skill')
        and screen_width * 0.50 <= b.x <= screen_width * 0.99
        and screen_height * 0.18 <= b.y <= screen_height * 0.82
    ]
    page_text = ' '.join(str(box.name) for box in ocr_boxes)
    bag_markers = any(
        marker in page_text for marker in ('声骸管理方案', '等级顺序', '批量管理')
    )
    display_markers = any(
        marker in page_text for marker in ('合鸣筛选', '未装备优先', '声骸推荐', '简述')
    )
    comparison_detected = (
        any(b.x < screen_width * 0.77 for b in headings)
        and any(b.x >= screen_width * 0.77 for b in headings)
    ) or (area.wide and len(left_rows) >= 2 and len(headings) >= 1)
    comparison_detected = comparison_detected and not bag_markers
    comparison = comparison_detected or comparison_hint
    if comparison_side == 'left' and not comparison:
        return EchoStatAnalysis((), (), '')
    min_x, max_x = (0.68, 0.99)
    selection_boxes = ocr_boxes
    if comparison:
        min_x, max_x = (0.52, 0.775) if comparison_side == 'left' else (0.775, 0.99)
        dx, _ = area.offset('right')
        # Both columns use the right/selected echo's role selection. Scores
        # under two different owners' weights would not be a valid delta.
        selection_boxes = [b for b in ocr_boxes if b.x >= dx + screen_width * 0.775]
        right_boxes = [b for b in right_boxes if min_x * screen_width <= b.x < max_x * screen_width]
    skill_headings = [b for b in headings if min_x * screen_width <= b.x < max_x * screen_width]
    right_end = min((b.y for b in skill_headings), default=screen_height * 0.47)
    right_rows = _find_ocr_rows(
        right_boxes,
        screen_width * min_x,
        screen_width * max_x,
        screen_height * 0.18,
        min(right_end, screen_height * 0.75),
    )
    screen_text = ' '.join(str(box.name) for box in ocr_boxes)
    is_tuning_page = any(
        marker in screen_text
        for marker in (
            '声骸强化',
            '强化并调谐',
            '已完成全部调谐',
            'Echo Enhancement',
        )
    )
    is_single_echo_page = any(
        marker in screen_text
        for marker in (
            '声骸技能',
            '合鸣效果',
            'Echo Skill',
            'Sonata Effect',
        )
    )

    # Left-side stat rows are accepted only on the tuning page.  This excludes
    # the Resonator Attribute Details page and the initial Echo summary page,
    # both of which also contain six ordinary stat rows in the same area.
    hint_tuning = layout_hint in ('enhancement', 'tuning')
    hint_single = layout_hint in ('single', 'display')
    hint_bag = layout_hint == 'bag'
    if (
        (is_tuning_page or hint_tuning)
        and len(left_rows) >= 2
        and skill_headings
        and len(right_rows) >= 2
    ):
        return EchoStatAnalysis((), (), '声骸布局待确认：左右详情混合，等待新帧')
    if (is_tuning_page or hint_tuning) and len(left_rows) >= 2:
        rows = left_rows
    elif (is_single_echo_page or hint_single or hint_bag) and len(right_rows) >= 2:
        rows = right_rows
        # A tuning marker elsewhere must not relabel a selected right panel,
        # discard its observed COST or switch its metadata to the left side.
        is_tuning_page = False
    else:
        summary = '声骸布局待确认：扩展画布未找到边缘锚点' if provisional else ''
        return EchoStatAnalysis((), (), summary)

    rows = rows[:7]
    main_rows, sub_rows = rows[:2], rows[2:]
    layout = (
        'enhancement'
        if (is_tuning_page or hint_tuning)
        else 'bag'
        if (
            hint_bag
            or (
                not hint_single
                and not display_markers
                and (bag_markers or main_rows[0].y > screen_height * 0.35)
            )
        )
        else 'display'
    )
    # Restrict observed COST to the selected detail header. The display's
    # left budget (12/12, 13/12), grid COST icons and other panels are excluded.
    cost_boxes = (
        []
        if is_tuning_page
        else [
            box
            for box in right_boxes
            if screen_width * min_x <= box.x <= screen_width * 0.99
            and screen_height * 0.08 <= box.y < main_rows[0].y
        ]
    )
    observed_cost = _observed_cost(cost_boxes)
    if observed_cost is None and cost_lookup is not None:
        observed_cost = cost_lookup(tuple((row.stat_name, row.value) for row in main_rows))
    dx, dy = area.offset('left' if is_tuning_page else 'right')

    def captured(row):
        return replace(
            row,
            x=round(row.x + dx),
            y=round(row.y + dy),
            tier_x=round(row.tier_x + dx),
            tier_y=round(row.tier_y + dy),
        )

    main_rows, sub_rows = [captured(r) for r in main_rows], [captured(r) for r in sub_rows]
    live = None
    error_summary = ''
    if template_resolver is not None:
        try:
            live = template_resolver(selection_boxes, 'tuning' if is_tuning_page else 'single')
            template_name = live.label
        except ValueError as error:
            error_summary = '评分模版需重新选择：' + str(error)
    if template_resolver is None:
        raise ValueError('观测评分需要明确的冻结角色/模版')
    evidence = observation_service.evaluate(
        main_rows,
        sub_rows,
        selections=((live.ids[0], live.ids[1], live.template),) if live is not None else (),
        weight_version=weight_version,
        cost=observed_cost,
        cancel_event=cancel_event,
    )
    cost = evidence.cost
    score = evidence.score_for(live.ids) if live is not None else None
    rectangles = tuple(row.rectangle((255, 0, 0)) for row in main_rows)
    rectangles += tuple(row.rectangle((255, 255, 255)) for row in sub_rows)
    if score is None:
        return EchoStatAnalysis(
            rectangles,
            (),
            error_summary,
            main_rows=tuple(main_rows),
            sub_rows=tuple(sub_rows),
            cost=cost,
            page_kind='tuning' if is_tuning_page else 'single',
            layout=layout,
            observed_cost=observed_cost,
            viewport=area.bounds if provisional else (),
            layout_provisional=provisional,
            comparison=comparison,
        )
    # An inferred COST may still show its estimated score, not a definite grade.
    rating = getattr(score, 'rating', None) if observed_cost is not None else None
    grade = f' · {rating.label}' if rating is not None else ''
    summary = (
        f'评分模版：{template_name}\n'
        f'当前评分：{score.current_score:.2f}{grade}\n'
        f'理论最高：{score.potential_score:.2f}'
    )
    if rating is not None:
        labels = ('C', 'B', 'A', 'S', 'SS', 'SSS')
        summary += '\n评级标准：' + ' '.join(
            f'{label}≥{lower:g}' for label, lower in zip(labels, rating.thresholds)
        )
    if live is not None:
        summary += f'\n权重：{live.version[:12]} · 主词条 +25 口径\n{live.reason}'
    if observed_cost is None:
        summary += f'\nCOST 未识别：按 {cost}C 估算，收录需确认'
    if provisional:
        summary += '\n扩展内容布局：按边缘/高度定位，收录需核对'
    tier_labels = ('', '') + tuple(
        f'{tier.index}档' if tier is not None else '' for tier in evidence.tiers
    )
    tier_colors = ((255, 0, 0), (255, 0, 0)) + tuple(
        _tier_text_color(tier) for tier in evidence.tiers
    )
    return EchoStatAnalysis(
        rectangles,
        score.row_scores,
        summary,
        tier_labels,
        tier_colors,
        '',
        tuple(main_rows),
        tuple(sub_rows),
        cost,
        'tuning' if is_tuning_page else 'single',
        live.ids if live is not None and auto_match else (),
        layout,
        observed_cost,
        area.bounds if provisional else (),
        provisional,
        comparison_detected,
        score.current_score,
        score.potential_score,
        f'{score.current_score:.2f}{grade}',
        f'{score.potential_score:.2f}',
        _rating_rgb(rating),
    )


def _find_ocr_rows(ocr_boxes, min_x, max_x, min_y, max_y):
    candidates = [box for box in ocr_boxes if min_x <= box.x <= max_x and min_y <= box.y <= max_y]
    properties = [box for box in candidates if _STAT_TEXT.search(str(box.name))]
    values = [box for box in candidates if _VALUE_TEXT.match(str(box.name))]
    rows = []
    used_values = set()
    for prop in sorted(properties, key=lambda box: (box.y + box.height / 2, box.x)):
        prop_center = prop.y + prop.height / 2
        matches = [
            value
            for value in values
            if id(value) not in used_values
            and value.x >= prop.x + prop.width * 0.5
            and abs((value.y + value.height / 2) - prop_center)
            <= max(9, min(prop.height, value.height) * 0.60)
        ]
        if not matches:
            continue
        value = min(matches, key=lambda item: abs((item.y + item.height / 2) - prop_center))
        used_values.add(id(value))
        left, top = min(prop.x, value.x) - 5, min(prop.y, value.y) - 3
        right = max(prop.x + prop.width, value.x + value.width) + 5
        bottom = max(prop.y + prop.height, value.y + value.height) + 3
        value_text = str(value.name)
        rows.append(
            RecognizedStatRow(
                round(left),
                round(top),
                round(right - left),
                round(bottom - top),
                _normalize_stat_name(str(prop.name), value_text),
                _numeric_value(value_text),
                value_text,
                round(prop.x + prop.width + 8),
                round(prop.y + max(0, (prop.height - 18) / 2)),
            )
        )
    return rows[:7]


def _numeric_value(text):
    match = re.search(r'\d+(?:[.,]\d+)?', str(text).replace('，', '.'))
    return float(match.group(0).replace(',', '.')) if match else 0.0


def _normalize_stat_name(text, value_text):
    compact = re.sub(r'\s+', '', str(text))
    is_percent = '%' in value_text or '％' in value_text
    # Small property icons can OCR as 以/十. Strip only a bounded icon prefix
    # followed by one exact known element/healing property, never prose.
    element = re.fullmatch(
        r'[以十+＋★☆✦✧]{0,2}((?:冷凝|热熔|导电|气动|衍射|湮灭)伤害加成|治疗效果加成)', compact
    )
    if element:
        return element[1]
    if '暴击伤害' in compact:
        return '暴击伤害'
    if '暴击' in compact:
        return '暴击'
    if '共鸣效率' in compact:
        return '共鸣效率'
    if '普攻' in compact:
        return '普攻'
    if '重击' in compact:
        return '重击'
    if '共鸣技能' in compact:
        return '共鸣技能'
    if '共鸣解放' in compact:
        return '共鸣解放'
    for name in ('攻击', '防御', '生命'):
        if name in compact:
            # XW-UID names percentage rolls with a trailing percent marker;
            # the unmarked name is the fixed companion/main property.
            return f'{name}%' if is_percent else name
    return compact


def _observed_cost(ocr_boxes):
    for box in ocr_boxes:
        match = re.fullmatch(r'\s*COST\s*[:：]?\s*([134])\s*', str(box.name), re.IGNORECASE)
        if match:
            return int(match.group(1))

    cost_labels = [box for box in ocr_boxes if str(box.name).strip().upper() == 'COST']
    digits = [box for box in ocr_boxes if str(box.name).strip() in {'1', '3', '4'}]
    for label in cost_labels:
        nearby = [
            box
            for box in digits
            if label.x + label.width * 0.7 <= box.x <= label.x + label.width + 5 * label.height
            and abs((box.y + box.height / 2) - (label.y + label.height / 2)) <= label.height * 0.7
        ]
        if nearby:
            return int(min(nearby, key=lambda box: abs(box.x - label.x)).name)

    return None


def _tier_text_color(tier):
    return {
        'lowest': LOWEST_TIER_TEXT_COLOR,
        'highest': HIGHEST_TIER_TEXT_COLOR,
    }.get(tier.kind if tier is not None else '', TIER_TEXT_COLOR)
