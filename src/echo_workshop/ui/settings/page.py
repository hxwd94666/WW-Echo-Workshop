# 展示简洁分组的助手设置页面。
"""Inline QWidget settings sharing the existing controllers, no dialog reparenting."""

from PySide6.QtWidgets import QWidget, QSizePolicy, QHBoxLayout, QGridLayout, QPushButton, QLineEdit
from echo_workshop.ui.widgets.inputs import QComboBox
from shiboken6 import delete
from echo_workshop.ui.settings.weight_editor import WeightContent
from echo_workshop.ui.settings.shortcuts import ShortcutContent
from echo_workshop.ui.settings.repair import ControlRepairContent


class _InlineSurface:
    _embedded_settings = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    def compact(self):
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().setSpacing(8)
        for control in self.findChildren(QWidget):
            if isinstance(control, (QPushButton, QComboBox, QLineEdit)):
                control.setMinimumHeight(30)
        # Long recovery action rows become two-column rows, not horizontally
        # scrolling strips. Take only widgets; never delete controller owners.
        layout = self.layout()
        for index in range(layout.count() - 1, -1, -1):
            row = layout.itemAt(index).layout()
            if isinstance(row, QHBoxLayout) and row.count() > 3:
                grid = QGridLayout()
                grid.setContentsMargins(0, 0, 0, 0)
                count = 0
                while row.count():
                    item = row.takeAt(0)
                    if item.widget() is not None:
                        grid.addWidget(item.widget(), count // 2, count % 2)
                        count += 1
                layout.takeAt(index)
                delete(row)
                layout.insertLayout(index, grid)
        if hasattr(self, 'user_details'):
            self.user_details.setMinimumHeight(90)
            self.user_details.setMaximumHeight(130)

    def hideEvent(self, event):
        # Switching back to a business page is not disposal/cancellation. Drafts
        # and shared background jobs survive; status resumes when shown again.
        self.timer.stop()
        super().hideEvent(event)


class WeightSettings(WeightContent, _InlineSurface, QWidget):
    def __init__(self, task, parent=None):
        super().__init__(task, parent)
        self.compact()

    def refresh(self):
        if hasattr(self, 'auto'):
            value = bool(self.task.config.get('AutoWeightUpdates', True))
            if self.auto.isChecked() != value:
                self.auto.blockSignals(True)
                self.auto.setChecked(value)
                self.auto.blockSignals(False)
        super().refresh()


class ShortcutSettings(ShortcutContent, _InlineSurface, QWidget):
    def __init__(self, task, parent=None):
        super().__init__(task, parent)
        self.compact()


class RepairSettings(ControlRepairContent, _InlineSurface, QWidget):
    def __init__(self, task, parent=None):
        super().__init__(task, parent)
        self.compact()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.closed:
            self.timer.start()
            self.poll()
