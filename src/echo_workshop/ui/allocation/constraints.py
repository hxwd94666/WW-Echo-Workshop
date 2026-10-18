# 编辑配装槽位和固定声骸约束。
"""Structured widgets with legacy text adapters, never exposed rule syntax."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QMenu,
    QPushButton,
)
from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES


class MainStatChoice(QPushButton):
    changed = Signal()

    def __init__(self, cost, text='', parent=None):
        super().__init__(parent)
        self._value = text
        self.set_options(cost)

    def text(self):
        # Stable compatibility adapter used by the existing strict serializer.
        return self._value

    def setText(self, value):
        self._value = value
        self.set_options(self.cost)
        self.changed.emit()

    def set_options(self, cost):
        self.cost = cost
        selected = tuple(
            name.strip() for name in self._value.replace('，', ',').split(',') if name.strip()
        )
        options = tuple(MAX_MAINSTAT_VALUES[cost][0])
        old = self.menu()
        menu = QMenu(self)
        for name in dict.fromkeys(options + selected):
            action = menu.addAction(name if name in options else name + '（与 COST 不兼容）')
            action.setCheckable(True)
            action.setChecked(name in selected)
            action.toggled.connect(lambda checked, name=name: self._toggle(name, checked))
        self.setMenu(menu)
        if old is not None:
            old.deleteLater()
        super().setText('不限主词条' if not selected else '、'.join(selected))
        self.setToolTip('可勾选多个主词条；全部取消表示不限。切换 COST 不会静默删除已有要求。')

    def _toggle(self, name, checked):
        selected = [part.strip() for part in self._value.split(',') if part.strip()]
        if checked and name not in selected:
            selected.append(name)
        if not checked and name in selected:
            selected.remove(name)
        self._value = ', '.join(selected)
        super().setText('、'.join(selected) or '不限主词条')
        self.changed.emit()
