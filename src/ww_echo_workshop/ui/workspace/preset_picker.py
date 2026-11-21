# 评分预设选择弹窗：按评分体系筛选并搜索角色/变体。
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from PySide6.QtWidgets import QPushButton

from ww_echo_workshop.core.policy_presets import SYSTEMS


class PresetPickerDialog(QDialog):
    def __init__(self, catalog, system, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.selected = None
        self.setWindowTitle('选择评分预设')
        self.resize(560, 520)
        self.system = system if system in catalog.systems() else catalog.systems()[0]
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel('评分体系', self))
        from ww_echo_workshop.ui.widgets.inputs import ComboBox

        self.system_box = ComboBox(self)
        for key in catalog.systems():
            self.system_box.addItem(SYSTEMS[key]['label'], userData=key)
        index = self.system_box.findData(self.system)
        self.system_box.setCurrentIndex(index if index >= 0 else 0)
        self.system_box.currentIndexChanged.connect(self._system_changed)
        top.addWidget(self.system_box, 1)
        layout.addLayout(top)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('搜索角色或变体')
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.list = QListWidget(self)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton('取消', self)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        apply = QPushButton('应用', self)
        apply.clicked.connect(self.accept)
        footer.addWidget(apply)
        layout.addLayout(footer)
        self._reload()

    def _system_changed(self, _index):
        self.system = self.system_box.currentData()
        self._reload()

    def _reload(self):
        self._items = self.catalog.presets(self.system)
        self._render()

    def _render(self):
        needle = self.search.text().strip().lower()
        self.list.clear()
        for preset in self._items:
            label = f'{preset.character} · {preset.variant}'
            if needle and needle not in label.lower():
                continue
            item = QListWidgetItem(label, self.list)
            item.setData(Qt.UserRole, preset.preset_id)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _filter(self, _text):
        self._render()

    def accept(self):
        item = self.list.currentItem()
        if item is None:
            return
        preset_id = item.data(Qt.UserRole)
        self.selected = self.catalog.get(preset_id, self.system)
        if self.selected is not None:
            super().accept()
