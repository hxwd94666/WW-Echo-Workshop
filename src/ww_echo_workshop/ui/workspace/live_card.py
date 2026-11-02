# 工作台实时评分卡：绑定评分开关、角色模版、目标分和策略偏好。
import json

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, PushButton

from ww_echo_workshop.application.policy import costs_from_config
from ww_echo_workshop.application.preset_catalog import PresetCatalog
from ww_echo_workshop.core.policy import POLICY_SOURCE
from ww_echo_workshop.core.policy_presets import SYSTEMS, default_scoring
from ww_echo_workshop.ui.widgets.inputs import ComboBox, QDoubleSpinBox
from ww_echo_workshop.ui.widgets.roles import RolePicker
from ww_echo_workshop.ui.workspace.preset_picker import PresetPickerDialog


class LivePolicyCard(CardWidget):
    preset_changed = Signal()

    def __init__(self, parent=None, *, task):
        super().__init__(parent)
        self.task = task
        self.runtime = task.runtime
        self._preset_catalog = PresetCatalog(getattr(self.runtime, 'data_dir', '.'))
        self._loading = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = BodyLabel('实时评分与策略', self)
        font = heading.font()
        font.setBold(True)
        font.setPointSize(11)
        heading.setFont(font)
        layout.addWidget(heading)

        self.live_check = QCheckBox('显示实时评分', self)
        self.hud_follow_check = QCheckBox('悬浮窗位置变换（仅适配2K）', self)
        self.auto_check = QCheckBox('跟随当前装备角色评分', self)
        self.policy_check = QCheckBox('显示强化/重抽建议', self)
        self.live_check.setAccessibleName('显示实时评分')
        self.hud_follow_check.setAccessibleName('悬浮窗自动变换位置')
        self.auto_check.setAccessibleName('跟随当前装备角色评分')
        self.policy_check.setAccessibleName('显示强化和重抽建议')
        checks = QHBoxLayout()
        checks.setContentsMargins(0, 0, 0, 0)
        for check in (
            self.live_check,
            self.hud_follow_check,
            self.auto_check,
            self.policy_check,
        ):
            checks.addWidget(check)
        checks.addStretch()
        layout.addLayout(checks)

        snapshot = self.runtime.weights.active
        configured = (
            self._config('TemplateCharacterID'),
            self._config('TemplateModalID'),
        )
        self.picker = RolePicker(snapshot, self, configured if all(configured) else None)
        layout.addWidget(self.picker)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.target = QDoubleSpinBox(self)
        self.target.setRange(0.0, 655.35)
        self.target.setMinimumWidth(180)
        self.target.setDecimals(1)
        self.target.setSingleStep(0.5)
        self.target.setSpecialValueText('未设置')
        self.target.setValue(float(self._config('强化策略目标分') or 0.0))
        self.target.setToolTip('用户设置的总分目标，包含主词条；0 表示未设置。')
        self.target.setAccessibleName('强化策略目标分')
        form.addRow('目标分', self.target)

        self.mode = ComboBox(self)
        for label, value in (
            ('自动', 'auto'),
            ('仅强化', 'upgrade'),
            ('仅重抽', 'reroll'),
            ('联合（实验）', 'joint'),
        ):
            self.mode.addItem(label, userData=value)
        self._select(self.mode, self._config('强化策略模式'), 'auto')
        self.mode.setMinimumWidth(180)
        self.mode.setAccessibleName('策略模式')
        form.addRow('策略模式', self.mode)

        self.scoring = ComboBox(self)
        self.scoring.addItem('XWUID（工坊默认）', userData='workshop_xwuid')
        for key, spec in SYSTEMS.items():
            if key == 'qq_bot':
                continue
            self.scoring.addItem(spec['label'], userData=key)
        self._select(self.scoring, self._config('策略评分体系'), 'workshop_xwuid')
        self.scoring.setMinimumWidth(180)
        self.scoring.setAccessibleName('策略评分体系')
        form.addRow('评分体系', self.scoring)

        self.resource_boxes = []
        resources = QWidget(self)
        resource_grid = QGridLayout(resources)
        resource_grid.setContentsMargins(0, 0, 0, 0)
        resource_grid.setHorizontalSpacing(8)
        resource_grid.setVerticalSpacing(6)
        for index, label in enumerate(('声骸胚子', '调谐器', '特级密音筒', '频整器')):
            box = QDoubleSpinBox(resources)
            box.setRange(0.0, 1_000_000_000.0)
            box.setDecimals(3)
            box.setSingleStep(1.0)
            box.setMinimumWidth(100)
            box.setValue(float(costs_from_config(self.task.config)[index]))
            box.setAccessibleName(f'资源相对价值：{label}')
            box.valueChanged.connect(self._cost_changed)
            self.resource_boxes.append(box)
            row, column = divmod(index, 2)
            resource_grid.addWidget(CaptionLabel(label, resources), row, column * 2)
            resource_grid.addWidget(box, row, column * 2 + 1)
            resource_grid.setColumnStretch(column * 2 + 1, 1)
        form.addRow(resources)
        layout.addLayout(form)
        layout.addWidget(CaptionLabel('0 表示不优化；频整器仅联合模式。', self))
        self.preset_box = QWidget(self)
        preset_row = QHBoxLayout(self.preset_box)
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.addWidget(CaptionLabel('角色预设', self.preset_box))
        self.preset_name = CaptionLabel('未选择', self.preset_box)
        self.preset_name.setWordWrap(True)
        preset_row.addWidget(self.preset_name, 1)
        self.preset_button = PushButton('选择', self.preset_box)
        self.preset_button.clicked.connect(self._pick_preset)
        preset_row.addWidget(self.preset_button)
        self.preset_match = PushButton('从当前角色匹配', self.preset_box)
        self.preset_match.clicked.connect(self._match_current_role)
        preset_row.addWidget(self.preset_match)
        layout.addWidget(self.preset_box)
        self.preset_note = CaptionLabel('', self)
        self.preset_note.setWordWrap(True)
        layout.addWidget(self.preset_note)

        self.source = CaptionLabel(POLICY_SOURCE, self)
        self.source.setToolTip(POLICY_SOURCE)
        self.source.setVisible(False)

        self.live_check.setChecked(bool(self._config('启用声骸评分', True)))
        self.hud_follow_check.setChecked(bool(self._config('策略悬浮窗自动定位', False)))
        self.auto_check.setChecked(bool(self._config('自动匹配评分模板', False)))
        self.policy_check.setChecked(bool(self._config('启用强化策略', False)))
        self.live_check.toggled.connect(self._live_changed)
        self.hud_follow_check.toggled.connect(self._hud_follow_changed)
        self.auto_check.toggled.connect(lambda value: self._write('自动匹配评分模板', bool(value)))
        self.policy_check.toggled.connect(lambda value: self._write('启用强化策略', bool(value)))
        self.target.valueChanged.connect(
            lambda value: self._write('强化策略目标分', round(float(value), 1))
        )
        self.mode.currentIndexChanged.connect(
            lambda _: self._write('强化策略模式', self.mode.currentData())
        )
        self.scoring.currentIndexChanged.connect(self._scoring_changed)
        self.picker.changed.connect(self._sync_enabled)
        self.picker.changed.connect(self._auto_apply_selection)
        self._loading = False
        self._scoring_changed(initial=True)
        self._sync_enabled()
        self.refresh_presets()
        self.refresh_status()

    def _scoring_changed(self, *_, initial=False):
        if self._loading:
            return
        system = self.scoring.currentData() or 'workshop_xwuid'
        if system == 'workshop_xwuid':
            maximum = 50.0
        elif system == 'fixed':
            maximum = 65535.0
        else:
            maximum = 655.35
        self.target.setRange(0.0, maximum)
        workshop = system == 'workshop_xwuid'
        self.picker.setVisible(workshop)
        self.auto_check.setVisible(workshop)
        self.preset_box.setVisible(not workshop)
        if not initial and system != 'workshop_xwuid':
            self.target.setValue(float(default_scoring(system)['target']))
        if not initial:
            self.task.config['策略评分体系'] = system
            self.task.config['强化策略目标分'] = round(float(self.target.value()), 1)
            if system == 'workshop_xwuid':
                self.task.config['策略评分预设'] = ''
                self.task.config['策略评分预设名称'] = ''
            self.refresh_presets()
            self.refresh_status()

    def _config(self, key, default=None):
        return self.task.config.get(key, default)

    def _write(self, key, value):
        if not self._loading:
            self.task.config[key] = value
            self.refresh_status()

    def _hud_follow_changed(self, enabled):
        self._write('策略悬浮窗自动定位', bool(enabled))
        if enabled:
            controller = getattr(self.runtime, 'hud_controller', None)
            reset = getattr(controller, 'reset_auto_position', None)
            if callable(reset):
                reset()

    def _cost_changed(self, *_):
        if self._loading:
            return
        values = [round(float(box.value()), 3) for box in self.resource_boxes]
        self._write('强化策略资源权重', json.dumps(values, separators=(',', ':')))

    def _select(self, combo, value, default):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else combo.findData(default))

    def _auto_apply_selection(self):
        if self._loading:
            return
        key = self.picker.key()
        if key is None:
            return
        self.apply_selection()

    def apply_selection(self):
        key = self.picker.key()
        if key is None:
            return
        try:
            template = self.runtime.weights.active.template(*key)
        except (ValueError, KeyError):
            return
        self.task.config['TemplateCharacterID'] = key[0]
        self.task.config['TemplateModalID'] = key[1]
        self.task.config['角色评分模板'] = template['name']
        self.refresh_status()

    def sync_preset(self):
        system = self.task.config.get('策略评分体系', 'workshop_xwuid')
        index = self.scoring.findData(system)
        if index < 0:
            return
        self._loading = True
        self.scoring.setCurrentIndex(index)
        self._loading = False
        self._scoring_changed(initial=True)
        self.target.setValue(float(self.task.config.get('强化策略目标分') or 0.0))
        self.refresh_presets()
        self.refresh_status()

    def refresh_presets(self):
        self._preset_catalog.load()
        system = self.scoring.currentData() or 'workshop_xwuid'
        preset_id = self._config('策略评分预设', '')
        preset = self._preset_catalog.get(preset_id, system) if preset_id else None
        if system == 'workshop_xwuid':
            self.preset_name.setText('')
            self.preset_note.setText('')
        elif preset is None:
            self.preset_name.setText('未选择' if not preset_id else '预设已失效')
            self.preset_note.setText('从策略数据包选择角色权重；应用后实时评分与强化测试共用。')
        else:
            self.preset_name.setText(f'{preset.character} · {preset.variant}')
            self.preset_note.setText('当前预设同时用于实时评分和强化测试。')

    def _pick_preset(self):
        system = self.scoring.currentData() or 'workshop_xwuid'
        dialog = PresetPickerDialog(self._preset_catalog, system, self)
        if dialog.exec() and dialog.selected is not None:
            self._apply_preset(dialog.selected)

    def _apply_preset(self, preset):
        system = preset.system
        self._loading = True
        index = self.scoring.findData(system)
        if index >= 0:
            self.scoring.setCurrentIndex(index)
        self._loading = False
        self._scoring_changed(initial=True)
        self.task.config['策略评分体系'] = system
        self.task.config['策略评分权重'] = json.dumps(
            [float(value) for value in preset.weights], separators=(',', ':')
        )
        self.task.config['策略主词条分'] = float(preset.main_score)
        self.task.config['策略归一满分'] = float(preset.max_score)
        self.task.config['强化策略目标分'] = round(float(preset.target), 1)
        self.task.config['策略评分预设'] = preset.preset_id
        self.task.config['策略评分预设名称'] = f'{preset.character} · {preset.variant}'
        self.target.setValue(float(preset.target))
        self.refresh_presets()
        self.refresh_status()
        self.preset_changed.emit()

    def _match_current_role(self):
        system = self.scoring.currentData() or 'workshop_xwuid'
        if system == 'workshop_xwuid':
            return
        store = getattr(self.runtime, 'live_snapshots', None)
        snapshot = store.latest() if store is not None else None
        ids = getattr(snapshot, 'selected_ids', ()) if snapshot is not None else ()
        if len(ids) != 2:
            self.preset_note.setText('当前没有可匹配的游戏角色。')
            return
        try:
            name = self.runtime.weights.active.scoring_template(*ids)['name']
        except (AttributeError, KeyError, ValueError):
            self.preset_note.setText('当前角色没有可用的工坊模版。')
            return
        character = name.split('-', 1)[0]
        preset = next(
            (item for item in self._preset_catalog.presets(system) if item.character == character),
            None,
        )
        if preset is None:
            self.preset_note.setText(f'没有找到“{character}”的当前体系预设。')
            return
        self._apply_preset(preset)

    def _live_changed(self, enabled):
        if (
            enabled
            and not self._loading
            and QApplication.platformName() != 'offscreen'
            and not self._ocr_warning_accepted()
        ):
            box = QMessageBox(self)
            box.setWindowTitle('实时评分 OCR 提示')
            box.setIcon(QMessageBox.Warning)
            box.setText('实时评分需要持续占用 OCR 资源。')
            box.setInformativeText(
                '自动战斗期间会自动暂停实时评分。使用其他 OCR 功能时，'
                '请先关闭实时评分，避免 OCR 资源冲突和识别延迟。'
            )
            box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
            box.setDefaultButton(QMessageBox.Cancel)
            if box.exec() != QMessageBox.Ok:
                self.live_check.blockSignals(True)
                self.live_check.setChecked(False)
                self.live_check.blockSignals(False)
                enabled = False
            else:
                self.task.config['实时评分OCR提示已确认'] = True
        self._write('启用声骸评分', bool(enabled))
        self._sync_enabled()

    def _ocr_warning_accepted(self):
        return bool(self.task.config.get('实时评分OCR提示已确认', False))

    def _sync_enabled(self):
        live = self.live_check.isChecked()
        for control in (
            self.hud_follow_check,
            self.auto_check,
            self.policy_check,
            self.picker,
            self.preset_box,
        ):
            control.setEnabled(live)
        enabled = live and self.policy_check.isChecked()
        self.target.setEnabled(enabled)
        self.mode.setEnabled(enabled)
        self.scoring.setEnabled(enabled)
        for box in self.resource_boxes:
            box.setEnabled(enabled)

    def refresh_weights(self):
        active = self.runtime.weights.active
        if self.picker.snapshot.version != active.version:
            self.picker.reload(active, self.picker.key())
        self.refresh_status()

    def refresh_status(self):
        self._sync_enabled()
