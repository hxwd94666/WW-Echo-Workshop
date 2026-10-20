# 构造角色设置中的共鸣效率范围控件。
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import CaptionLabel
from echo_workshop.ui.widgets.inputs import QDoubleSpinBox


def build_energy_range(parent, minimum, maximum):
    widget = QWidget(parent)
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(CaptionLabel('共鸣效率范围', widget))
    lower = QDoubleSpinBox(widget)
    lower.setRange(100, 1000)
    lower.setDecimals(1)
    lower.setSuffix('%')
    lower.setValue(minimum)
    lower.setMinimumHeight(36)
    lower.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    upper = QDoubleSpinBox(widget)
    upper.setRange(0, 1000)
    upper.setDecimals(1)
    upper.setSuffix('%')
    upper.setSpecialValueText('无限制')
    upper.setValue(maximum or 0)
    upper.setMinimumHeight(36)
    upper.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    row.addWidget(lower, 1)
    row.addWidget(CaptionLabel('~', widget))
    row.addWidget(upper, 1)
    return widget, lower, upper
