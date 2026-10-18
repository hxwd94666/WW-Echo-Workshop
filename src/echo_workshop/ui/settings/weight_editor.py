# 展示当前权重和更新状态。
"""Small main-thread update dashboard; all HTTP lives in the shared worker."""

from datetime import datetime
import time

from PySide6.QtCore import QTimer, QEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QPlainTextEdit,
    QWidget,
    QProgressBar,
)

from echo_workshop.resources.weights import ALGORITHM_VERSION
from echo_workshop.ui.settings.help import add_diagnostics, job_text


def _time(value):
    return (
        datetime.fromtimestamp(value).astimezone().strftime('%Y-%m-%d %H:%M:%S')
        if value
        else '尚无记录'
    )


class _InfoView(QPlainTextEdit):
    # Keep existing read-only text access for task/UI integration callers.
    def text(self):
        return self.toPlainText()

    def setText(self, value):
        # A timer refresh of unchanged text must not reset the user's scroll.
        if value != self.toPlainText():
            self.setPlainText(value)


class WeightContent:
    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task, self.runtime = task, task.runtime
        self.setWindowTitle('评分权重 · XWUID')
        self.resize(900, 460)
        layout = QVBoxLayout(self)
        self.resource_controls = QWidget(self)
        resources = QVBoxLayout(self.resource_controls)
        resources.setContentsMargins(0, 0, 0, 0)
        self.auto = QCheckBox('自动检查资料更新')
        self.auto.setToolTip(
            '低频后台检查权重、角色属性、套装和头像；不开页面也可检查，不上传库存或截图。'
        )
        self.auto.setChecked(bool(task.config.get('AutoWeightUpdates', True)))
        self.auto.toggled.connect(self.set_auto)
        self.update_options = QHBoxLayout()
        self.update_options.addWidget(self.auto)
        resources.addLayout(self.update_options)
        buttons = QHBoxLayout()
        self.check_button = QPushButton('检查更新')
        self.check_button.setToolTip(
            '统一检查权重、角色与套装资料；离线继续使用已有资料，自定义权重保留。'
        )
        self.check_button.clicked.connect(self.check)
        buttons.addWidget(self.check_button)
        self.cancel_update_button = QPushButton('取消更新')
        self.cancel_update_button.clicked.connect(self.cancel_update)
        self.cancel_update_button.hide()
        buttons.addWidget(self.cancel_update_button)
        resources.addLayout(buttons)
        self.info = _InfoView()
        self.info.setReadOnly(True)
        self.info.setMinimumHeight(100)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        resources.addWidget(self.summary)
        self.progress_bar = QProgressBar(self.resource_controls)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        resources.addWidget(self.progress_bar)
        self.diagnostics = add_diagnostics(resources, self, self.info, '资料更新诊断')
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self._check_feedback = False
        resources.addWidget(self.feedback)
        layout.addWidget(self.resource_controls)
        self.advanced_controls = QWidget(self)
        advanced = QVBoxLayout(self.advanced_controls)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.allow_removals = QCheckBox('移除失效模版')
        self.allow_removals.setToolTip(
            '只影响下次明确检查；旧方案和自定义权重保留，失效模版需重新选择。'
        )
        self.update_options.addWidget(self.allow_removals)
        self.update_options.addStretch(1)
        self.reload_button = QPushButton('重新读取本机权重')
        self.reload_button.clicked.connect(self.reload_local)
        advanced.addWidget(self.reload_button)
        self.advanced_feedback = QLabel()
        self.advanced_feedback.setWordWrap(True)
        advanced.addWidget(self.advanced_feedback)
        layout.addWidget(self.advanced_controls)
        self.custom_editor = None
        if not getattr(self, '_embedded_settings', False):
            self.ensure_custom_editor()
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.resource_controls.installEventFilter(self)
        self.refresh()

    def eventFilter(self, watched, event):
        # Data controls are reparented out of the hidden Calculation page.
        # Their visibility, not the editor's visibility, must drive polling.
        if watched is self.resource_controls:
            if event.type() == QEvent.Show:
                self.timer.start()
                self.refresh()
            elif event.type() == QEvent.Hide and not self.isVisible():
                self.timer.stop()
        return super().eventFilter(watched, event)

    def ensure_custom_editor(self):
        if self.custom_editor is None:
            from echo_workshop.ui.settings.weights import CustomWeightEditor

            self.custom_editor = CustomWeightEditor(self.task, self)
            self.layout().addWidget(self.custom_editor)
        return self.custom_editor

    def cancel_update(self):
        service = getattr(self.runtime, 'resources', None)
        if service is not None:
            service.cancel()
            self.feedback.setText('正在取消更新…')
            self.refresh()

    def set_auto(self, enabled):
        if not self.runtime._closed:
            self.task.config['AutoWeightUpdates'] = bool(enabled)
            if service := getattr(self.runtime, 'resources', None):
                service.set_auto(enabled)
            else:
                self.runtime.updates.set_auto(enabled)
                if auxiliary := getattr(self.runtime, 'sonata_updates', None):
                    auxiliary.start(auto_enabled=enabled)
                if effects := getattr(self.runtime, 'sonata_effect_updates', None):
                    effects.start(auto_enabled=enabled)
            self.refresh()

    def check(self):
        if not self.runtime._closed:
            service = getattr(self.runtime, 'resources', None)
            accepted = (service or self.runtime.updates).request(
                allow_removals=self.allow_removals.isChecked()
            )
            if service is None:
                if auxiliary := getattr(self.runtime, 'sonata_updates', None):
                    auxiliary.request()
                if effects := getattr(self.runtime, 'sonata_effect_updates', None):
                    effects.request()
            self._check_feedback = accepted
            self.feedback.setText(
                '正在检查资料更新。' if accepted else '已有操作正在进行，请稍候。'
            )
            self.refresh()

    def reload_local(self):
        if not self.runtime._closed:
            accepted = self.runtime.updates.request_reload()
            self._check_feedback = accepted
            self.feedback.setText(
                '正在读取本机权重。' if accepted else '已有操作正在进行，请稍候。'
            )
            self.refresh()

    def refresh(self):
        if self.runtime._closed or self.task._destroyed:
            self.close()
            return
        state = self.runtime.updates.status
        snapshot = self.runtime.weights.active
        if self.custom_editor is not None:
            self.custom_editor.refresh()
        progress_text = ''
        if state.operation == 'check' and state.state == 'checking':
            elapsed = max(0, int(time.time() - state.started_at)) if state.started_at else 0
            limit = getattr(self.runtime.updates.client, 'deadline', 300)
            progress_text = f'\n{state.message}\n已用 {elapsed} 秒 · 网络检查预算 {int(limit)} 秒'
        if self._check_feedback and state.state not in ('checking', 'queued'):
            self.feedback.clear()
            self._check_feedback = False
        explanation = {
            'failed': '更新未完成，继续使用已有权重。可稍后重试，或查看诊断信息。',
            'deferred': '这次更新会移除旧模版，请核对后勾选上方选项，再检查更新。',
            'busy': '其他操作正在使用权重，请稍后重试。',
        }.get(state.state, '')
        self.summary.setText(
            job_text(state.state)
            + '\n最近检查：'
            + _time(state.last_checked)
            + ('\n' + explanation if explanation else '')
        )
        if state.operation == 'check' and '个角色属性未更新' in state.message:
            self.summary.setText(self.summary.text() + '\n' + state.message.split('；')[-1])
        if auxiliary := getattr(self.runtime, 'sonata_updates', None):
            auxiliary_state = auxiliary.status
            auxiliary_text = {
                'idle': '套装资料离线可用',
                'queued': '套装资料待检查',
                'checking': '正在检查套装资料',
                'current': '套装资料已是最新',
                'updated': '套装资料已更新',
                'failed': '套装资料检查未完成，继续使用已有资料',
                'stopped': '套装资料检查已停止',
            }.get(auxiliary_state.state, '套装资料待核对')
            if auxiliary_state.state in ('failed', 'checking', 'queued'):
                self.summary.setText(self.summary.text() + '\n' + auxiliary_text)
        checked_source = (
            state.source
            if state.last_success
            else '尚无；预设 ww3 → ww1 → ww2；直连（不继承 HTTP 代理）'
        )
        attempts = '\n'.join(
            f'{row.source} · {row.state} · {row.requests} 请求'
            + ((' · ' + row.error[:160]) if row.error else '')
            for row in state.attempts
        )
        details = (
            f'当前快照来源：{snapshot.source}\n最近完整校验来源：{checked_source}\n活动快照：{snapshot.version}\n模版：{len(snapshot.options)}；算法：{ALGORITHM_VERSION}\n'
            f'最近检查：{_time(state.last_checked)}；最近成功检查：{_time(state.last_success)}\n'
            f'下次自动检查不早于：{_time(state.next_check)}（自动检查关闭时不执行）\n'
            f'状态：{state.state}；{state.message}\n本次文件 {state.files} / 请求 {state.requests}\n来源尝试：\n{attempts or "尚无"}'
            f'\n自定义角色：{len(self.runtime.weights.customized_characters)}；角色属性：{len(snapshot.character_elements)}'
            + progress_text
        )
        if auxiliary := getattr(self.runtime, 'sonata_updates', None):
            details += (
                f'\n套装资料：{auxiliary.status.state}；{auxiliary.status.message}'
                f'\n套装快照：{self.runtime.sonata.active.version}；数量：{len(self.runtime.sonata.active.icons)}'
            )
        if effects := getattr(self.runtime, 'sonata_effect_updates', None):
            if effects.status.state in ('failed', 'checking', 'queued'):
                self.summary.setText(self.summary.text() + '\n' + effects.status.message)
            details += f'\n套装效果：{effects.status.state}；{effects.status.message}\n效果快照：{self.runtime.sonata_effects.active.version}'
        service = getattr(self.runtime, 'resources', None)
        resource_state = service.status if service is not None else None
        busy = state.state in ('checking', 'queued')
        if resource_state is not None:
            busy = busy or resource_state.state in ('checking', 'queued')
            terminal = {
                'idle': '已有资料离线可用',
                'queued': '等待检查',
                'checking': '正在检查资料',
                'current': '资料已是最新',
                'updated': '资料更新完成',
                'partial': '部分资料未更新，已有资料保留',
                'failed': '检查未完成，已有资料保留',
                'cancelled': '资料检查已取消',
            }
            label = terminal.get(resource_state.state, '资料状态待确认')
            message = resource_state.message.strip()
            status_text = label if not message or message == label else label + ' · ' + message
            self.summary.setText(label + '\n最近检查：' + _time(resource_state.last_checked))
            if resource_state.state in ('partial', 'failed'):
                self.summary.setText(self.summary.text() + '\n可稍后重试，或查看诊断信息。')
            details += '\n资料摘要：' + status_text
            details += (
                '\n统一资料作业：' + resource_state.operation_id + '；' + resource_state.state
            )
            for part in resource_state.parts:
                details += f'\n{part.channel}：{part.state}；{part.message}；版本 {part.version}'
        if state.operation == 'reload':
            self.advanced_feedback.setText(
                '本机权重 · ' + job_text(state.state) + ' · ' + state.message
            )
        self.summary.setToolTip('')
        updating = resource_state is not None and resource_state.state in ('checking', 'queued')
        self.progress_bar.setVisible(updating)
        if not updating and self.feedback.text() == '正在取消更新…':
            self.feedback.clear()
        if updating:
            total = max(1, resource_state.total)
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(resource_state.completed)
            elapsed = (
                max(0, int(time.time() - resource_state.started_at))
                if resource_state.started_at
                else 0
            )
            part = resource_state.part(resource_state.channel)
            detail = part.message if part.state == 'checking' else ''
            if resource_state.channel == 'avatars':
                detail = f'已检查 {part.files} 张头像'
            self.summary.setText(
                f'步骤 {min(resource_state.completed + 1, total)} / {total} · {resource_state.message}'
                + (f'\n{detail}' if detail else '')
                + f'\n已用 {elapsed} 秒'
            )
            if self._check_feedback:
                self.feedback.clear()
        self.info.setText(details)
        self.check_button.setEnabled(not busy)
        self.allow_removals.setEnabled(not busy)
        self.reload_button.setEnabled(not busy)
        self.cancel_update_button.setVisible(
            resource_state is not None and resource_state.state in ('checking', 'queued')
        )

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def closeEvent(self, event):
        self.timer.stop()
        self.diagnostics.close()
        super().closeEvent(event)


class WeightWindow(WeightContent, QDialog):
    """Compatibility dialog; the embedded settings reuse QWidget content."""

    pass
