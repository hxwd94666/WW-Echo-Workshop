# 绘制五槽费用选择并限制总费用。
"""Explicit COST buttons; no wheel selection, IO, preferences or calculation."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, PushButton, TogglePushButton
from echo_workshop.core.costs import (
    COST_OPTIONS,
    COST_PRESETS,
    DEFAULT_COST_LAYOUT,
    MAX_TOTAL_COST,
    read_cost_layout,
)


class CostLayoutPicker(QWidget):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self._costs = DEFAULT_COST_LAYOUT
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        heading = QHBoxLayout()
        self.title_label = BodyLabel('五槽 COST', self)
        self.total_label = BodyLabel(self)
        heading.addWidget(self.title_label)
        heading.addWidget(self.total_label)
        heading.addStretch()
        layout.addLayout(heading)
        self.legacy_warning = CaptionLabel(self)
        self.legacy_warning.setWordWrap(True)
        layout.addWidget(self.legacy_warning)
        self.presets = QBoxLayout(QBoxLayout.LeftToRight)
        self.presets.setSpacing(8)
        self.preset_buttons = {}
        for costs in COST_PRESETS:
            button = PushButton(' / '.join(map(str, costs)), self)
            button.clicked.connect(lambda _checked=False, value=costs: self._preset(value))
            self.presets.addWidget(button, 1)
            self.preset_buttons[costs] = button
        layout.addLayout(self.presets)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        layout.addLayout(self.grid)
        self.slot_boxes, self.button_groups, self.buttons = [], [], []
        for index in range(5):
            box = QWidget(self)
            box.setMinimumWidth(0)
            box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            body = QVBoxLayout(box)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(4)
            title = CaptionLabel(f'槽位 {index + 1}', box)
            title.setAlignment(Qt.AlignCenter)
            body.addWidget(title)
            row = QVBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            group = QButtonGroup(box)
            group.setExclusive(True)
            buttons = {}
            for cost in COST_OPTIONS:
                button = TogglePushButton(str(cost), box)
                button.setCheckable(True)
                button.setFixedSize(52, 28)
                button.setToolTip(f'第 {index + 1} 槽：COST {cost}')
                group.addButton(button, cost)
                row.addWidget(button, 0, Qt.AlignHCenter)
                buttons[cost] = button
            body.addLayout(row)
            group.idClicked.connect(lambda cost, slot=index: self._choose(slot, cost))
            self.slot_boxes.append(box)
            self.button_groups.append(group)
            self.buttons.append(buttons)
        self._column_count = None
        self._reflow()
        self._sync()

    def costs(self):
        return self._costs

    def set_costs(self, costs, *, allow_legacy=False):
        # Programmatic loads never emit; the total-cost rule is enforced by the
        # native terminal, this picker only keeps the five-slot shape.
        value = read_cost_layout(costs)
        self._costs = value
        self._sync()

    def _sync(self):
        total = sum(self._costs)
        suffix = '（历史配置，调整至 12 以内后才能保存）' if total > MAX_TOTAL_COST else ''
        self.total_label.setText(f'{total} / {MAX_TOTAL_COST}')
        self.legacy_warning.setText(suffix)
        self.legacy_warning.setVisible(bool(suffix))
        for index, group in enumerate(self.button_groups):
            blocked = group.blockSignals(True)
            try:
                for cost, button in self.buttons[index].items():
                    button.setChecked(cost == self._costs[index])
                    allowed = total - self._costs[index] + cost <= MAX_TOTAL_COST
                    button.setEnabled(allowed)
                    button.setToolTip(
                        f'第 {index + 1} 槽：COST {cost}'
                        if allowed
                        else '此选择会超过 COST 12；请先降低其他槽位。'
                    )
            finally:
                group.blockSignals(blocked)

    def _choose(self, index, cost):
        value = list(self._costs)
        value[index] = cost
        value = read_cost_layout(value)
        if value != self._costs:
            self._costs = value
            self._sync()
            self.changed.emit(value)

    def _preset(self, costs):
        self.set_costs(costs)
        # Reapplying the visible preset is an explicit confirmation of a
        # fallback/legacy draft, even when the displayed five values match.
        self.changed.emit(self._costs)

    def _reflow(self):
        if not hasattr(self, 'slot_boxes'):
            return
        width = self.contentsRect().width()
        count = 5 if width >= 330 else 3 if width >= 190 else 2 if width >= 130 else 1
        if count != self._column_count:
            for index, box in enumerate(self.slot_boxes):
                self.grid.removeWidget(box)
                self.grid.addWidget(box, index // count, index % count)
            self._column_count = count
        self.presets.setDirection(
            QBoxLayout.LeftToRight if width >= 280 else QBoxLayout.TopToBottom
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()
