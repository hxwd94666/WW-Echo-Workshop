# 提供工作台使用的轻量折叠区，默认收起高级内容。
from PySide6.QtWidgets import QVBoxLayout, QWidget
from PySide6.QtWidgets import QPushButton


class CollapsibleSection(QWidget):
    def __init__(self, title, parent=None, *, expanded=False):
        super().__init__(parent)
        self._title = title
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        self.toggle = QPushButton(self)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.clicked.connect(self._toggle)
        outer.addWidget(self.toggle)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(12, 4, 0, 0)
        self.content_layout.setSpacing(8)
        outer.addWidget(self.content)
        self.content.setVisible(expanded)
        self._sync_text()

    def _toggle(self, checked):
        self.content.setVisible(bool(checked))
        self._sync_text()

    def _sync_text(self):
        arrow = '▾' if self.toggle.isChecked() else '▸'
        self.toggle.setText(f'{arrow}  {self._title}')

    def set_expanded(self, expanded):
        self.toggle.setChecked(bool(expanded))
        self.content.setVisible(bool(expanded))
        self._sync_text()

    def is_expanded(self):
        return self.toggle.isChecked()
