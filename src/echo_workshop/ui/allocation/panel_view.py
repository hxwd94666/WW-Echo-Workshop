# 绘制单套配装的面板卡，并提供可折叠来源的属性弹窗。
from dataclasses import dataclass

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from echo_workshop.core.panel import display_stat_name
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QVBoxLayout,
)
from qfluentwidgets import BodyLabel, CaptionLabel, PushButton, isDarkTheme

PANEL_ROLE = int(Qt.UserRole) + 30
STATS = (
    ('hp', '生命', '生命%'),
    ('atk', '攻击', '攻击%'),
    ('defense', '防御', '防御%'),
    ('crit_rate', '暴击率', '暴击'),
    ('crit_dmg', '暴击伤害', '暴击伤害'),
    ('energy_regen', '共鸣效率', '共鸣效率'),
)


@dataclass(frozen=True)
class PanelCardData:
    rows: tuple
    weapon: tuple = ()
    detail: tuple = ()
    title: str = '角色面板'


def _order_rank(label):
    core = {'生命': 0, '攻击': 1, '防御': 2, '共鸣效率': 3, '暴击率': 4, '暴击伤害': 5}
    if label in core:
        return core[label]
    elements = ('热熔', '冷凝', '导电', '气动', '衍射', '湮灭', '全属性')
    if '伤害加成' in label and label.startswith(elements):
        return 6
    if '伤害加成' in label:
        return 7
    return 8


def _extra_rows(detail):
    values = {}
    skip = {
        '生命',
        '攻击',
        '防御',
        '暴击',
        '暴击伤害',
        '共鸣效率',
        '攻击%',
        '生命%',
        '防御%',
    }
    for bucket in (
        detail.echoes,
        detail.skill_bonus,
        detail.sonata,
        detail.weapon_passive,
    ):
        for name, value in bucket.items():
            if name in skip or not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            label = display_stat_name(name)
            values[label] = values.get(label, 0.0) + float(value)
    return tuple((label, f'{value:g}%') for label, value in values.items() if value)


def _weapon_rows(weapon, weapon_id):
    name = (weapon or {}).get('name') or weapon_id or '未指定'
    rows = [('武器', name)]
    atk = (weapon or {}).get('atk_90')
    if isinstance(atk, (int, float)) and not isinstance(atk, bool):
        rows.append(('基础攻击', f'{atk:g}'))
    substat = (weapon or {}).get('substat')
    value = (weapon or {}).get('substat_value')
    if isinstance(substat, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
        suffix = '%' if (weapon or {}).get('substat_is_percent') else ''
        rows.append(('副词条', f'{substat} {value:g}{suffix}'))
    return tuple(rows)


def build_panel_data(row, request_role, records, service):
    if row is None or service is None:
        return PanelCardData((('状态', '面板资料不可用'),))
    by_id = {record.item_id: record for record in records}
    item_ids = tuple(row.get('item_ids', ()))
    echoes = tuple(by_id[item] for item in item_ids if item in by_id)
    if len(echoes) != 5:
        return PanelCardData((('状态', '五件声骸记录缺失'),))
    try:
        weapon_id, detail = service.breakdown(
            row['character_id'], (request_role or {}).get('weapon_id', ''), echoes
        )
    except (ValueError, KeyError, TypeError) as error:
        return PanelCardData((('状态', str(error)),))
    totals = detail.totals
    formatted = {
        'hp': f'{totals.hp:.0f}',
        'atk': f'{totals.atk:.0f}',
        'defense': f'{totals.defense:.0f}',
        'crit_rate': f'{totals.crit_rate:.1f}%',
        'crit_dmg': f'{totals.crit_dmg:.1f}%',
        'energy_regen': f'{totals.energy_regen:.1f}%',
    }
    ordered = [(label, formatted[key]) for key, label, _ in STATS]
    ordered.extend(_extra_rows(detail))
    ordered.sort(key=lambda item: _order_rank(item[0]))
    rows = tuple(ordered)

    def join(bucket):
        return '，'.join(f'{display_stat_name(k)} {v:g}' for k, v in bucket.items())

    parts = []
    if detail.weapon_passive:
        parts.append('武器被动：' + join(detail.weapon_passive))
    if detail.skill_bonus:
        parts.append('技能树：' + join(detail.skill_bonus))
    if detail.sonata:
        parts.append('套装：' + join(detail.sonata))
    if detail.echoes:
        parts.append('声骸：' + join(detail.echoes))
    return PanelCardData(rows, _weapon_rows(detail.weapon, weapon_id), tuple(parts))


def paint_panel_card(painter, rect, data, font, selected=False):
    dark = isDarkTheme()
    surface = QColor('#202020' if dark else '#f5f5f5')
    text = QColor('#eeeeee' if dark else '#202020')
    muted = QColor('#9a9a9a' if dark else '#666666')
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    border = QColor('#333333' if dark else '#dedede')
    painter.setBrush(surface)
    painter.setPen(QPen(border, 1.5 if selected else 1))
    painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)
    inside = rect.adjusted(14, 10, -14, -10)
    title_font = QFont(font)
    title_font.setBold(True)
    if title_font.pixelSize() > 0:
        title_font.setPixelSize(title_font.pixelSize() + 5)
    else:
        title_font.setPointSizeF(title_font.pointSizeF() + 3)
    subtitle_font = QFont(font)
    if subtitle_font.pixelSize() > 0:
        subtitle_font.setPixelSize(max(9, subtitle_font.pixelSize() - 1))
    else:
        subtitle_font.setPointSizeF(max(8.0, subtitle_font.pointSizeF() - 1.0))
    title_line = QFontMetrics(title_font).height() + 6
    subtitle_line = QFontMetrics(subtitle_font).height() + 2
    row_line = QFontMetrics(font).height() + 5
    available = max(1, (inside.height() - title_line - subtitle_line - 4) // row_line)
    shown = min(available, len(data.rows))
    content_h = title_line + subtitle_line + shown * row_line
    top = inside.y() + max(0, (inside.height() - content_h) // 2)
    painter.setFont(title_font)
    painter.setPen(text)
    painter.drawText(QRect(inside.x(), top, inside.width(), title_line), Qt.AlignCenter, data.title)
    painter.setFont(subtitle_font)
    painter.setPen(muted)
    painter.drawText(
        QRect(inside.x(), top + title_line, inside.width(), subtitle_line),
        Qt.AlignCenter,
        '静态属性',
    )
    x = inside.x() + 4
    block = inside.width() - 8
    block_top = top + title_line + subtitle_line
    separator = QColor('#303030' if dark else '#d8d8d8')
    value_font = QFont(font)
    value_font.setBold(True)
    for index, (label, value) in enumerate(data.rows[:shown]):
        y = block_top + index * row_line
        if index:
            painter.setPen(QPen(separator, 1))
            painter.drawLine(x + 8, y, x + block - 8, y)
        painter.setFont(font)
        painter.setPen(text)
        painter.drawText(
            QRect(x + 8, y, max(1, block - 86), row_line),
            Qt.AlignLeft | Qt.AlignVCenter,
            QFontMetrics(font).elidedText(label, Qt.ElideRight, max(1, block - 86)),
        )
        painter.setFont(value_font)
        painter.setPen(text)
        painter.drawText(
            QRect(x + block - 76, y, 76, row_line),
            Qt.AlignRight | Qt.AlignVCenter,
            value,
        )
    painter.restore()


class PanelDetailsDialog(QDialog):
    def __init__(self, parent, data):
        super().__init__(parent)
        self.setWindowTitle('角色面板')
        self.setMinimumWidth(520)
        self._sources_open = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        title = BodyLabel('角色面板', self)
        font = title.font()
        font.setBold(True)
        if font.pixelSize() > 0:
            font.setPixelSize(font.pixelSize() + 4)
        else:
            font.setPointSizeF(font.pointSizeF() + 3)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(CaptionLabel('静态面板 · 不含条件触发', self))
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        for index, (label, value) in enumerate(data.rows):
            row, column = divmod(index, 2)
            grid.addWidget(CaptionLabel(label, self), row, column * 2)
            cell = QLabel(value, self)
            cell.setStyleSheet('font-weight: 600;')
            grid.addWidget(cell, row, column * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)
        self.source_button = PushButton('展开详情', self)
        self.source_button.setFixedWidth(112)
        self.source_button.clicked.connect(self._toggle_sources)
        layout.addWidget(self.source_button, alignment=Qt.AlignRight)
        self.source_box = QFrame(self)
        self.source_box.setObjectName('PanelSourceBox')
        border = '#444444' if isDarkTheme() else '#d8d8d8'
        self.source_box.setStyleSheet(
            f'QFrame#PanelSourceBox {{ border: 1px solid {border}; border-radius: 6px; }}'
        )
        source_layout = QVBoxLayout(self.source_box)
        source_layout.setContentsMargins(12, 10, 12, 10)
        source_layout.setSpacing(6)
        if data.detail:
            source_title = BodyLabel('来源', self.source_box)
            source_font = source_title.font()
            source_font.setBold(True)
            if source_font.pixelSize() > 0:
                source_font.setPixelSize(source_font.pixelSize() + 2)
            else:
                source_font.setPointSizeF(source_font.pointSizeF() + 1.5)
            source_title.setFont(source_font)
            source_layout.addWidget(source_title)
            for line in data.detail:
                label = CaptionLabel('· ' + line, self.source_box)
                label.setWordWrap(True)
                source_layout.addWidget(label)
        if data.weapon:
            weapon_name = data.weapon[0][1] if data.weapon[0][0] == '武器' else '未指定'
            stats = data.weapon[1:] if data.weapon[0][0] == '武器' else data.weapon
            text = f'武器（{weapon_name}）：' + '，'.join(
                f'{label} {value}' for label, value in stats
            )
            weapon_line = CaptionLabel(text, self.source_box)
            weapon_line.setWordWrap(True)
            source_layout.addWidget(weapon_line)
        self.source_box.hide()
        layout.addWidget(self.source_box)
        layout.addStretch(1)

    def _toggle_sources(self):
        self._sources_open = not self._sources_open
        self.source_box.setVisible(self._sources_open)
        self.source_button.setText('收起详情' if self._sources_open else '展开详情')
        self.layout().activate()
        self.adjustSize()
