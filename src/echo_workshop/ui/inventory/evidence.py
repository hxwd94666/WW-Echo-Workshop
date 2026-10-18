# 查看声骸原始截图证据。
"""Explicit original-image viewer, with off-thread read and QImage decoding."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QScrollArea, QVBoxLayout
from echo_workshop.ui.widgets.layout import fit_dialog


def decode_image(data):
    image = QImage.fromData(data, 'PNG')
    if image.isNull():
        raise ValueError('截图解码失败')
    return image


class EvidenceDialog(QDialog):
    def __init__(self, reference, evidence, parent=None):
        super().__init__(parent)
        self.setWindowTitle('原始截图（滚动查看；不是当前游戏状态）')
        fit_dialog(self, 1000, 700)
        layout = QVBoxLayout(self)
        self.status = QLabel('正在读取原图…')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.area = QScrollArea()
        self.area.setWidgetResizable(False)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.area.setWidget(self.image)
        layout.addWidget(self.area, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.service = evidence.views
        self.job = self.service.submit(reference, decode_image)
        # Only plain job state is captured: parent destruction must not call Qt.
        self.destroyed.connect(lambda *_, job=self.job: job.cancel())
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    def _poll(self):
        if self.service.closed:
            self.reject()
            return
        job = self.job
        if job is None or not job.future.done():
            return
        self.timer.stop()
        self.job = None
        if job.cancelled.is_set() or job.future.cancelled():
            self.reject()
            return
        try:
            image = job.future.result()
            if image is not None:
                pixmap = QPixmap.fromImage(image)
                if pixmap.isNull():
                    raise ValueError('截图显示资源创建失败')
                self.image.setPixmap(pixmap)
                # The scroll child was installed while empty; its geometry does
                # not follow a later asynchronous pixmap's size hint by itself.
                self.image.adjustSize()
                self.status.setText('原始截图 · 内容未经修改')
        except (OSError, ValueError, RuntimeError) as error:
            self.status.setText('原图读取失败；原证据引用保留。\n' + str(error))
            self.status.setToolTip(str(error))

    def done(self, result):
        self.timer.stop()
        if self.job is not None:
            self.job.cancel()
            self.job = None
        super().done(result)


def open_evidence(record, evidence, parent):
    if not record.screenshot_ref or evidence is None:
        raise ValueError('没有可核对的原始截图')
    dialog = EvidenceDialog(record.screenshot_ref, evidence, parent)
    try:
        return dialog.exec()
    finally:
        dialog.done(dialog.result())
        dialog.deleteLater()
