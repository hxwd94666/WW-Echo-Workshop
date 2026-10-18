# 提供稳定滚轮行为和可搜索输入控件。
"""Plugin-owned controls: wheel scrolls the page, search preserves item IDs."""

from PySide6.QtCore import QEvent, QIdentityProxyModel, QPointF, QStringListModel, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QComboBox as NativeCombo,
    QCompleter,
    QSpinBox as NativeSpin,
    QDoubleSpinBox as NativeDoubleSpin,
    QAbstractScrollArea,
    QApplication,
)
from qfluentwidgets import ComboBox as FluentCombo, EditableComboBox as FluentEditable
from echo_workshop.resources.search import search_keys

PINYIN_ROLE = int(Qt.UserRole) + 432


class SearchKeysModel(QIdentityProxyModel):
    def __init__(self, parent=None, search_role=Qt.DisplayRole):
        super().__init__(parent)
        self.search_role = search_role

    def data(self, index, role=Qt.DisplayRole):
        if role == PINYIN_ROLE:
            source = self.mapToSource(index)
            return search_keys(str(source.data(self.search_role) or ''))
        return super().data(index, role)


class PinyinCompleter(QCompleter):
    def __init__(self, model, parent=None, search_role=Qt.DisplayRole):
        super().__init__(parent)
        self._keys = SearchKeysModel(self, search_role)
        self.setModel(model)
        self.setCompletionRole(PINYIN_ROLE)
        self.setCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterMode(Qt.MatchContains)
        self.setCompletionMode(QCompleter.PopupCompletion)

    def setModel(self, model):
        if isinstance(model, (tuple, list)):
            model = QStringListModel(list(model), self)
        self._keys.setSourceModel(model)
        super().setModel(self._keys)

    def pathFromIndex(self, index):
        # Matching keys must never be inserted into a field or persisted as IDs.
        return str(index.data(Qt.DisplayRole) or '')


def combo_search_draft(box):
    """Describe an unsubmitted search draft so passive refreshes keep it.

    An editable combo box shows the selected item's label as its text. Anything
    else — including half-typed pinyin — is a user draft; a rebuild must not
    overwrite it. Returns ``None`` when the box simply shows its selection.
    """
    edit = box.lineEdit()
    if edit is None:
        return None
    text = box.currentText()
    index = box.currentIndex()
    if not (bool(text) or edit.hasFocus()):
        return None
    if index >= 0 and text == box.itemText(index):
        return None
    return text, edit.cursorPosition(), edit.selectionStart(), len(edit.selectedText())


def restore_combo_search(box, draft):
    """Re-apply a draft after items or the current index were rebuilt."""
    if draft is None:
        return False
    edit = box.lineEdit()
    if edit is None:
        return False
    text, cursor, start, length = draft
    blocked = box.blockSignals(True)
    box.setEditText(text)
    edit.setCursorPosition(cursor)
    if start >= 0:
        edit.setSelection(start, length)
    box.blockSignals(blocked)
    return True


class _NoWheel:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._watch_editor()

    def _watch_editor(self):
        editor = getattr(self, 'lineEdit', lambda: None)()
        self._wheel_editor = editor
        if editor is not None:
            editor.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is getattr(self, '_wheel_editor', None) and event.type() == QEvent.Wheel:
            self.wheelEvent(event)
            return True
        return super().eventFilter(watched, event)

    def wheelEvent(self, event):
        # Explicitly hand the gesture to its scroll viewport. Merely ignoring
        # it can swallow wheel input when a focused editor owns the gesture.
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(
                    QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                    event.globalPosition(),
                    event.pixelDelta(),
                    event.angleDelta(),
                    event.buttons(),
                    event.modifiers(),
                    event.phase(),
                    event.inverted(),
                )
                QApplication.sendEvent(viewport, forwarded)
                if forwarded.isAccepted():
                    event.accept()
                    return
            parent = parent.parentWidget()
        event.ignore()


class QComboBox(_NoWheel, NativeCombo):
    automatic_pinyin_completion = True

    def setEditable(self, editable):
        super().setEditable(editable)
        self._watch_editor()
        if editable and self.automatic_pinyin_completion:
            self.setCompleter(PinyinCompleter(self.model(), self))


class QSpinBox(_NoWheel, NativeSpin):
    pass


class QDoubleSpinBox(_NoWheel, NativeDoubleSpin):
    pass


class _StableComboMenu:
    """Fluent's text lookup aliases same-name items; bind the actual row instead."""

    def clear(self):
        self._closeComboMenu()
        super().clear()

    def setItemText(self, index, text):
        self._closeComboMenu()
        super().setItemText(index, text)


class ComboBox(_NoWheel, _StableComboMenu, FluentCombo):
    pass


class EditableComboBox(_NoWheel, _StableComboMenu, FluentEditable):
    pass
