# 编辑角色自定义权重并说明来源。
"""Inline character weight editor; commits use the existing shared worker."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QHBoxLayout, QLabel, QPushButton
from echo_workshop.ui.widgets.inputs import QComboBox, QDoubleSpinBox
from echo_workshop.resources.custom_weights import MAIN_NAMES, SUB_NAMES, LABELS
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.cancellation import OperationCancelled
import threading


class _WeightSpin(QDoubleSpinBox):
    def textFromValue(self, value):
        return (
            super()
            .textFromValue(value)
            .rstrip(self.locale().zeroDigit())
            .rstrip(self.locale().decimalPoint())
        )


class CustomWeightEditor(QWidget):
    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task, self.runtime = task, task.runtime
        self.version = ''
        self.selection = None
        self.dirty = False
        self.drafts = {}
        self.pending = False
        self._projection = None
        self._ready = False
        self.destroyed.connect(self._cancel_projection)
        self.reset_armed = False
        self.main_controls = {}
        self.sub_controls = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        title = QLabel('自定义角色权重')
        title.setStyleSheet('font-weight: bold;')
        layout.addWidget(title)
        help_text = QLabel('数值越大，词条越重要。')
        help_text.setToolTip(
            '0 表示不计分。保存后，此角色的所有模版均保留本地权重，不随更新改变。理想声骸仍按 50 分计算。'
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        row = QHBoxLayout()
        self.characters = QComboBox()
        self.characters.setEditable(True)
        self.characters.setInsertPolicy(QComboBox.NoInsert)
        self.characters.setMinimumWidth(130)
        self.variants = QComboBox()
        self.variants.setMinimumWidth(130)
        row.addWidget(QLabel('角色'))
        row.addWidget(self.characters, 1)
        row.addWidget(QLabel('模版'))
        row.addWidget(self.variants, 1)
        layout.addLayout(row)
        self.state = QLabel()
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        self.body = QWidget(self)
        layout.addWidget(self.body)
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        buttons = QHBoxLayout()
        self.save_button = QPushButton('保存权重')
        self.save_button.setToolTip('保存当前角色、当前模版的权重。此角色的自定义权重更新时保留。')
        self.reset_button = QPushButton('恢复默认')
        self.reset_button.setToolTip('恢复此角色所有模版的默认权重；点击后需再次确认。')
        self.discard_button = QPushButton('撤销修改')
        self.discard_button.setToolTip('放弃当前模版尚未保存的修改，保留已保存的权重。')
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.reset_button)
        buttons.addWidget(self.discard_button)
        layout.addLayout(buttons)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self.characters.currentIndexChanged.connect(self._character_changed)
        self.variants.currentIndexChanged.connect(self._variant_changed)
        self.save_button.clicked.connect(self.save)
        self.reset_button.clicked.connect(self.reset)
        self.discard_button.clicked.connect(self.discard)
        self.refresh()

    def _remember(self):
        if self.selection and self.dirty:
            self.drafts[self.selection] = (self.version, self.original, self.values())

    def _character_changed(self):
        self._remember()
        char_id = self.characters.currentData()
        snapshot = self.runtime.weights.active
        self.variants.blockSignals(True)
        self.variants.clear()
        if char_id in snapshot.templates:
            for variant, template in snapshot.templates[char_id].items():
                self.variants.addItem(template['name'], variant)
        self.variants.blockSignals(False)
        self._load()

    def _variant_changed(self):
        self._remember()
        self._load()

    def _load(self):
        self._cancel_projection()
        self._ready = False
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.main_controls, self.sub_controls = {}, {}
        char_id, variant = self.characters.currentData(), self.variants.currentData()
        snapshot = self.runtime.weights.active
        self.selection = (
            (char_id, variant)
            if char_id in snapshot.templates and variant in snapshot.templates[char_id]
            else None
        )
        self.reset_armed = False
        self.reset_button.setText('恢复默认')
        if self.selection is None:
            self.state.setText('请选择已有角色和模版')
            self.dirty = False
            return
        self.version, self.original = snapshot.version, snapshot.template(*self.selection)
        self.dirty = self.selection in self.drafts
        if self.dirty:
            self.version, self.original, (main, sub) = self.drafts[self.selection]
            self._show_values(main, sub)
            return
        event = threading.Event()
        try:
            future = self.runtime.custom_weights.project_async(
                self.original,
                identity=self.selection,
                weight_version=self.version,
                cancel_event=event,
            )
        except (ValueError, ComputeFailure, OperationCancelled) as error:
            self.feedback.setText(str(error))
            return
        self._projection = (future, event, self.selection, self.version)
        self.feedback.setText('正在读取权重编辑项…')
        self.body.setEnabled(False)
        self._status()

    def _cancel_projection(self, *_):
        if self._projection is not None:
            self._projection[1].set()
            self._projection = None

    def _poll_projection(self):
        projection = self._projection
        if projection is None or not projection[0].done():
            return
        future, event, selection, version = projection
        self._projection = None
        if (
            event.is_set()
            or selection != self.selection
            or version != self.version
            or self.runtime.weights.active.version != version
        ):
            return
        try:
            main, sub = self.runtime.custom_weights.projected_values(
                future.result(),
                identity=selection,
                weight_version=version,
            )
        except Exception as error:
            self.feedback.setText('权重编辑项未读取：' + str(error))
            return
        self.feedback.clear()
        self._show_values(main, sub)

    def _show_values(self, main, sub):
        for column, label in enumerate(
            ('词条', '主词条 COST 1', '主词条 COST 3', '主词条 COST 4', '副词条')
        ):
            header = QLabel(label)
            header.setWordWrap(True)
            self.grid.addWidget(header, 0, column)
        names = [name for name in MAIN_NAMES if any(name in weights for weights in main.values())]
        names += [name for name in SUB_NAMES if name not in names and name in sub]
        for row, name in enumerate(names, 1):
            label = QLabel(LABELS.get(name, name))
            label.setWordWrap(True)
            self.grid.addWidget(label, row, 0)
            for col, cost in enumerate(('1', '3', '4'), 1):
                if name in main[cost]:
                    control = self._spin(main[cost][name])
                    self.main_controls[cost, name] = control
                    self.grid.addWidget(control, row, col)
                else:
                    self.grid.addWidget(QLabel('—'), row, col, Qt.AlignCenter)
            if name in sub:
                control = self._spin(sub[name])
                self.sub_controls[name] = control
                self.grid.addWidget(control, row, 4)
            else:
                self.grid.addWidget(QLabel('—'), row, 4, Qt.AlignCenter)
        self._ready = True
        self.body.setEnabled(self._ready and not self.pending)
        self._status()

    def _spin(self, value):
        spin = _WeightSpin(self.body)
        spin.setRange(0, 1000000)
        spin.setDecimals(6)
        spin.setSingleStep(0.1)
        spin.setKeyboardTracking(False)
        spin.setMinimumWidth(72)
        spin.setValue(value)
        spin.valueChanged.connect(self._edited)
        return spin

    def _edited(self):
        self.dirty = True
        self._status()

    def values(self):
        main = {cost: {} for cost in ('1', '3', '4')}
        for (cost, name), spin in self.main_controls.items():
            main[cost][name] = spin.value()
        return main, {name: spin.value() for name, spin in self.sub_controls.items()}

    def _status(self):
        custom = self.selection and self.selection[0] in self.runtime.weights.customized_characters
        self.state.setText(
            ('自定义权重（更新时保留）' if custom else '默认权重')
            + (' · 未保存' if self.dirty else '')
        )
        element = (
            self.runtime.weights.active.character_elements.get(
                self.selection[0], '待更新（请检查更新）'
            )
            if self.selection and self.selection[0] != 'default'
            else '通用模版不限定属性'
        )
        self.state.setToolTip(
            ('自定义权重更新时保留；' if custom else '默认权重跟随资料更新；')
            + '角色属性：'
            + element
        )

    def _valid_character(self):
        if self.characters.currentText() != self.characters.itemText(
            self.characters.currentIndex()
        ):
            self.feedback.setText('请从搜索结果中选择一个角色。')
            return False
        return True

    def discard(self):
        if self.pending:
            return
        self.drafts.pop(self.selection, None)
        self.dirty = False
        self._load()
        self.feedback.setText('已重新读取当前保存的权重。')

    def save(self):
        if not self.selection or self.pending or not self._ready or not self._valid_character():
            return
        # Spinboxes with keyboard tracking disabled must commit typed values
        # before reading, including programmatic button activation.
        for spin in (*self.main_controls.values(), *self.sub_controls.values()):
            spin.interpretText()
        main, sub = self.values()
        self.pending = self.runtime.updates.request_custom(
            *self.selection,
            main=main,
            sub=sub,
            expected_version=self.version,
        )
        self.feedback.setText(
            '正在保存…' if self.pending else '已有权重操作正在进行，请稍候再保存。'
        )
        self.refresh()

    def reset(self):
        if not self.selection or self.pending or not self._valid_character():
            return
        if not self.reset_armed:
            self.reset_armed = True
            self.reset_button.setText('再次点击，改用默认权重')
            self.feedback.setText('将清除此角色所有模版的自定义设置，改用最近一次更新的默认权重。')
            return
        self.pending = self.runtime.updates.request_custom(
            self.selection[0], expected_version=self.runtime.weights.active.version
        )
        self.feedback.setText(
            '正在切回默认权重…' if self.pending else '已有权重操作正在进行，请稍候。'
        )
        self.refresh()

    def refresh(self):
        self._poll_projection()
        snapshot = self.runtime.weights.active
        status = self.runtime.updates.status
        if (
            self.pending
            and status.operation == 'custom'
            and status.state not in ('checking', 'queued')
        ):
            self.pending = False
            self.feedback.setText(status.message)
            if status.state == 'customized':
                char_id = self.selection[0] if self.selection else None
                if self.reset_armed:
                    self.drafts = {
                        key: value for key, value in self.drafts.items() if key[0] != char_id
                    }
                else:
                    self.drafts.pop(self.selection, None)
                self.dirty = False
                self._load()
        if getattr(self, '_catalog_version', None) != snapshot.version:
            self._remember()
            char_id = self.characters.currentData() or str(
                self.task.config.get('TemplateCharacterID', 'default')
            )
            variant = self.variants.currentData()
            self.characters.blockSignals(True)
            self.characters.clear()
            for cid, variants in snapshot.templates.items():
                name = next(iter(variants.values()))['name'].split('-', 1)[0]
                self.characters.addItem(
                    name
                    + ('（自定义）' if cid in self.runtime.weights.customized_characters else ''),
                    cid,
                )
            self.characters.setCurrentIndex(max(0, self.characters.findData(char_id)))
            self.characters.blockSignals(False)
            self._character_changed()
            index = self.variants.findData(variant)
            if index >= 0:
                self.variants.setCurrentIndex(index)
            self._catalog_version = snapshot.version
        busy = (
            self.pending or self._projection is not None or status.state in ('checking', 'queued')
        )
        self.characters.setEnabled(not self.pending)
        self.variants.setEnabled(not self.pending)
        self.body.setEnabled(self._ready and not self.pending)
        self.discard_button.setEnabled(bool(self.selection) and self.dirty and not self.pending)
        self.save_button.setEnabled(bool(self.selection) and self._ready and not busy)
        self.reset_button.setEnabled(
            bool(self.selection)
            and self.selection[0] in self.runtime.weights.customized_characters
            and not busy
        )
        self._status()
