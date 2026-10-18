# 编辑已启用的快捷键配置。
"""Main-thread, opt-in shortcut configuration, no implicit collection start."""

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout
from echo_workshop.application.hotkeys import (
    ACTIONS,
    KEY_CODES,
    encode_bindings,
    parse_bindings,
    parse_chord,
)


class ShortcutRecorder(QLineEdit):
    """Record one local Qt chord, without registering or polling global keys."""

    recorded = Signal(str)
    rejected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText('点击设置快捷键')
        self.setToolTip(
            '支持字母、主键盘数字、F1–F12，可加 Ctrl/Alt/Shift。Delete 清除，Esc 保留，Tab 切换。'
        )

    def event(self, event):
        # Alt/function keys must reach this editor before a parent shortcut or
        # dialog default button handles them. Tab retains normal focus traversal.
        if event.type() == QEvent.ShortcutOverride and self.hasFocus():
            if event.key() not in (Qt.Key_Tab, Qt.Key_Backtab):
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        key, modifiers = event.key(), event.modifiers()
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            return super().keyPressEvent(event)
        event.accept()
        if event.isAutoRepeat() or key in (
            Qt.Key_Control,
            Qt.Key_Alt,
            Qt.Key_Shift,
            Qt.Key_Meta,
            Qt.Key_AltGr,
        ):
            return
        if key == Qt.Key_Escape:
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete) and modifiers == Qt.NoModifier:
            self.clear()
            self.recorded.emit('')
            return
        if Qt.Key_F1 <= key <= Qt.Key_F12:
            key_name = f'F{key - Qt.Key_F1 + 1}'
        elif Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
            key_name = chr(key)
        else:
            key_name = ''
        if key_name not in KEY_CODES or modifiers & (
            Qt.MetaModifier | Qt.GroupSwitchModifier | Qt.KeypadModifier
        ):
            self.rejected.emit('请选择字母、主键盘数字或 F1–F12；原快捷键保留')
            return
        names = [
            name
            for name, mask in (
                ('Ctrl', Qt.ControlModifier),
                ('Alt', Qt.AltModifier),
                ('Shift', Qt.ShiftModifier),
            )
            if modifiers & mask
        ]
        try:
            chord = parse_chord('+'.join((*names, key_name)))
        except ValueError as error:
            self.rejected.emit(str(error) + '；原快捷键保留')
            return
        self.setText(chord.text)
        self.recorded.emit(chord.text)


class ShortcutContent:
    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task, self.runtime = task, task.runtime
        self.setWindowTitle('收录快捷键')
        self.resize(800, 390)
        layout = QVBoxLayout(self)
        self.notice = QLabel(
            '点击输入框录入快捷键，再保存。逐件收录需先点击“开始收录”；连续浏览收录会自动保存稳定识别的声骸。'
        )
        self.notice.setToolTip(
            '支持 F8、K、Ctrl+K 等；Delete/Backspace 清除。仅在鸣潮窗口中生效，建议使用组合键避开游戏按键。'
        )
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.fields = {}
        self.feedback = QLabel()
        form = QFormLayout()
        for action, label in ACTIONS.items():
            field = ShortcutRecorder(self)
            field.setAccessibleName(label + '快捷键录制')
            field.recorded.connect(
                lambda text, name=label: self.feedback.setText(
                    f'{name}：'
                    + (f'已录入 {text}' if text else '已清除')
                    + '，点击“保存快捷键”生效。'
                )
            )
            field.rejected.connect(self.feedback.setText)
            self.fields[action] = field
            form.addRow(label, field)
        layout.addLayout(form)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        try:
            saved = parse_bindings(task.config.get('CaptureHotkeysJSON', ''))
            for action, chord in saved.items():
                self.fields[action].setText(chord.text)
        except ValueError as error:
            self.feedback.setText(str(error))
        apply = QPushButton('保存快捷键')
        apply.setToolTip('只保存快捷键，不会自动开始收录。')
        apply.clicked.connect(self.apply)
        layout.addWidget(apply)
        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def apply(self):
        if self.runtime._closed:
            return
        try:
            raw = encode_bindings({action: field.text() for action, field in self.fields.items()})
            self.task.apply_shortcuts(raw)
            for action, chord in parse_bindings(raw).items():
                self.fields[action].setText(chord.text)
            self.feedback.setText('快捷键已保存。')
        except (ValueError, OSError) as error:
            self.feedback.setText('设置未完成：' + str(error))
        self.refresh()

    def refresh(self):
        if self.runtime._closed or self.task._destroyed:
            self.close()
            return
        label = self.runtime.inventory.profiles().get(
            self.task.config.get('InventoryProfileID', ''), '未选择有效档案'
        )
        try:
            enabled = bool(parse_bindings(self.task.config.get('CaptureHotkeysJSON', '')))
        except ValueError:
            enabled = False
        state = self.runtime.capture.status.state
        shown = {
            'stopped': '未开始',
            'active': '收录中',
            'paused': '已暂停',
            'single': '正在收录当前声骸',
            'armed': '等待收录快捷键',
        }.get(state, '未开始')
        service = self.runtime.shortcuts
        lines = [
            f'当前档案：{label} · 收录：{shown}',
            service.message if service else ('快捷键尚未启动' if enabled else '未设置快捷键'),
        ]
        if self.task.shortcut_message:
            lines.append(self.task.shortcut_message)
        self.info.setText('\n'.join(lines))
        self.info.setToolTip('')

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


class ShortcutWindow(ShortcutContent, QDialog):
    """Compatibility dialog; the embedded settings reuse QWidget content."""

    pass
