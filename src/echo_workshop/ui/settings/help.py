# 提供设置的简洁中文说明。
"""User wording and opt-in diagnostics, without changing persisted keys."""

from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout, QWidget
from echo_workshop.application.hotkeys import ACTIONS, parse_bindings

SETTING_NAMES = {
    '启用声骸评分': '显示实时评分',
    '自动匹配评分模板': '自动选择评分角色',
    '角色评分模板': '评分角色与模版',
    'Show Debug Boxes': '识别调试框',
    'ShowCurrentComparisons': '显示其他角色评分',
    'AutoWeightUpdates': '自动更新权重与角色属性',
    'CaptureHotkeysJSON': '收录快捷键',
    'InventoryProfileID': '声骸档案',
    'IntakeMode': '收录方式',
    'ScanMinimumLevel': '自动扫描最低等级',
    'TemplateCharacterID': '评分角色',
    'TemplateModalID': '评分模版',
    'ComparisonSettingsJSON': '匹配设置',
    'AllocationSettingsJSON': '配装要求',
}


def shortcut_text(raw):
    try:
        values = parse_bindings(raw)
    except ValueError:
        return '快捷键设置需要重新选择'
    return '；'.join(f'{ACTIONS[key]}：{value.text}' for key, value in values.items()) or '未开启'


def setting_text(key, value, profiles=None):
    if key == 'IntakeMode':
        return {'manual': '手动收录', 'automatic': '自动扫描'}.get(value, '待选择')
    if key == 'ScanMinimumLevel':
        return '+' + str(value) + ' 及以上'
    if type(value) is bool:
        return '开启' if value else '关闭'
    if key == 'CaptureHotkeysJSON':
        return shortcut_text(value)
    if key in ('ComparisonSettingsJSON', 'AllocationSettingsJSON'):
        return '未设置' if not value else '已保存（详细要求可在对应页面查看）'
    if key == 'InventoryProfileID':
        return (profiles or {}).get(value, '未选择' if not value else '原档案')
    if key in ('TemplateCharacterID', 'TemplateModalID'):
        return '已选择' if value else '未选择'
    return str(value) if value else '未设置'


def job_text(state):
    return {
        'idle': '尚未开始',
        'queued': '等待处理',
        'checking': '正在检查',
        'preparing': '准备中',
        'running': '处理中',
        'exporting': '正在备份',
        'importing': '正在恢复',
        'validating': '正在检查',
        'completed': '已完成',
        'failed': '处理失败',
        'cancelled': '已取消',
        'closed': '已结束',
        'updated': '检查完成，权重已更新',
        'unchanged': '没有变化',
        'current': '检查完成，暂无更新',
        'stopped': '已取消',
        'conflict': '本机权重已变化，请重新读取',
        'deferred': '需要确认模版变更',
        'busy': '请等待其他操作完成',
        'loaded': '已读取本机权重',
        'reloaded': '已读取本机权重',
        'customized': '权重设置已保存',
    }.get(state, '状态待确认，请查看诊断信息')


def add_diagnostics(layout, owner, widget, title):
    if getattr(owner, '_embedded_settings', False):
        # Optional read-only diagnostics remain on the same page. Only technical
        # text is folded; editable preferences are always visible.
        body = QWidget(owner)
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 0, 0, 0)
        widget.setReadOnly(True)
        widget.setMinimumHeight(100)
        widget.setMaximumHeight(160)
        content.addWidget(widget)
        button = QPushButton('查看诊断信息', owner)
        button.setCheckable(True)
        button.toggled.connect(body.setVisible)
        button.toggled.connect(
            lambda checked: button.setText('收起诊断信息' if checked else '查看诊断信息')
        )
        layout.addWidget(button)
        layout.addWidget(body)
        body.hide()
        return body
    dialog = QDialog(owner)
    dialog.setWindowTitle(title)
    body = QVBoxLayout(dialog)
    body.addWidget(widget)
    widget.setReadOnly(True)
    close = QPushButton('关闭', dialog)
    close.clicked.connect(dialog.close)
    body.addWidget(close)
    from echo_workshop.ui.widgets.layout import fit_dialog

    fit_dialog(dialog, 850, 450)
    button = QPushButton('诊断信息', owner)
    button.setToolTip('排查问题时查看；日常使用无需阅读。')
    button.clicked.connect(lambda checked=False: dialog.show())
    layout.addWidget(button)
    return dialog
