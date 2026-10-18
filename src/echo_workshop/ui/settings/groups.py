# 定义现行设置分组和控件范围。
"""Five small settings groups, one content scroll surface, no configuration IO."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import SegmentedWidget


class GroupedSettingsSurface(QWidget):
    groupChanged = Signal(str)
    GROUPS = (
        ('common', '常用'),
        ('intake', '收录'),
        ('scoring', '计算'),
        ('data', '数据'),
        ('advanced', '其他'),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.sections, self._groups, self._positions = {}, {}, {}
        self.current_group = 'common'
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        self.navigation = SegmentedWidget(self)
        for key, label in self.GROUPS:
            self.navigation.addItem(
                key, label, onClick=lambda checked, group=key: self.show_group(group)
            )
        self.navigation.setCurrentItem(self.current_group)
        root.addWidget(self.navigation)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.body = QWidget(self.scroll)
        self.body.setMinimumWidth(0)
        self.content_layout = QVBoxLayout(self.body)
        self.content_layout.setContentsMargins(12, 12, 12, 12)
        self.content_layout.setSpacing(16)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

    def add_section(self, key, section, group):
        self.sections[key] = section
        self._groups[key] = group
        self.content_layout.addWidget(section)
        section.setVisible(group == self.current_group)

    def show_group(self, group):
        if group not in dict(self.GROUPS):
            raise ValueError('未知设置分组')
        self._positions[self.current_group] = self.verticalScrollBar().value()
        self.current_group = group
        self.navigation.setCurrentItem(group)
        self.groupChanged.emit(group)  # Owner lazily creates only this group's controllers.
        for key, section in self.sections.items():
            section.setVisible(self._groups[key] == group)
        self.content_layout.activate()
        self.verticalScrollBar().setValue(self._positions.get(group, 0))

    def group_for_section(self, key):
        return self._groups[key]

    def verticalScrollBar(self):
        return self.scroll.verticalScrollBar()

    def horizontalScrollBar(self):
        return self.scroll.horizontalScrollBar()

    def viewport(self):
        return self.scroll.viewport()

    def ensureWidgetVisible(self, widget, *args):
        for key, section in self.sections.items():
            if section is widget or section.isAncestorOf(widget):
                group = self._groups[key]
                if group != self.current_group:
                    self.show_group(group)
                break
        self.scroll.ensureWidgetVisible(widget, *args)
