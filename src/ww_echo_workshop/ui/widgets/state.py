# 保存页面选择动作和诊断投影而不构造隐藏控件。
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextDocument


def update_text(widget, text):
    """Avoid redundant label relayout/stylesheet events on unchanged polling."""
    if widget.text() != text:
        widget.setText(text)


def update_tooltip(widget, text):
    if widget.toolTip() != text:
        widget.setToolTip(text)


class ActionState:
    """Nonvisual action availability; actual buttons bind only where displayed."""

    def __init__(self, text=''):
        self._text, self.enabled, self.visible, self.tooltip = text, True, False, ''

    def setEnabled(self, value):
        self.enabled = bool(value)

    def isEnabled(self):
        return self.enabled

    def setVisible(self, value):
        self.visible = bool(value)

    def hide(self):
        self.visible = False

    def show(self):
        self.visible = True

    def setText(self, text):
        self._text = str(text)

    def text(self):
        return self._text

    def setToolTip(self, text):
        self.tooltip = str(text)


class TextState(ActionState):
    """Diagnostic text remains queryable without a hidden QTextEdit."""

    def __init__(self, text=''):
        super().__init__(text)
        self._document = QTextDocument()
        self._document.setPlainText(text)

    def setPlainText(self, text):
        self.setText(text)
        self._document.setPlainText(str(text))

    def toPlainText(self):
        return self.text()

    def clear(self):
        self.setPlainText('')

    def document(self):
        return self._document


class RecordPreviewState(TextState):
    """Selected record metadata, without an undisplayed card or image read."""

    def __init__(self):
        super().__init__()
        self.record = None

    def set_record(self, record):
        self.record = record

    def clear(self):
        self.record = None
        super().clear()


class ChoiceState(QObject):
    """Stable identifiers and current selection, independent of widget lifetime."""

    currentIndexChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.index, self.enabled = [], -1, True

    def count(self):
        return len(self.rows)

    def addItem(self, text, data=None, *, userData=None):
        self.rows.append((str(text), data if userData is None else userData))
        if self.index < 0:
            self.setCurrentIndex(0)

    def addItems(self, labels):
        for label in labels:
            self.addItem(label)

    def clear(self):
        self.rows = []
        self.setCurrentIndex(-1)

    def removeItem(self, index):
        if 0 <= index < len(self.rows):
            del self.rows[index]
            self.setCurrentIndex(min(self.index, len(self.rows) - 1))

    def currentIndex(self):
        return self.index

    def setCurrentIndex(self, index):
        index = index if 0 <= index < len(self.rows) else -1
        if index != self.index:
            self.index = index
            self.currentIndexChanged.emit(index)

    def currentData(self):
        return self.itemData(self.index)

    def itemData(self, index):
        return self.rows[index][1] if 0 <= index < len(self.rows) else None

    def itemText(self, index):
        return self.rows[index][0] if 0 <= index < len(self.rows) else ''

    def currentText(self):
        return self.itemText(self.index)

    def setItemText(self, index, text):
        if 0 <= index < len(self.rows):
            self.rows[index] = (str(text), self.rows[index][1])

    def findData(self, data):
        return next((i for i, row in enumerate(self.rows) if row[1] == data), -1)

    def setEnabled(self, value):
        self.enabled = bool(value)

    def isEnabled(self):
        return self.enabled


class ToggleState(QObject):
    toggled = Signal(bool)

    def __init__(self, parent=None, checked=False):
        super().__init__(parent)
        self.checked, self.enabled, self.visible = bool(checked), True, False

    def isChecked(self):
        return self.checked

    def setChecked(self, value):
        value = bool(value)
        if value != self.checked:
            self.checked = value
            self.toggled.emit(value)

    def setEnabled(self, value):
        self.enabled = bool(value)

    def setVisible(self, value):
        self.visible = bool(value)


class TableState(QObject):
    """Result cell metadata and selected row, not an offscreen table widget."""

    itemSelectionChanged = Signal()

    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.columns, self.rows, self.row, self.column = columns, [], -1, 0

    def rowCount(self):
        return len(self.rows)

    def setRowCount(self, count):
        self.rows = self.rows[:count] + [
            [None] * self.columns for _ in range(max(0, count - len(self.rows)))
        ]
        if self.row >= count:
            self.clearSelection()

    def item(self, row, column):
        return (
            self.rows[row][column]
            if 0 <= row < len(self.rows) and 0 <= column < self.columns
            else None
        )

    def setItem(self, row, column, item):
        self.rows[row][column] = item

    def currentRow(self):
        return self.row

    def currentItem(self):
        return self.item(self.row, self.column)

    def setCurrentCell(self, row, column):
        self.row, self.column = row if 0 <= row < len(self.rows) else -1, column
        self.itemSelectionChanged.emit()

    def clearSelection(self):
        self.row = -1
        self.itemSelectionChanged.emit()
