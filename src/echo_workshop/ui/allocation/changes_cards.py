# 紧凑展示冻结的旧新声骸词条和分差，不查询头像或重新评分。
from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, isDarkTheme

from echo_workshop.ui.inventory.card_data import project_card
from echo_workshop.ui.widgets.rating import apply_rating_colour
from echo_workshop.ui.widgets.rating_style import score_text


def points(cents, rating):
    return score_text(cents / 100, rating) if cents is not None else '未计算'


def delta_colour(cents):
    if cents is None or cents == 0:
        return '#b7b7b7' if isDarkTheme() else '#656565'
    if cents > 0:
        return '#69d69d' if isDarkTheme() else '#087849'
    return '#ff999f' if isDarkTheme() else '#ba3340'


def set_delta(label, cents, *, prefix=''):
    label.setText(prefix + (f'{cents / 100:+.2f} 分' if cents is not None else '—'))
    label.setStyleSheet('color:' + delta_colour(cents) + ';font-weight:600;')


class CompactChangeCard(QFrame):
    """Full observed rows, smaller spacing; no owner-avatar lookup path exists."""

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item
        self.show_owner_avatar = False
        self.data = project_card(item.record)
        self.rating = item.rating if item.score_cents is not None else None
        self.setObjectName('compactChangeCard')
        self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(3)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.name = BodyLabel(self.data.title, self)
        self.name.setWordWrap(True)
        font = self.name.font()
        font.setPixelSize(12)
        font.setBold(True)
        self.name.setFont(font)
        header.addWidget(self.name, 1)
        if self.data.owner:
            owner = CaptionLabel(self.data.owner, self)
            owner.setWordWrap(True)
            header.addWidget(owner)
        layout.addLayout(header)
        meta = CaptionLabel(self.data.metadata, self)
        meta.setWordWrap(True)
        layout.addWidget(meta)
        stats = QGridLayout()
        stats.setHorizontalSpacing(5)
        stats.setVerticalSpacing(1)
        stats.setContentsMargins(0, 2, 0, 2)
        stats.setColumnStretch(0, 1)
        self.stat_labels = []
        for ordinal, row in enumerate(self.data.rows):
            name = CaptionLabel(row.name, self)
            name.setWordWrap(True)
            value = CaptionLabel(row.value, self)
            if row.main:
                for label in (name, value):
                    font = label.font()
                    font.setBold(True)
                    label.setFont(font)
            stats.addWidget(name, ordinal, 0)
            stats.addWidget(value, ordinal, 1)
            self.stat_labels.append((name, value))
        layout.addLayout(stats)
        if self.data.empty_sub:
            empty = CaptionLabel(self.data.empty_sub, self)
            empty.setWordWrap(True)
            layout.addWidget(empty)
        self.score = BodyLabel(points(item.score_cents, self.rating), self)
        layout.addWidget(self.score)
        self.update_colours()

    def update_colours(self):
        apply_rating_colour(self.score, self.rating)


class ChangeColumn(QWidget):
    def __init__(self, item, label, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(CaptionLabel(label, self))
        self.card = None
        if item is None or item.record is None:
            self.empty = CaptionLabel(
                '无原声骸'
                if item is None and label == '原配装'
                else '无新声骸'
                if item is None
                else '记录缺失 · ' + item.item_id[:8],
                self,
            )
            self.empty.setWordWrap(True)
            layout.addWidget(self.empty)
            layout.addStretch(1)
        else:
            self.card = CompactChangeCard(item, self)
            layout.addWidget(self.card)
            layout.addStretch(1)


class ChangePairPanel(QFrame):
    def __init__(self, pair, parent=None):
        super().__init__(parent)
        self.pair = pair
        self.setObjectName('changePairPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 6, 7, 7)
        layout.setSpacing(4)
        header = QHBoxLayout()
        label = {'replaced': '替换', 'removed': '移除', 'added': '新增'}[pair.kind]
        header.addWidget(CaptionLabel(label, self))
        header.addStretch(1)
        self.delta = CaptionLabel(self)
        header.addWidget(self.delta)
        layout.addLayout(header)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.old = ChangeColumn(pair.old, '原配装', self)
        self.new = ChangeColumn(pair.new, '新配装', self)
        row.addWidget(self.old, 1)
        row.addWidget(self.new, 1)
        layout.addLayout(row)
        self.update_colours()

    def update_colours(self):
        set_delta(self.delta, self.pair.delta_cents, prefix='单件 ')
        for column in (self.old, self.new):
            if column.card is not None:
                column.card.update_colours()


class ChangePairsGrid(QWidget):
    """Two compact pairs on a wide surface, one on a narrow surface."""

    def __init__(self, pairs, parent=None):
        super().__init__(parent)
        self.panels = [ChangePairPanel(pair, self) for pair in pairs]
        self.columns = 0
        self.grid = QGridLayout(self)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._reflow()

    def minimumSizeHint(self):
        return QSize(0, 0)

    def _reflow(self):
        columns = 2 if self.width() >= 920 else 1
        if columns == self.columns:
            return
        self.columns = columns
        for panel in self.panels:
            self.grid.removeWidget(panel)
        for ordinal, panel in enumerate(self.panels):
            self.grid.addWidget(panel, ordinal // columns, ordinal % columns)
        for column in range(2):
            self.grid.setColumnStretch(column, int(column < columns))
        self.grid.invalidate()
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()
