# 校验声骸核对表单并保留原始观察证据。
from dataclasses import replace
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)
from echo_workshop.ui.widgets.inputs import QComboBox, QDoubleSpinBox, QSpinBox
from echo_workshop.core.models import StatValue, MAINSTAT_VALIDATION_MAXIMA, utc_now
from echo_workshop.core.scoring import SUBSTAT_TIERS
from echo_workshop.ui.widgets.layout import Disclosure, fit_dialog
from echo_workshop.application.view_state import (
    review_checked_reason,
    review_issue_details,
    review_reason,
)
from echo_workshop.ui.inventory.evidence import open_evidence
from echo_workshop.ui.inventory.review_fields import ChoiceBox, ReviewChoices


class StatEditorDelegate(QStyledItemDelegate):
    """The cell widget alone draws edited text; backing items still hold values."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.column() < 2:
            option.text = ''


class RecordEditorContent:
    def __init__(
        self,
        record,
        parent=None,
        *,
        evidence=None,
        confirm=False,
        previous=None,
        choices=None,
        quality_check=None,
        quality=None,
        review_kind=None,
    ):
        super().__init__(parent)
        self.record = record
        self.quality = quality
        self.review_kind = review_kind
        self._quality_check = quality_check
        self._quality_request = None
        self._quality_pending_record = None
        self._quality_timer = QTimer(self)
        self._quality_timer.setInterval(20)
        self._quality_timer.timeout.connect(self._poll_quality)
        self.result_record = None
        self.require_confirmation = confirm
        self.setWindowTitle('对照截图纠错 / 确认字段')
        fit_dialog(self, 1000, 700)
        layout = QVBoxLayout(self)
        self.splitter = QSplitter()
        left = QWidget()
        self.preview_panel = left
        left_layout = QVBoxLayout(left)
        cards = getattr(self, '_structured_cards', False)
        if cards:
            from echo_workshop.ui.inventory.cards import EchoCardWidget

            self.preview = EchoCardWidget()
            self.preview.set_record(record)
        else:
            self.preview = QLabel('此记录没有可读取的截图，请返回游戏核对。')
            self.preview.setAlignment(Qt.AlignCenter)
            self.preview.setWordWrap(True)
            self.preview.setMinimumSize(160, 100)
        self._preview_pixmap = QPixmap()
        if not cards and record.screenshot_ref and evidence is not None:
            try:
                self._preview_pixmap.loadFromData(evidence.read(record.screenshot_ref), 'PNG')
            except (OSError, ValueError):
                self.preview.setText('原截图读取失败，已有证据引用保留。')
        left_layout.addWidget(self.preview)
        self.reason_label = QLabel('')
        self.reason_label.setWordWrap(True)
        self.reason_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        issues = self.quality.issues if self.quality is not None else tuple(record.issues)
        details = review_issue_details(issues)
        reasons = list(details)
        if self.review_kind == 'duplicate':
            reasons.insert(0, '疑似重复：属性与库存中已有记录相同，请选择另存或更新旧件')
        elif self.review_kind == 'upgrade':
            reasons.insert(0, '疑似升级：与旧件同套装、同主词条，请选择更新旧件或另存')
        if reasons:
            self.reason_label.setText(
                '待核对原因：\n' + '\n'.join('· ' + detail for detail in reasons)
            )
        elif record.review != 'confirmed':
            self.reason_label.setText('待核对原因：字段完整；勾选“已核对”后可保存确认。')
        self.reason_scroll = QScrollArea()
        self.reason_scroll.setWidgetResizable(True)
        self.reason_scroll.setFrameShape(QFrame.NoFrame)
        self.reason_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.reason_scroll.setMaximumHeight(150)
        self.reason_scroll.setWidget(self.reason_label)
        self.reason_scroll.setVisible(bool(self.reason_label.text()))
        left_layout.addWidget(self.reason_scroll)
        screenshot = QPushButton('打开原始截图，对照后返回纠错')
        screenshot.setEnabled(bool(record.screenshot_ref and evidence is not None))

        def show_evidence():
            try:
                open_evidence(record, evidence, self)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, '截图校验未通过', str(error))

        screenshot.clicked.connect(show_evidence)
        left_layout.addWidget(screenshot)
        if previous is not None:
            old = Disclosure('对照旧件：升级关联前请核对', self)
            old_label = QLabel(
                f'旧件 {previous.name}　COST {previous.cost}　+{previous.level}\n新观察 {record.name}　COST {record.cost}　+{record.level}\n'
                f'旧副词条：{", ".join(s.stat_name + " " + str(s.value) for s in previous.sub_stats)}\n'
                f'新副词条：{", ".join(s.stat_name + " " + str(s.value) for s in record.sub_stats)}'
            )
            old_label.setWordWrap(True)
            if not cards:
                old.content_layout.addWidget(old_label)
            if cards:
                old_image = EchoCardWidget()
                old_image.set_record(previous)
                old_button = QPushButton('查看旧件原图')
                old_button.setEnabled(bool(previous.screenshot_ref and evidence is not None))
                old_button.clicked.connect(lambda: open_evidence(previous, evidence, self))
                old.content_layout.addWidget(old_button)
            else:
                old_image = QLabel()
            if not cards and previous.screenshot_ref and evidence is not None:
                try:
                    old_pixmap = QPixmap()
                    old_pixmap.loadFromData(evidence.read(previous.screenshot_ref), 'PNG')
                    old_image.setPixmap(
                        old_pixmap.scaled(300, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    )
                    old_button = QPushButton('放大旧件原图')
                    old_button.clicked.connect(lambda: open_evidence(previous, evidence, self))
                    old.content_layout.addWidget(old_button)
                except (ValueError, OSError):
                    old_image.setText('旧件截图未读取，请返回游戏核对。')
            old.content_layout.addWidget(old_image)
            old.toggle.setChecked(True)
            left_layout.addWidget(old)
        left_layout.addStretch(1)
        self.splitter.addWidget(left)
        self.field_area = QScrollArea()
        self.field_area.setWidgetResizable(True)
        fields = QWidget()
        fields_layout = QVBoxLayout(fields)
        self.field_area.setWidget(fields)
        self.splitter.addWidget(self.field_area)
        layout.addWidget(self.splitter, 1)
        self.edit_all = QCheckBox('编辑其他字段（默认只开放待核对字段）')
        fields_layout.addWidget(self.edit_all)
        form = QFormLayout()
        choices = choices if choices is not None else ReviewChoices.from_record(record)
        self.name_edit = ChoiceBox(
            ((name, name) for name in choices.names), record.name, field_name='声骸名称'
        )
        self.sonata_edit = ChoiceBox(
            ((name, name) for name in choices.sonatas), record.sonata, field_name='套装'
        )
        self.owner_edit = ChoiceBox(
            choices.owners,
            (record.owner, record.owner_state, record.owner_character_id),
            field_name='工坊装备者',
        )
        self.cost_edit = QComboBox()
        self.cost_edit.addItems(['1', '3', '4'])
        self.cost_edit.setCurrentText(str(record.cost))
        self.level_edit = QSpinBox()
        self.level_edit.setRange(-1, 25)
        self.level_edit.setSpecialValueText('未知，请核对')
        self.level_edit.setValue(-1 if record.level is None else record.level)
        for label, widget in (
            ('声骸名称', self.name_edit),
            ('套装', self.sonata_edit),
            ('工坊装备者', self.owner_edit),
            ('COST', self.cost_edit),
            ('等级', self.level_edit),
        ):
            form.addRow(label, widget)
        fields_layout.addLayout(form)
        self.stats = QTableWidget(7, 3)
        self.stats.setItemDelegate(StatEditorDelegate(self.stats))
        self.stats.verticalHeader().setDefaultSectionSize(36)
        self.stats.setHorizontalHeaderLabels(['词条类型', '游戏显示数值', '原 OCR 数值（保留）'])
        self.stats.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for index in range(7):
            self.stats.setVerticalHeaderItem(
                index, QTableWidgetItem(f'主{index + 1}' if index < 2 else f'副{index - 1}')
            )
            rows = record.main_stats + record.sub_stats
            row = rows[index] if index < len(rows) else None
            for column, text in enumerate(
                (
                    row.stat_name if row else '',
                    str(row.value) if row else '',
                    row.value_text if row else '',
                )
            ):
                item = QTableWidgetItem(text)
                if column == 2:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.stats.setItem(index, column, item)
        self.stats.setColumnHidden(2, True)
        self.stat_controls = []
        all_main = sorted(
            {
                name
                for slots in MAINSTAT_VALIDATION_MAXIMA.values()
                for slot in slots
                for name in slot
            }
        )
        for index in range(7):
            combo = QComboBox()
            combo.addItem('未开启 / 留空' if index >= 2 else '请选择主词条', '')
            for name in sorted(SUBSTAT_TIERS) if index >= 2 else all_main:
                from echo_workshop.core.filters import STAT_LABELS

                combo.addItem(STAT_LABELS.get(name, name.replace('%', '（百分比）')), name)
            current = self.stats.item(index, 0).text()
            if current and combo.findData(current) < 0:
                combo.addItem('待核对：' + current, current)
            combo.setCurrentIndex(combo.findData(current))
            value = QDoubleSpinBox()
            value.setRange(0, 1000000)
            value.setDecimals(2)
            value.setValue(float(self.stats.item(index, 1).text() or 0))
            value.setSuffix('' if current in ('生命', '攻击', '防御', '') else ' %')
            self.stats.setCellWidget(index, 0, combo)
            self.stats.setCellWidget(index, 1, value)

            def choose(_, row=index, field=combo, numeric=value):
                name = field.currentData() or ''
                self.stats.item(row, 0).setText(name)
                numeric.setSuffix('' if name in ('生命', '攻击', '防御', '') else ' %')
                if not name and row >= 2:
                    self.stats.item(row, 1).setText('')
                elif name and not self.stats.item(row, 1).text():
                    self.stats.item(row, 1).setText(str(numeric.value()))

            combo.currentIndexChanged.connect(choose)
            value.valueChanged.connect(
                lambda number, row=index: self.stats.item(row, 1).setText(str(number))
            )
            self.stat_controls.append((combo, value))
        self.stats.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stats.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stats.setFixedHeight(
            self.stats.horizontalHeader().sizeHint().height()
            + sum(self.stats.rowHeight(row) for row in range(self.stats.rowCount()))
            + 2 * self.stats.frameWidth()
        )
        fields_layout.addWidget(self.stats)
        self.acknowledge = QCheckBox('已对照原始截图核对上述字段，清除识别警告（原文及历史仍保留）')
        fields_layout.addWidget(self.acknowledge)
        reason = (
            review_checked_reason(record, self.quality)
            if self.quality is not None
            else review_reason(record)
        )
        self.feedback = QLabel(reason + '；未展示的套装/COST 请在游戏内查看后补填，不会自动猜测。')
        self.feedback.setWordWrap(True)
        fields_layout.addWidget(self.feedback)
        fields_layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        if confirm:
            buttons.button(QDialogButtonBox.Save).setText('保存核对并下一件')
            buttons.button(QDialogButtonBox.Cancel).setText('跳过，稍后核对')
        buttons.accepted.connect(self.save_record)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.edit_all.toggled.connect(self.configure_fields)
        self.configure_fields()
        self.resizeEvent(None)

    def configure_fields(self, *_):
        issues = self.quality.issues if self.quality is not None else tuple(self.record.issues)
        all_fields = self.edit_all.isChecked()
        fields = (
            (self.name_edit, 'name'),
            (self.sonata_edit, 'sonata'),
            (self.owner_edit, 'owner'),
            (self.cost_edit, 'cost'),
            (self.level_edit, 'level'),
        )
        for widget, field in fields:
            problem = any(field in issue for issue in issues)
            widget.setEnabled(all_fields or problem)
            widget.setToolTip('此字段待核对' if problem else '已识别；勾选编辑其他字段可修改')
        for index, controls in enumerate(self.stat_controls):
            problem = any(
                issue.startswith('main:') if index < 2 else issue.startswith('sub:')
                for issue in issues
            )
            problem = problem or any(
                issue in ('ocr:ambiguous_value', 'ocr:missing_tuned_rows') for issue in issues
            )
            for widget in controls:
                widget.setEnabled(all_fields or problem)

    def resizeEvent(self, event):
        if hasattr(self, 'splitter'):
            narrow = self.width() < 800
            cards = getattr(self, '_structured_cards', False)
            self.preview_panel.setMaximumHeight(16777215 if cards else 190 if narrow else 16777215)
            self.splitter.setOrientation(Qt.Vertical if narrow else Qt.Horizontal)
            self.splitter.setSizes([180, max(240, self.height() - 340)] if narrow else [420, 580])
            if not self._preview_pixmap.isNull():
                self.preview.setPixmap(
                    self._preview_pixmap.scaled(
                        max(160, self.preview.width()),
                        max(100, self.preview.height()),
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
        if event is not None:
            super().resizeEvent(event)

    def save_record(self):
        try:
            if self.require_confirmation and not self.acknowledge.isChecked():
                raise ValueError('请先核对信息，勾选“已核对”后再保存。')
            echo_name = self.name_edit.selected_value()
            sonata = self.sonata_edit.selected_value()
            owner, owner_state, owner_id = self.owner_edit.selected_value()
            main, sub = [], []
            for index in range(7):
                name = self.stats.item(index, 0).text().strip()
                value = self.stats.item(index, 1).text().strip()
                if index >= 2 and not name and not value:
                    continue
                stat = StatValue(name, float(value), self.stats.item(index, 2).text())
                (main if index < 2 else sub).append(stat)
            owner_changed = (owner, owner_state, owner_id) != (
                self.record.owner,
                self.record.owner_state,
                self.record.owner_character_id,
            )
            record = replace(
                self.record,
                name=echo_name,
                sonata=sonata,
                owner=owner,
                owner_state=owner_state,
                owner_character_id=owner_id,
                owner_updated_at=utc_now() if owner_changed else self.record.owner_updated_at,
                cost=int(self.cost_edit.currentText()),
                level=None if self.level_edit.value() < 0 else self.level_edit.value(),
                main_stats=tuple(main),
                sub_stats=tuple(sub),
                source='manual_review',
                issues=() if self.acknowledge.isChecked() else self.record.issues,
            )
            if self._quality_check is None:
                raise ValueError('编辑器缺少原生质量校验服务，无法保存')
            self._start_quality_check(record)
        except (ValueError, TypeError) as error:
            self.feedback.setText(f'请修正输入：{error}')

    def _start_quality_check(self, record):
        if self._quality_request is not None:
            return
        try:
            self._quality_request = self._quality_check((record,))
        except Exception as error:
            self.feedback.setText(f'质量校验未启动：{error}')
            return
        self._quality_pending_record = record
        self._quality_timer.start()
        self.feedback.setText('正在校验原生质量结论…')

    def _poll_quality(self):
        request = self._quality_request
        if request is None or not request.future.done():
            return
        self._quality_timer.stop()
        record = self._quality_pending_record
        try:
            facts = request.future.result()
            if len(facts) != 1 or facts[0].record != record:
                raise ValueError('质量校验返回记录身份错误')
            if facts[0].issues:
                raise ValueError(
                    '仍有待补字段或不合理数值，请修正后保存。详情：' + ', '.join(facts[0].issues)
                )
        except Exception as error:
            self._quality_request = None
            self._quality_pending_record = None
            self.feedback.setText(f'质量校验未通过：{error}')
            return
        self._quality_request = None
        self._quality_pending_record = None
        self.result_record = replace(record, review='confirmed')
        self.accept()


class RecordEditor(RecordEditorContent, QDialog):
    """Compatibility editor; embedded editing uses QWidget content."""

    pass
