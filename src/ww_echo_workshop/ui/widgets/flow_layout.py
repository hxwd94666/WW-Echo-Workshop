# 让筛选选项按可用宽度自动换行。
from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import QLayout


class FlowLayout(QLayout):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.setSpacing(8)

    def addItem(self, item):
        self.items.append(item)
        self.invalidate()

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < self.count() else None

    def takeAt(self, index):
        if self.itemAt(index) is not None:
            return self.items.pop(index)
        return None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), place=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize(0, 0)
        for item in self.items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect, *, place):
        margins = self.contentsMargins()
        area = rect.marginsRemoved(margins)
        x, y, row_height = area.x(), area.y(), 0
        spacing = self.spacing()
        for item in self.items:
            if item.isEmpty():
                continue
            size = item.sizeHint()
            if row_height and x + size.width() > area.x() + area.width():
                x = area.x()
                y += row_height + spacing
                row_height = 0
            if place:
                item.setGeometry(QRect(x, y, size.width(), size.height()))
            x += size.width() + spacing
            row_height = max(row_height, size.height())
        return y - rect.y() + row_height + margins.bottom()
