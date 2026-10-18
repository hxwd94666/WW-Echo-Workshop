# 编辑角色五槽要求和套装件数。
"""Structured role requirements; persisted RoleRequest schema stays unchanged."""

import threading

from PySide6.QtCore import Qt, QTimer, QSize, Signal
from PySide6.QtGui import QPixmap
from echo_workshop.core.allocation.models import RoleRequest, SlotRule
from echo_workshop.core.compatibility import CompatibilityRule
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
)
from qfluentwidgets import BodyLabel, CaptionLabel, LineEdit, FluentIcon, PushButton
from echo_workshop.ui.widgets.inputs import QComboBox, QDoubleSpinBox, QSpinBox
from echo_workshop.core.costs import read_cost_layout
from echo_workshop.ui.allocation.costs import CostLayoutPicker
from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES
from echo_workshop.ui.allocation.constraints import MainStatChoice
from echo_workshop.ui.allocation.requirements import MainRequirementsPicker, SonataEffectsPicker
from echo_workshop.ui.widgets.role_layout import RoleSettingsSurface
from echo_workshop.resources.search import search_matches
from echo_workshop.ui.allocation.pin_model import PinModel, PinSelector
from echo_workshop.ui.widgets.roles import IdentityCombo, RolePicker
from echo_workshop.core.scoring import ELEMENT_NAMES
from echo_workshop.ui.widgets.layout import fit_dialog


class RoleOptionalSection(QWidget):
    """Natural-height disclosure inside the page's single main scroll area."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle = PushButton(title, self)
        self.toggle.setCheckable(True)
        self.toggle.setIcon(FluentIcon.CHEVRON_DOWN_MED)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.content.hide()
        self.toggle.toggled.connect(self._show_content)

    def _show_content(self, expanded):
        self.content.setVisible(expanded)
        self.toggle.setIcon(FluentIcon.UP if expanded else FluentIcon.CHEVRON_DOWN_MED)


class RoleSonataChoice(QWidget):
    """The same search/check pattern as matching, preserving hidden choices."""

    changed = Signal()

    def __init__(self, names=(), parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.search = LineEdit(self)
        self.search.setPlaceholderText('搜索套装 · 支持拼音 / 首字母')
        self.search.setClearButtonEnabled(True)
        self.table = QListWidget(self)
        self.table.setMinimumWidth(0)
        self.table.setFixedHeight(170)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setAccessibleName('允许套装，多选')
        self.warning = CaptionLabel(self)
        self.warning.setWordWrap(True)
        self.warning.hide()
        layout.addWidget(self.search)
        layout.addWidget(self.table)
        layout.addWidget(self.warning)
        self.search.textChanged.connect(self._search)
        self.table.itemChanged.connect(self._changed)
        self.set_names(names)

    def allowed(self):
        return tuple(
            self.table.item(i).data(Qt.UserRole)
            for i in range(self.table.count())
            if self.table.item(i).checkState() == Qt.Checked
        )

    def set_names(self, names):
        self.load(self.allowed(), names=names)

    def load(self, allowed, requirements=(), *, names=None):
        old = tuple(self.table.item(i).data(Qt.UserRole) for i in range(self.table.count()))
        names = tuple(old if names is None else names)
        values = sorted(set(names) | set(allowed) | {name for name, _ in requirements})
        self._loading = True
        self.table.clear()
        for name in values:
            item = QListWidgetItem(name)
            item.setSizeHint(QSize(0, 28))
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in allowed else Qt.Unchecked)
            self.table.addItem(item)
        self._loading = False
        self._search()

    def _search(self, *_):
        for i in range(self.table.count()):
            item = self.table.item(i)
            item.setHidden(not search_matches(item.data(Qt.UserRole), self.search.text()))

    def _changed(self, *_):
        if not self._loading:
            self.changed.emit()


class RoleEditorUX:
    def build_role_editor(self, snapshot, catalog, role, seed_key, seed_rule, evidence):
        self.snapshot, self.records = snapshot, catalog.records
        self.evidence, self.value = evidence, None
        self._syncing = False
        self.pin_model = PinModel(
            catalog, (slot.fixed_item_id for slot in role.slots) if role else (), self
        )
        self._avatars = self._sonata_snapshot = self._effects_snapshot = None
        self._avatar_revision = -1
        self.setWindowTitle('角色配装要求')
        fit_dialog(self, 1080, 820)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.settings_surface = self.surface = RoleSettingsSurface(self)
        layout.addWidget(self.surface)
        self.role_avatar = QLabel(self.surface.header)
        self.role_avatar.setFixedSize(36, 36)
        self.role_title = BodyLabel('角色配装要求', self.surface.header)
        self.role_title.setMinimumWidth(0)
        self.role_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.surface.header_layout.addWidget(self.role_avatar)
        self.surface.header_layout.addWidget(self.role_title, 1)
        self.surface.header_layout.setContentsMargins(4, 0, 4, 0)
        self.error = QLabel(self.surface.footer)
        self.error.setWordWrap(True)
        self.error.setMinimumWidth(0)
        self.error.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        left, right = self.surface.role_layout, self.surface.calculation_layout
        left.addWidget(BodyLabel('角色与套装', self))
        self.template = IdentityCombo(self)
        self.template_positions = {}
        for label, (character, modal, _) in snapshot.options.items():
            self.template_positions[(character, modal)] = self.template.count()
            self.template.addItem(label, (character, modal))
        key = role.key if role else tuple(seed_key) if seed_key else None
        if key is not None and key not in self.template_positions:
            self.template_positions[key] = self.template.count()
            self.template.addItem('失效选择：' + '/'.join(key), key)
        self.template.setCurrentIndex(self.template_positions.get(key, -1))
        self.template.hide()
        self.role_picker = RolePicker(snapshot, key=key)
        self.role_picker.hint.hide()
        left.addWidget(self.role_picker)
        form = QFormLayout()
        self.element = QComboBox(self)
        self.element.addItem('请选择明确元素', '')
        for element in sorted(ELEMENT_NAMES):
            self.element.addItem(element, element)
        # A matching handoff may propose a new rule. Keep old slot/quantity
        # requirements visible so conflicting changes need user confirmation.
        rule = seed_rule if seed_rule is not None else role.rule if role else None
        default_counts = ()
        if rule is None and key:
            from echo_workshop.resources.role_defaults import default_rule, default_sets

            runtime = getattr(self.parentWidget(), 'runtime', None)
            provider = getattr(runtime, 'role_defaults', None)
            defaults = provider.active if provider is not None else None
            rule = default_rule(key[0], defaults)
            default_counts = default_sets(key[0], defaults)
        self.element.setCurrentIndex(self.element.findData(rule.element if rule else ''))
        self.element.hide()  # 保留已保存属性与资料自动推导，不提供重复编辑栏。
        self.weight_options = PushButton('编辑模版权重', self)
        self.weight_options.clicked.connect(self._configure_weights)
        form.addRow(self.weight_options)
        self._sync_element()
        self.target = QDoubleSpinBox()
        self.target.setRange(0, 1000000)
        self.target.setMinimumHeight(36)
        self.target.setDecimals(2)
        self.target.setValue(role.target if role else 0)
        self.target.setToolTip('目标分达成后，会优先将好的声骸给其他同级角色。为0时不启用。')
        form.addRow('目标分', self.target)
        left.addLayout(form)
        basis = CaptionLabel('目标分达成后，会优先将好的声骸给其他同级角色。为0时不启用。', self)
        basis.setWordWrap(True)
        left.addWidget(basis)
        # Keep the old serializer/input API as an invisible adapter. Users fill
        # checks and numeric controls, never comma/equal-sign expressions.
        self.sonatas = QLineEdit(', '.join(rule.sonatas) if rule else '', self)
        self.requirements = QLineEdit(
            '; '.join(
                f'{name}={count}'
                for name, count in (role.set_requirements if role else default_counts)
            ),
            self,
        )
        self.sonatas.hide()
        self.requirements.hide()
        candidates = {
            record.sonata
            for record, usable in zip(catalog.records, catalog.usable)
            if usable and record.sonata
        }
        self.sonata_choices = self.set_controls = RoleSonataChoice(
            candidates | set(rule.sonatas if rule else ()), self
        )
        self.set_controls.load(
            rule.sonatas if rule else (), role.set_requirements if role else default_counts
        )
        left.addWidget(BodyLabel('套装配置', self))
        left.addWidget(self.set_controls)
        self.sonata_effects_picker = SonataEffectsPicker(self)
        self.sonata_effects_picker.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.sonata_effects_picker.load(
            rule.sonatas if rule else (), role.set_requirements if role else default_counts
        )
        self.set_controls.changed.connect(self._allowed_sets_changed)
        self.sonata_effects_picker.changed.connect(self._sets_to_text)
        self.sonatas.textChanged.connect(self._text_to_sets)
        self.requirements.textChanged.connect(self._text_to_sets)
        left.addWidget(self.sonata_effects_picker)
        self.budget = QSpinBox(self)
        self.budget.setRange(12, 12)
        self.budget.hide()
        if role is not None and role.cost_budget > 12:
            historical = QLabel(
                f'原历史预算 {role.cost_budget} 保留在原配置；本次重新保存的预算最多 12。取消不修改原数据。'
            )
            historical.setWordWrap(True)
            self.error.setText(historical.text())
        self.cost_picker = CostLayoutPicker(self)
        self.cost_layout_picker = self.cost_layout = self.cost_picker
        self.cost_picker.set_costs(
            tuple(slot.cost for slot in role.slots) if role else (4, 3, 3, 1, 1), allow_legacy=True
        )
        right.addWidget(self.cost_picker)
        self.main_requirements_picker = MainRequirementsPicker(self)
        self.main_requirements_picker.load(
            tuple(slot.cost for slot in role.slots) if role else (4, 3, 3, 1, 1),
            tuple(slot.main_names for slot in role.slots) if role else ((),) * 5,
        )
        self.main_requirements_picker.changed.connect(self._main_to_slots)
        right.addWidget(self.main_requirements_picker)
        self.fixed_disclosure = RoleOptionalSection('固定声骸（可选）', self)
        self.slots = QTableWidget(5, 3, self)
        self.slots.setHorizontalHeaderLabels(
            ['槽位 COST', '允许主词条（多选）', '固定声骸（可选）']
        )
        self.slots.setEditTriggers(QTableWidget.NoEditTriggers)
        self.slots.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.slots.verticalHeader().setDefaultSectionSize(40)
        self.slots.setMinimumHeight(235)
        for index in range(5):
            slot = role.slots[index] if role else SlotRule((4, 3, 3, 1, 1)[index])
            cost = QComboBox()
            for value in (1, 3, 4):
                cost.addItem(str(value), value)
            cost.setCurrentIndex(cost.findData(slot.cost))
            main = MainStatChoice(slot.cost, ', '.join(slot.main_names))
            cost.currentIndexChanged.connect(
                lambda _, cost=cost, main=main: main.set_options(cost.currentData())
            )
            cost.currentIndexChanged.connect(self._legacy_slots_changed)
            main.changed.connect(self._legacy_slots_changed)
            pin = PinSelector(self.pin_model, slot.fixed_item_id)
            pin.set_cost(slot.cost)
            pin.lineEdit().setPlaceholderText('搜索名称/套装/词条，选择一件')
            self.slots.setCellWidget(index, 0, cost)
            self.slots.setCellWidget(index, 1, main)
            self.slots.setCellWidget(index, 2, pin)
        self.slots.setColumnHidden(
            0, True
        )  # Adapter only; visible edits use explicit COST buttons.
        self.slots.setColumnHidden(1, True)
        self.slots.setHorizontalHeaderLabels(['', '', '选择固定声骸（空白表示不固定）'])
        self.cost_picker.changed.connect(self.apply_cost_layout)
        self.fixed_disclosure.content_layout.addWidget(
            CaptionLabel('仅在需要保留某件时使用。固定件也必须满足本页要求。', self)
        )
        self.fixed_disclosure.content_layout.addWidget(self.slots)
        right.addWidget(self.fixed_disclosure)
        # Preserve the objective's historical owner hint without exposing a
        # second requirements form. New saves use only per-set name counting.
        self.owner = QLineEdit(role.owner_label if role else '', self)
        self.owner.hide()
        self.buttons = buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('确认要求')
        buttons.button(QDialogButtonBox.Cancel).setText('返回')
        buttons.accepted.connect(self._save_cost_checked)
        buttons.rejected.connect(self.reject)
        self.surface.footer_layout.addWidget(self.error, 1)
        self.surface.footer_layout.addWidget(buttons)
        self.role_picker.changed.connect(self._picker_to_template)
        self.template.currentIndexChanged.connect(self._template_to_picker)
        self.template.currentIndexChanged.connect(self._sync_element)
        self.template.currentIndexChanged.connect(self._refresh_identity)
        self._avatar_timer = QTimer(self)
        self._avatar_timer.setInterval(500)
        self._avatar_timer.timeout.connect(self._refresh_avatar)
        runtime = getattr(self.parentWidget(), 'runtime', None)
        if runtime is not None:
            self.set_resources(
                avatars=runtime.avatars,
                sonata=runtime.sonata.active,
                effects=runtime.sonata_effects.active,
            )
        else:
            # Real built-in tiers are offline assets, not inferred from names.
            from echo_workshop.resources.effects import SonataEffectsProvider, _snapshot

            try:
                from echo_workshop.resources.paths import builtin_path

                data = SonataEffectsProvider._read(builtin_path('sonata_effects_catalog.json'))
                self.set_resources(effects=_snapshot(data['items'], data['source']))
            except (OSError, ValueError, KeyError):
                pass
        self._refresh_identity()
        self.surface.update_theme()
        self._filter_pins(self.cost_picker.costs())
        self.mark_confirmed_state()

    def edit_state(self):
        return (
            self.template.currentData(),
            self.target.value(),
            self.element.currentData(),
            self.set_controls.allowed(),
            self.requirements.text(),
            self.owner.text(),
            tuple(
                (
                    self.slots.cellWidget(i, 0).currentData(),
                    self.slots.cellWidget(i, 1).text(),
                    self.slots.cellWidget(i, 2).currentData(),
                    self.slots.cellWidget(i, 2).currentText(),
                )
                for i in range(5)
            ),
        )

    def mark_confirmed_state(self):
        self._confirmed_state = self.edit_state()

    def confirm_return(self):
        from PySide6.QtWidgets import QMessageBox

        if self.edit_state() == self._confirmed_state:
            return True
        return (
            QMessageBox.question(
                self,
                '要求尚未确认',
                '当前修改尚未确认，返回将放弃这些修改。是否返回？',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        )

    def set_resources(self, *, avatars=None, sonata=None, effects=None):
        if avatars is not None:
            self._avatars = avatars
        if sonata is not None:
            self._sonata_snapshot = getattr(sonata, 'active', sonata)
        if effects is not None:
            self._effects_snapshot = getattr(effects, 'active', effects)
        names = set(self.set_controls.allowed())
        names.update(record.sonata for record in self.records if record.sonata)
        names.update(icon.name for icon in getattr(self._sonata_snapshot, 'icons', {}).values())
        names.update(
            row.name for row in (getattr(self._effects_snapshot, 'entries', None) or {}).values()
        )
        self.set_controls.set_names(names)
        self._text_to_sets()
        self._refresh_identity()

    def _tiers(self, names):
        return {
            name: self._effects_snapshot.tiers(name) if self._effects_snapshot is not None else ()
            for name in names
        }

    def _descriptions(self, names):
        return {
            name: self._effects_snapshot.descriptions(name)
            if self._effects_snapshot is not None
            else {}
            for name in names
        }

    def _refresh_identity(self, *_):
        key = self.template.currentData()
        self.role_title.setText(self.template.currentText() if key else '选择角色与模版')
        if self._avatars is not None and key:
            self._avatars.request(key[0])  # One lazy request per identity; cache is single-flight.
        self._refresh_avatar(force=True)

    def _refresh_avatar(self, *, force=False):
        key = self.template.currentData()
        revision = getattr(self._avatars, 'revision', -1)
        if not force and revision == self._avatar_revision:
            return
        self._avatar_revision = revision
        body = self._avatars.lookup(key[0]) if self._avatars is not None and key else None
        pixmap = QPixmap()
        if body and pixmap.loadFromData(body):
            self.role_avatar.setPixmap(
                pixmap.scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self.role_avatar.setPixmap(FluentIcon.PEOPLE.icon().pixmap(30, 30))

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, '_avatar_timer'):
            self._avatar_timer.start()
            self._refresh_avatar(force=True)

    def hideEvent(self, event):
        if hasattr(self, '_avatar_timer'):
            self._avatar_timer.stop()
        super().hideEvent(event)

    def _legacy_slots_changed(self, *_):
        if self._syncing or any(
            self.slots.cellWidget(i, column) is None for i in range(5) for column in (0, 1)
        ):
            return
        costs = tuple(self.slots.cellWidget(i, 0).currentData() for i in range(5))
        rows = tuple(
            tuple(
                x.strip()
                for x in self.slots.cellWidget(i, 1).text().replace('，', ',').split(',')
                if x.strip()
            )
            for i in range(5)
        )
        self.cost_picker.set_costs(costs, allow_legacy=True)
        self.main_requirements_picker.load(costs, rows)
        self._filter_pins(costs)

    def _main_to_slots(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            for index, names in enumerate(self.main_requirements_picker.main_names()):
                self.slots.cellWidget(index, 1).setText(', '.join(names))
        finally:
            self._syncing = False

    def _configure_weights(self):
        from echo_workshop.ui.widgets.template_weights import open_template_weights

        try:
            task = getattr(self.parentWidget(), 'task', None)
            open_template_weights(task, self.template.currentData())
        except (ValueError, KeyError) as error:
            self.error.setText(str(error))

    def _allowed_sets_changed(self):
        names = self.set_controls.allowed()
        self.sonata_effects_picker.set_allowed(names, self._tiers(names), default_max=True)
        self.sonata_effects_picker.set_descriptions(self._descriptions(names))
        self._sets_to_text()

    def apply_cost_layout(self, costs):
        costs = read_cost_layout(costs)
        self.cost_picker.set_costs(costs)
        self._syncing = True
        try:
            for index, cost in enumerate(costs):
                control = self.slots.cellWidget(index, 0)
                control.setCurrentIndex(control.findData(cost))
        finally:
            self._syncing = False
        self.main_requirements_picker.set_costs(costs)
        self._filter_pins(costs)
        issues = self._cost_layout_conflicts()
        if issues:
            self.error.setText(
                'COST 布局已带入；原主词条和固定件保留。请调整后保存：' + '；'.join(issues)
            )

    def _filter_pins(self, costs):
        for index, cost in enumerate(costs):
            self.slots.cellWidget(index, 2).set_cost(cost)
            self.slots.setVerticalHeaderItem(index, QTableWidgetItem(f'{index + 1} · C{cost}'))

    def _cost_layout_conflicts(self):
        issues = []
        for index in range(5):
            cost = self.slots.cellWidget(index, 0).currentData()
            main = self.slots.cellWidget(index, 1).text()
            unsupported = [
                name.strip()
                for name in main.replace('，', ',').split(',')
                if name.strip() and name.strip() not in MAX_MAINSTAT_VALUES[cost][0]
            ]
            if unsupported:
                issues.append(
                    f'第 {index + 1} 槽主词条 {"、".join(unsupported)} 与 COST {cost} 不兼容'
                )
            try:
                item_id = self.slots.cellWidget(index, 2).selected_id()
            except ValueError:
                issues.append(f'第 {index + 1} 槽固定声骸尚未选定')
                continue
            if item_id:
                position = self.pin_model.catalog.positions.get(item_id, 0)
                record = self.records[position - 1] if position else None
                if record is None:
                    issues.append(f'第 {index + 1} 槽固定声骸不在当前候选快照中')
                elif record.cost != cost:
                    issues.append(
                        f'第 {index + 1} 槽固定声骸实际 COST {record.cost}，与所选 COST {cost} 不一致；请重新选择或解除固定'
                    )
        return issues

    def validate_cost_layout(self):
        self.main_requirements_picker.validate()
        self.sonata_effects_picker.validate()
        costs = read_cost_layout(
            tuple(self.slots.cellWidget(index, 0).currentData() for index in range(5))
        )
        issues = self._cost_layout_conflicts()
        if issues:
            raise ValueError('；'.join(issues))
        return costs

    def _save_cost_checked(self):
        try:
            role = self._build_role()
        except (ValueError, KeyError, TypeError) as error:
            self.error.setText('请检查配置：' + str(error))
            return
        runtime = getattr(self.parentWidget(), 'runtime', None)
        if runtime is None:
            self.value = role
            self.accept()
            return
        from echo_workshop.core.requirements import RoleRequirements

        try:
            request = runtime.requirements_checks.start(
                {(role.character_id, role.modal_id): RoleRequirements.from_role(role)},
                cancel_event=threading.Event(),
            )
        except Exception as error:
            self.error.setText('请检查配置：' + str(error))
            return
        self._check_request, self._check_role = request, role
        if getattr(self, '_check_timer', None) is None:
            self._check_timer = QTimer(self)
            self._check_timer.setInterval(20)
            self._check_timer.timeout.connect(self._poll_requirement_check)
        self._check_timer.start()
        self.error.setText('正在校验角色要求…')

    def _poll_requirement_check(self):
        request = getattr(self, '_check_request', None)
        if request is None or not request.future.done():
            return
        runtime = getattr(self.parentWidget(), 'runtime', None)
        from echo_workshop.application.compute_protocol import ComputeFailure

        try:
            if runtime is None:
                raise ValueError('运行时已关闭')
            errors = runtime.requirements_checks.response(request)
            rejected = next((message for _, message in errors if message), None)
            if rejected:
                raise ValueError(rejected)
        except (ValueError, ComputeFailure) as error:
            self._check_timer.stop()
            self._check_request = None
            self.error.setText('请检查配置：' + str(error))
            return
        self._check_timer.stop()
        self._check_request = None
        self.value = self._check_role
        self.error.clear()
        self.accept()

    def _sync_element(self, *_):
        key = self.template.currentData()
        known = self.snapshot.character_elements.get(key[0], '') if key else ''
        self.element.setEnabled(not known)
        if known:
            self.element.setCurrentIndex(self.element.findData(known))
        elif getattr(self, '_previous_element_known', False):
            self.element.setCurrentIndex(0)
        self._previous_element_known = bool(known)
        self.element.setToolTip(
            '角色属性已自动获取：' + known if known else '属性未更新，请检查更新或明确选择'
        )

    def _picker_to_template(self):
        if not self._syncing:
            self._syncing = True
            self.template.setCurrentIndex(self.template_positions.get(self.role_picker.key(), -1))
            self._syncing = False

    def _template_to_picker(self, *_):
        if not self._syncing:
            self._syncing = True
            self.role_picker.reload(self.snapshot, self.template.currentData())
            self._syncing = False

    def _sets_to_text(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.sonatas.setText(', '.join(self.set_controls.allowed()))
            self.requirements.setText(
                '; '.join(
                    f'{name}={count}' for name, count in self.sonata_effects_picker.requirements()
                )
            )
        finally:
            self._syncing = False

    def _text_to_sets(self, *_):
        if self._syncing:
            return
        try:
            allowed = tuple(
                name.strip()
                for name in self.sonatas.text().replace('，', ',').split(',')
                if name.strip()
            )
            requirements = []
            for part in self.requirements.text().replace('；', ';').split(';'):
                if part.strip():
                    name, count = part.rsplit('=', 1)
                    requirements.append((name.strip(), int(count.strip())))
            if any(not 1 <= count <= 5 for _, count in requirements):
                raise ValueError('套装数量应为 1–5')
            self.set_controls.load(allowed, tuple(requirements))
            self.sonata_effects_picker.load(allowed, tuple(requirements), self._tiers(allowed))
            self.sonata_effects_picker.set_descriptions(self._descriptions(allowed))
            self.set_controls.warning.clear()
            self.set_controls.warning.hide()
        except ValueError:
            self.set_controls.warning.setText(
                '原数量配置需要核对；原值保留，使用选择控件重新填写后再保存。'
            )
            self.set_controls.warning.show()


class RoleEditorContent(RoleEditorUX):
    def __init__(
        self,
        snapshot,
        role=None,
        parent=None,
        *,
        catalog,
        seed_key=None,
        seed_rule=None,
        evidence=None,
    ):
        super().__init__(parent)
        self.build_role_editor(snapshot, catalog, role, seed_key, seed_rule, evidence)

    def _build_role(self):
        self.validate_cost_layout()
        if self.template.currentData() is None:
            raise ValueError('请明确选择角色和模版')
        character, modal = self.template.currentData()
        self.snapshot.template(character, modal)
        requirements = []
        for part in self.requirements.text().replace('；', ';').split(';'):
            if part.strip():
                name, count = part.rsplit('=', 1)
                requirements.append((name.strip(), int(count.strip())))
        slots = tuple(
            SlotRule(
                self.slots.cellWidget(index, 0).currentData(),
                _names(self.slots.cellWidget(index, 1).text()),
                self.slots.cellWidget(index, 2).selected_id(),
            )
            for index in range(5)
        )
        return RoleRequest(
            character,
            modal,
            self.target.value(),
            self.snapshot.compatibility_rule(
                character,
                CompatibilityRule(self.element.currentData(), _names(self.sonatas.text())),
            ),
            slots,
            tuple(requirements),
            12,
            False,
            self.owner.text().strip(),
            set_unique_names=True,
        )

    def save(self):
        try:
            self.value = self._build_role()
            self.error.clear()
            self.accept()
        except (ValueError, KeyError, TypeError) as error:
            self.error.setText('请检查配置：' + str(error))


def _names(text):
    return tuple(value.strip() for value in text.replace('，', ',').split(',') if value.strip())
