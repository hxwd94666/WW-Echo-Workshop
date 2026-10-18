# 展示全量截图清理入口与后台清理反馈，危险动作需要用户确认。
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QMessageBox
from qfluentwidgets import PushButton


class ScreenshotSettings(QWidget):
    def __init__(self, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel('自动清理已移出声骸库的截图，保留所有档案中在库及待核对声骸的截图。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.clear_button = PushButton('清理全部声骸截图', self)
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(self.clear_button)
        self.feedback = QLabel(self)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.refresh)

    def clear(self):
        if (
            QMessageBox.question(
                self,
                '清理全部声骸截图',
                '删除当前工作区所有档案的声骸原始截图？此操作不可撤销。\n'
                '声骸数据、评分、配装与已导出的备份保留；新收录的截图仍会保存。',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self.runtime.stop_intake()
        if not self.runtime.screenshot_cleanup.request_all():
            self.feedback.setText('截图清理正在进行，请稍后重试。')
        else:
            self.refresh()

    def refresh(self):
        state = self.runtime.screenshot_cleanup.status
        self.feedback.setText(state.message)
        self.clear_button.setEnabled(state.state not in ('queued', 'running'))

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)
