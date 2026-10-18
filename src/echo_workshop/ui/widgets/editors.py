# 复用内嵌核对和角色要求编辑器。
"""Page-local editors; validation shared with legacy dialogs, no exec loop."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialogButtonBox, QWidget
from echo_workshop.ui.inventory.editor import RecordEditorContent
from echo_workshop.ui.allocation.editor import RoleEditorContent


class InlineRecordEditor(RecordEditorContent, QWidget):
    _structured_cards = True
    saveRequested = Signal()
    cancelled = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.require_confirmation:
            self.acknowledge.setText('已核对以上信息，清除识别警告')
            self.acknowledge.setToolTip('可对照游戏画面或原始截图；数值仍需通过校验。')
            buttons = self.findChild(QDialogButtonBox)
            buttons.button(QDialogButtonBox.Save).setText('保存并确认')
            buttons.button(QDialogButtonBox.Cancel).setText('取消')

    def accept(self):
        self.saveRequested.emit()

    def reject(self):
        self.cancelled.emit()


class InlineRoleEditor(RoleEditorContent, QWidget):
    _structured_cards = True
    saveRequested = Signal()
    cancelled = Signal()

    def accept(self):
        self.saveRequested.emit()

    def reject(self):
        if self.confirm_return():
            self.cancelled.emit()
            return True
        return False
