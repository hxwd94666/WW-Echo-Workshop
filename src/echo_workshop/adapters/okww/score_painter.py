# 根据游戏页面布局绘制紧凑实时评分，不遮挡词条与操作按钮。
import ctypes
import os
import re

from echo_workshop.capture.viewport import EchoViewport
from echo_workshop.ui.widgets.rating_style import summary_colour

TIER_TEXT_COLOR = (80, 185, 255)
SUMMARY_TEMPLATE_COLOR = (255, 110, 220)
SUMMARY_CURRENT_COLOR = (255, 220, 80)
SUMMARY_POTENTIAL_COLOR = (120, 235, 150)


def summary_region(layout, width, height, viewport=None, rectangles=(), *, template_count=1):
    area = EchoViewport.checked(viewport or None, width, height)
    bounds = {
        'bag': (0.545, 0.43, 0.665, 0.71),
        'comparison': (0.48, 0.215, 0.64, 0.46),
        'display': (0.64, 0.215, 0.76, 0.46),
        'enhancement': (0.375, 0.235, 0.535, 0.51),
    }.get(layout, (0.39, 0.215, 0.66, 0.80))
    dx, dy = area.offset('left' if layout == 'enhancement' else 'right')
    x1, y1, x2, y2 = bounds
    # Fewer templates have larger text and a wider corridor, still next to the stats.
    span = (0.16, 0.145, 0.125)[max(1, min(template_count, 3)) - 1]
    if layout in ('bag', 'display'):
        span = min(span, 0.12)
    if layout == 'enhancement':
        x2 = x1 + span
    else:
        x1 = x2 - span
    left = dx + area.reference_width * x1
    top = dy + area.height * y1
    right = dx + area.reference_width * x2
    bottom = dy + area.height * y2
    return tuple(round(v) for v in (left, top, right - left, bottom - top))


def summary_font_size(template_count, height, available_height, line_count):
    """One role is prominent; three retain compact type and all score rows."""
    scale = (0.0240, 0.0236, 0.0194)[max(1, min(template_count, 3)) - 1]
    gap = max(2, round(height * 0.0015))
    return max(12, min(round(height * scale), available_height // max(1, line_count) - gap))


def summary_text_top(top, available_height, font_size, line_count, gap):
    content_height = line_count * (font_size + gap) - gap
    return round(top + max(0, available_height - content_height) / 2)


def wrap_summary(lines, max_width, measure):
    result = []
    for line in lines:
        current = ''
        tokens = re.findall(r'[A-Za-z0-9≥≤+−%./_-]+|\s+|.', line)
        # Keep a complete threshold/score together whenever it fits alone.
        units = [
            part
            for token in tokens
            for part in (list(token) if measure(token) > max_width else [token])
        ]
        for char in units:
            if current and measure(current + char) > max_width:
                result.append(current)
                current = ''
            current += char
        result.append(current)
    return result


class EchoStatBoxPainter:
    def __init__(self):
        self.rectangles = []
        self.summary = ''
        self.layout = ''
        self.viewport = ()
        self.tier_labels = []
        self.tier_colors = []
        self.row_scores = []
        self.summary_colours = []

    def update(
        self,
        rectangles,
        summary='',
        tier_labels=(),
        tier_colors=(),
        row_scores=(),
        *,
        layout='',
        viewport=(),
        summary_colours=(),
    ):
        self.rectangles = list(rectangles)
        self.summary = summary
        self.layout = layout
        self.viewport = viewport
        self.tier_labels = list(tier_labels)
        self.tier_colors = list(tier_colors)
        self.row_scores = list(row_scores)
        self.summary_colours = list(summary_colours)

    def paint(self, canvas, _overlay):
        for index, rectangle in enumerate(self.rectangles):
            canvas.rectangle(
                rectangle.x,
                rectangle.y,
                rectangle.width,
                rectangle.height,
                color=rectangle.color,
                line_width=1,
            )
            score = self.row_scores[index] if index < len(self.row_scores) else 0.0
            if abs(score) >= 0.005:
                _paint_row_score(canvas, rectangle, score)
            tier_label = self.tier_labels[index] if index < len(self.tier_labels) else ''
            if tier_label:
                tier_color = (
                    self.tier_colors[index] if index < len(self.tier_colors) else TIER_TEXT_COLOR
                )
                _paint_bold_text(
                    canvas,
                    rectangle.tier_x or rectangle.x,
                    rectangle.tier_y or rectangle.y,
                    tier_label,
                    tier_color,
                    max(18, round(getattr(_overlay, '_frame_height', 1080) * 0.018)),
                )
        if self.summary:
            _paint_score_summary(
                canvas,
                _overlay,
                self.summary,
                self.layout,
                self.viewport,
                self.rectangles,
                self.summary_colours,
            )


def _paint_row_score(canvas, rectangle, score):
    """Draw the per-stat contribution next to its stat row, as the original did."""
    x = rectangle.x + rectangle.width + 8
    y = rectangle.y + max(0, (rectangle.height - 16) / 2)
    canvas.text(x, y, f'+{score:.2f}', color=rectangle.color)


def _paint_bold_text(canvas, x, y, text, color, font_size=24):
    """Paint an OCR-adjacent tier label with a readable bold native font."""
    if os.name != 'nt':
        canvas.text(x, y, text, color=color)
        return
    from ok.ui.overlay import win32_gdi

    font = win32_gdi.gdi32.CreateFontW(
        -max(16, round(font_size * canvas.ratio)),
        0,
        0,
        0,
        700,
        0,
        0,
        0,
        1,
        0,
        0,
        5,
        0,
        'Microsoft YaHei UI',
    )
    old_font = win32_gdi.gdi32.SelectObject(canvas.hdc, font)
    try:
        win32_gdi.gdi32.SetBkMode(canvas.hdc, 1)
        win32_gdi.gdi32.SetTextColor(canvas.hdc, win32_gdi._rgb(*color))
        win32_gdi.gdi32.TextOutW(
            canvas.hdc, round(x * canvas.ratio), round(y * canvas.ratio), text, len(text)
        )
    finally:
        win32_gdi.gdi32.SelectObject(canvas.hdc, old_font)
        win32_gdi.gdi32.DeleteObject(font)


def _paint_score_summary(
    canvas, overlay, text, layout='', viewport=(), rectangles=(), summary_colours=()
):
    if os.name != 'nt':
        return
    from ok.ui.overlay import win32_gdi

    width = round(getattr(overlay, '_frame_width', 0) * canvas.ratio)
    height = round(getattr(overlay, '_frame_height', 0) * canvas.ratio)
    if width <= 0 or height <= 0:
        return
    lines = text.splitlines()
    if not lines:
        return
    lines = [line if len(line) <= 160 else line[:159] + '…' for line in lines]
    template_count = max(1, sum(line.startswith(('当前评分', '参考评分')) for line in lines))
    region = summary_region(
        layout,
        getattr(overlay, '_frame_width', 0),
        getattr(overlay, '_frame_height', 0),
        viewport,
        rectangles,
        template_count=template_count,
    )
    x, y, available_width, available_height = [round(v * canvas.ratio) for v in region]
    padding = max(2, round(height * 0.002))
    font_size = summary_font_size(
        template_count, height, available_height - 2 * padding, len(lines)
    )
    font = win32_gdi.gdi32.CreateFontW(
        -font_size,
        0,
        0,
        0,
        700,
        0,
        0,
        0,
        1,
        0,
        0,
        5,
        0,
        'Microsoft YaHei UI',
    )
    old_font = win32_gdi.gdi32.SelectObject(canvas.hdc, font)
    old_mode = win32_gdi.gdi32.SetBkMode(canvas.hdc, 1)
    try:

        def measure(line):
            size = win32_gdi.SIZE()
            win32_gdi.gdi32.GetTextExtentPoint32W(
                canvas.hdc, line, len(line.encode('utf-16-le')) // 2, ctypes.byref(size)
            )
            return size.cx

        numeric = [
            line for line in lines if line.startswith(('当前评分', '参考评分', '左 ', '理论最高'))
        ]
        max_numeric = max(map(measure, numeric), default=0)
        if max_numeric > available_width - 2 * padding:
            # Re-select a fitting native font, never ellipsize score digits or grades.
            fit_size = max(12, int(font_size * (available_width - 2 * padding) / max_numeric))
            fit_font = win32_gdi.gdi32.CreateFontW(
                -fit_size, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, 'Microsoft YaHei UI'
            )
            win32_gdi.gdi32.SelectObject(canvas.hdc, fit_font)
            win32_gdi.gdi32.DeleteObject(font)
            font, font_size = fit_font, fit_size
        fitted = []
        for line in lines:
            # Numeric rows stay intact; long template names are bounded, not wrapped
            # into the following character's score block.
            while (
                line not in numeric
                and measure(line) > available_width - 2 * padding
                and len(line) > 1
            ):
                line = line.rstrip('…')[:-1] + '…'
            fitted.append(line)
        lines = fitted
        line_height = font_size + max(2, round(height * 0.0015))
        x += padding
        y = summary_text_top(y, available_height, font_size, len(lines), line_height - font_size)
        previous_line = ''
        for index, line in enumerate(lines):
            line_y = y + index * line_height
            row_colours = summary_colours[index] if index < len(summary_colours) else ()
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                win32_gdi.gdi32.SetTextColor(canvas.hdc, win32_gdi._rgb(0, 0, 0))
                win32_gdi.gdi32.TextOutW(
                    canvas.hdc, x + dx, line_y + dy, line, len(line.encode('utf-16-le')) // 2
                )
            color = (
                SUMMARY_POTENTIAL_COLOR
                if line.startswith('理论最高') or previous_line.startswith('理论最高')
                else SUMMARY_CURRENT_COLOR
                if line.startswith(('当前', '参考', '左当前', '右当前'))
                or previous_line.startswith(('当前评分', '参考评分'))
                else SUMMARY_TEMPLATE_COLOR
            )
            rated_colour = summary_colour(line)
            if line.startswith('差值 '):
                color = (255, 115, 110) if line.startswith('差值 -') else SUMMARY_POTENTIAL_COLOR
            elif rated_colour:
                color = tuple(int(rated_colour[i : i + 2], 16) for i in (1, 3, 5))
            elif '不适配' in line:
                color = (255, 100, 100)
            elif '适配未知' in line or '待核对' in line or '待纠错' in line:
                color = SUMMARY_CURRENT_COLOR
            parts = line.split(' / ')
            if len(parts) == 2:
                cursor = x
                for part_index, part in enumerate(parts):
                    rated = summary_colour(part)
                    part_colour = (
                        tuple(int(rated[i : i + 2], 16) for i in (1, 3, 5))
                        if rated
                        else (
                            row_colours[part_index]
                            if part_index < len(row_colours) and row_colours[part_index]
                            else color
                        )
                    )
                    win32_gdi.gdi32.SetTextColor(canvas.hdc, win32_gdi._rgb(*part_colour))
                    win32_gdi.gdi32.TextOutW(
                        canvas.hdc, cursor, line_y, part, len(part.encode('utf-16-le')) // 2
                    )
                    cursor += measure(part)
                    if part_index == 0:
                        win32_gdi.gdi32.SetTextColor(canvas.hdc, win32_gdi._rgb(*color))
                        win32_gdi.gdi32.TextOutW(canvas.hdc, cursor, line_y, ' / ', 3)
                        cursor += measure(' / ')
            else:
                if row_colours and row_colours[0]:
                    color = row_colours[0]
                win32_gdi.gdi32.SetTextColor(canvas.hdc, win32_gdi._rgb(*color))
                win32_gdi.gdi32.TextOutW(
                    canvas.hdc, x, line_y, line, len(line.encode('utf-16-le')) // 2
                )
            previous_line = line
    finally:
        win32_gdi.gdi32.SetBkMode(canvas.hdc, old_mode)
        win32_gdi.gdi32.SelectObject(canvas.hdc, old_font)
        win32_gdi.gdi32.DeleteObject(font)
