# 展示控制记录修复预览并交由应用服务执行。
"""One emergency repair form, also available before runtime initialization."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from echo_workshop.application.repair import RepairService
from echo_workshop.ui.widgets.inputs import QComboBox
from echo_workshop.ui.settings.help import add_diagnostics


class ControlRepairContent:
    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        runtime = task.runtime
        self.service = RepairService(
            task.config.config_file,
            stop_event=runtime.stop_event if runtime is not None else None,
            compute=runtime.compute if runtime is not None else None,
        )
        self.preview = None
        self.closed = False
        self._rendered_receipt = None
        self.setWindowTitle('启动故障恢复')
        self.resize(920, 560)
        layout = QVBoxLayout(self)
        notice = QLabel(
            '用于处理数据副本或设置恢复记录损坏造成的启动问题。先预览，再确认修复。\n'
            '声骸、截图、配装方案和当前设置不变。原文件会备份，修复后请重启 OKWW。'
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.state_label = QLabel(
            '插件启动遇到问题，请先预览。'
            if task.startup_error
            else '仅在遇到启动故障时使用此工具。'
        )
        self.state_label.setToolTip(task.startup_error)
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        row = QHBoxLayout()
        self.kinds = QComboBox()
        self.kinds.addItem('数据副本切换记录', 'selection')
        self.kinds.addItem('插件设置恢复记录', 'preferences')
        row.addWidget(self.kinds)
        self.targets = QComboBox()
        row.addWidget(self.targets, 1)
        self.refresh_button = QPushButton('刷新数据副本')
        self.refresh_button.clicked.connect(self.reload)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.user_details = QTextEdit()
        self.user_details.setReadOnly(True)
        layout.addWidget(self.user_details, 1)
        self.user_details.setPlainText('选择需要修复的记录，点击“预览修复”。确认前不会改动文件。')
        actions = QHBoxLayout()
        self.preview_button = QPushButton('预览修复')
        self.preview_button.clicked.connect(self.inspect)
        actions.addWidget(self.preview_button)
        self.apply_button = QPushButton('确认修复')
        self.apply_button.clicked.connect(self.apply)
        actions.addWidget(self.apply_button)
        self.cancel_button = QPushButton('取消操作')
        self.cancel_button.clicked.connect(self.service.cancel)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.diagnostics = add_diagnostics(layout, self, self.details, '启动恢复诊断')
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.kinds.currentIndexChanged.connect(self.invalidate)
        self.targets.currentIndexChanged.connect(self.invalidate)
        self.reload()
        self.timer.start()

    @property
    def future(self):
        """The shell can observe busy state without owning the repair executor."""
        return self.service.future

    def _accepting(self):
        return not self.closed and not self.task._destroyed and self.service.accepting

    def invalidate(self):
        if self.future is not None:
            return
        self.service.invalidate_preview()
        self.preview = None
        self.apply_button.setEnabled(False)
        self.targets.setEnabled(self._accepting() and self.kinds.currentData() == 'selection')

    def reload(self):
        if self.future is not None or not self._accepting():
            return
        try:
            selected = self.targets.currentData()
            candidates = self.service.candidates()
            blocked = self.targets.blockSignals(True)
            try:
                self.targets.clear()
                for number, target in enumerate(candidates, 1):
                    self.targets.addItem(f'数据副本 {number}' if target else '默认声骸库', target)
                self.targets.setCurrentIndex(max(0, self.targets.findData(selected)))
            finally:
                self.targets.blockSignals(blocked)
            self.invalidate()
        except (ValueError, OSError) as error:
            self.status_label.setText(str(error))
        self.poll()

    def inspect(self):
        if self.future is not None or not self._accepting():
            return
        self.invalidate()
        kind = self.kinds.currentData()
        target = self.targets.currentData() if kind == 'selection' else ''
        try:
            self.service.request_preview(kind, target)
        except (ValueError, OSError) as error:
            self.status_label.setText(str(error))
        self.poll()

    def apply(self):
        if self.preview is None or self.future is not None or not self._accepting():
            return
        answer = QMessageBox.question(
            self,
            '确认修复启动记录',
            '修复会清除损坏的切换计划或设置恢复记录，原文件会备份。\n'
            '声骸库、方案和当前设置不变。当前收录会停止，修复后需重启。\n'
            '请先关闭其他 OKWW 实例。确定继续？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        runtime = self.task.runtime
        if runtime is not None and not runtime._closed:
            if runtime.backups.status.busy:
                self.status_label.setText('先完成或取消库存/工作区后台作业，再修复')
                return
            runtime.capture.stop()
        try:
            self.service.request_apply(self.preview)
            self.preview = None
        except (ValueError, OSError) as error:
            self.status_label.setText(str(error))
        self.poll()

    def _render_status(self, status):
        self.status_label.setText(status.message)
        if status.preview is not None:
            result = self.preview = status.preview
            self.details.setPlainText(
                f'记录：{result.kind}\n故障：{result.reason}\n原文件：{result.source_bytes} bytes\n'
                f'原 SHA256：{result.source_hash}\n修复目标：{result.target or "原始工作区 / 当前 Config 不变"}\n'
                f'校验文件：{len(result.target_files)}\n替换记录：\n{result.replacement.decode("utf-8")}\n\n'
                '原文件将完整隔离保存；此处不执行旧预约。确认前请核对丢失的预约/回退历史。'
            )
            self.user_details.setPlainText(
                '将修复：'
                + self.kinds.currentText()
                + '\n'
                + (
                    '重启后使用：' + self.targets.currentText() + '\n'
                    if result.kind == 'selection'
                    else ''
                )
                + '损坏的切换计划或设置恢复记录会清除。\n'
                '声骸、截图、配装方案和当前设置不变。\n原文件会保留备份；确认后需重启。'
            )
        elif status.result is not None:
            result = status.result
            self.details.append(
                '\n隔离证据：' + result.archive + '\n新 SHA256：' + result.replacement_hash
            )
            self.user_details.append('\n修复完成。请关闭其他 OKWW 实例，然后重启。')
            self.status_label.setToolTip(result.message)
        elif status.state in ('failed', 'cancelled'):
            self.preview = None

    def poll(self):
        status = self.service.poll()
        receipt = (status.job_id, status.state)
        if receipt != self._rendered_receipt:
            self._rendered_receipt = receipt
            self._render_status(status)
        busy = status.busy
        active = self._accepting()
        for widget in (self.kinds, self.refresh_button, self.preview_button):
            widget.setEnabled(not busy and active)
        self.targets.setEnabled(not busy and active and self.kinds.currentData() == 'selection')
        self.apply_button.setEnabled(not busy and active and self.preview is not None)
        self.cancel_button.setEnabled(busy)

    def stop_worker(self):
        """Service cancellation/join is independent from Qt event delivery."""
        self.service.close()

    def closeEvent(self, event):
        self.stop_worker()
        self.poll()
        self.closed = True
        self.timer.stop()
        self.diagnostics.close()
        if getattr(self.task, 'control_repair_window', None) is self:
            self.task.control_repair_window = None
        super().closeEvent(event)


class ControlRepairWindow(ControlRepairContent, QDialog):
    """The embedded settings and standalone fallback share this exact form."""

    pass
