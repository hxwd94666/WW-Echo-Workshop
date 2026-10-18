# 管理匹配角色适配要求计算选项和未保存草稿。
from PySide6.QtCore import Qt, QTimer
from concurrent.futures import CancelledError
import threading
from PySide6.QtWidgets import QVBoxLayout, QWidget
from echo_workshop.ui.widgets.inputs import QSpinBox
from qfluentwidgets import BodyLabel, CaptionLabel, PushButton, LineEdit
from echo_workshop.ui.widgets.inputs import ComboBox
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.application.pins import pin_error_text
from echo_workshop.core.cancellation import OperationCancelled


class MatchingRoleSettingsController:
    def _build_managed_rules(self):
        from echo_workshop.ui.inventory.filters import CheckableSonataList

        box = self.edit_controls.role_layout
        box.addWidget(BodyLabel('此模版适配范围'))
        # 属性仍由角色资料与已保存要求维护，页面不再提供重复编辑入口。
        self.element_rule.hide()
        box.addWidget(self.weight_options)
        note = CaptionLabel('允许套装支持多选；只筛选单件所属套装，不校验整套件数。')
        note.setWordWrap(True)
        box.addWidget(note)
        self.managed_sonatas = CheckableSonataList(self)
        self.managed_sonatas.setMinimumHeight(220)
        self.managed_sonatas.setMaximumHeight(300)
        self.managed_sonatas.itemChanged.connect(self._managed_sonata_edited)
        self.managed_sonata_search = LineEdit(self)
        self.managed_sonata_search.setPlaceholderText('搜索套装（支持拼音）')
        self.managed_sonata_search.setClearButtonEnabled(True)
        self.managed_sonata_search.textChanged.connect(self._filter_managed_sonatas)
        box.addWidget(self.managed_sonata_search)
        box.addWidget(self.managed_sonatas)

    def _sync_managed_sonatas(self):
        if not hasattr(self, 'managed_sonatas'):
            return
        from PySide6.QtWidgets import QListWidgetItem

        checked = tuple(
            x.strip() for x in self.sonata_rule.text().replace('，', ',').split(',') if x.strip()
        )
        state = self.runtime.sonata.active.version, checked
        if state == getattr(self, '_managed_sonata_state', None):
            return
        self._managed_sonata_state = state
        names = {icon.name for icon in self.runtime.sonata.active.icons.values()}
        names.update(checked)  # Retain historical/custom names, never silently drop.
        blocked = self.managed_sonatas.blockSignals(True)
        try:
            self.managed_sonatas.clear()
            for name in sorted(names):
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if name in checked else Qt.Unchecked)
                self.managed_sonatas.addItem(item)
        finally:
            self.managed_sonatas.blockSignals(blocked)
        self._filter_managed_sonatas()

    def _filter_managed_sonatas(self, *_):
        if not hasattr(self, 'managed_sonata_search'):
            return
        from echo_workshop.resources.search import search_keys

        needle = ''.join(self.managed_sonata_search.text().casefold().split())
        for i in range(self.managed_sonatas.count()):
            item = self.managed_sonatas.item(i)
            item.setHidden(
                bool(needle and needle not in search_keys(item.data(Qt.UserRole)).casefold())
            )

    def _managed_sonata_edited(self, *_):
        names = [
            self.managed_sonatas.item(i).data(Qt.UserRole)
            for i in range(self.managed_sonatas.count())
            if self.managed_sonatas.item(i).checkState() == Qt.Checked
        ]
        self.sonata_rule.setText(', '.join(names))

    def _sync_managed_header(self, *_):
        if not hasattr(self, 'managed_role_header'):
            return
        key = self.participants.currentData()
        selected = key in self.selections() if key is not None else False
        text = '请选择参与角色右侧的管理按钮'
        if selected:
            try:
                text = self.runtime.weights.active.template(*key)['name']
            except (ValueError, KeyError):
                text = '失效角色：' + '/'.join(key)
        self.managed_role_header.setText(text)
        if hasattr(self, 'managed_avatar'):
            button = self.role_strip.buttons.get(key) if hasattr(self, 'role_strip') else None
            from qfluentwidgets import FluentIcon

            from qfluentwidgets import isDarkTheme

            avatars = getattr(self.runtime, 'avatars', None)
            revision = getattr(avatars, 'revision', None)
            state = key if selected else None, avatars, revision, isDarkTheme()
            if revision is None or state != getattr(self, '_managed_avatar_state', None):
                self._managed_avatar_state = state
                cached = avatars.lookup(key[0]) if selected and avatars is not None else None
                if button is not None:
                    icon = button.icon() if cached is not None else FluentIcon.PEOPLE.icon()
                    self.managed_avatar.setPixmap(icon.pixmap(36, 36))
                else:
                    self.managed_avatar.clear()
        for name in (
            'weight_options',
            'managed_sonatas',
            'managed_sonata_search',
            'cost_layout_picker',
            'main_requirements_picker',
            'fixed_items_picker',
        ):
            control = getattr(self, name, None)
            if control is not None:
                control.setEnabled(selected)
        self._sync_cost_layout_picker(key if selected else None)
        self._sync_requirement_controls(key if selected else None)

    def _saved_role_costs(self, key):
        from echo_workshop.core.costs import DEFAULT_COST_LAYOUT, read_cost_layout

        if key in self._cost_layouts:
            return read_cost_layout(self._cost_layouts[key])
        return DEFAULT_COST_LAYOUT

    def role_cost_layouts(self):
        return {key: self._saved_role_costs(key) for key in self.selections()}

    def _sync_cost_layout_picker(self, key=None):
        if not hasattr(self, 'cost_layout_picker') or key is None:
            return
        from echo_workshop.core.costs import DEFAULT_COST_LAYOUT

        try:
            costs = (
                self._draft_costs
                if self._requirements_context == key and self._draft_costs is not None
                else self._saved_role_costs(key)
            )
            note = ''
        except ValueError as error:
            costs, note = DEFAULT_COST_LAYOUT, str(error)
        state = (self.profile_box.currentData(), key, costs, note)
        if state != self._cost_picker_state:
            self._cost_picker_state = state
            self.cost_layout_picker.set_costs(costs)
            self.cost_layout_note.setText(note)
            self.cost_layout_note.setVisible(bool(note))

    def _cost_layout_edited(self, costs):
        from echo_workshop.core.costs import read_cost_layout

        key = self.participants.currentData()
        if key not in self.selections():
            raise ValueError('请先选择参与角色，再设置 COST。')
        costs = read_cost_layout(costs)
        self._draft_costs = costs
        self._cost_picker_state = None
        self._sync_cost_layout_picker(key)
        if hasattr(self, 'main_requirements_picker'):
            self.main_requirements_picker.set_costs(costs)
            if self.fixed_items_picker.boxes:
                self.fixed_items_picker.set_costs(costs)
            self._rule_edited()
        self._requirements_form_state = None
        self._refresh_controls()

    def _saved_role_requirements(self, key):
        from echo_workshop.core.requirements import RoleRequirements

        if key in self._role_requirements:
            return self._role_requirements[key]
        return RoleRequirements(
            self._saved_role_costs(key),
            ((),) * 5,
            self.effective_rule(key).sonatas,
        )

    def role_requirements(self):
        from echo_workshop.core.requirements import RoleRequirements

        values = {}
        for key in self.selections():
            saved = self._saved_role_requirements(key)
            rule = self._rules.get(key)
            allowed = rule.sonatas if rule is not None else saved.allowed_sonatas
            values[key] = RoleRequirements(
                self._saved_role_costs(key),
                saved.main_names,
                allowed,
                saved.set_requirements,
                saved.fixed_item_ids,
            )
        return values

    def _sync_requirement_controls(self, key):
        if (
            key is None
            or not hasattr(self, 'main_requirements_picker')
            or self._rule_dirty
            or self._saving_rule
        ):
            return
        if not self.edit_controls.toggle.isChecked():
            return  # Hidden settings never build full-inventory UUID selectors.
        try:
            saved = self._saved_role_requirements(key)
            main, pins = saved.main_names, saved.fixed_item_ids
            self.requirements_note.clear()
            self.requirements_note.hide()
        except (ValueError, TypeError, KeyError) as error:
            self.requirements_note.setText('原装备要求保留，请重新选择并保存：' + str(error))
            self.requirements_note.show()
            main, pins = ((),) * 5, None
        costs = self._saved_role_costs(key)
        state = (
            self.profile_box.currentData(),
            key,
            costs,
            main,
            pins,
            id(self.item_picker.catalog),
        )
        if state == self._requirements_form_state:
            return
        self._requirements_form_state = state
        self._requirements_context = key
        self._draft_costs = costs
        self.cost_layout_picker.set_costs(costs)
        self.main_requirements_picker.load(costs, main)
        self.fixed_items_picker.load(self.item_picker.fixed_catalog, costs, pins)
        self._requirements_baseline = (
            costs,
            self.main_requirements_picker.main_names(),
            self.fixed_items_picker.item_ids(),
        )

    def _configure_managed_weights(self):
        key = self.participants.currentData()
        if key not in self.selections() or self._rule_dirty:
            raise ValueError('请选择参与角色，并先修正角色要求的错误，再编辑权重。')
        from echo_workshop.ui.widgets.template_weights import open_template_weights

        open_template_weights(self.task, key)

    def _build_calculation_controls(self):
        from PySide6.QtWidgets import QFormLayout
        from qfluentwidgets import CheckBox

        box = self.edit_controls.calculation_layout
        # 左侧集中适配与可选要求，右侧集中五槽配置，避免右栏独自过长。
        role_box = self.edit_controls.role_layout
        from echo_workshop.ui.allocation.costs import CostLayoutPicker

        self.cost_layout_picker = CostLayoutPicker(self)
        self.cost_layout_picker.changed.connect(
            lambda costs: self.action(lambda: self._cost_layout_edited(costs))
        )
        box.addWidget(self.cost_layout_picker)
        self.cost_layout_note = CaptionLabel(self)
        self.cost_layout_note.hide()
        self.cost_layout_note.setWordWrap(True)
        box.addWidget(self.cost_layout_note)
        box.addWidget(BodyLabel('主词条自选'))
        from echo_workshop.ui.allocation.requirements import MainRequirementsPicker

        self.main_requirements_picker = MainRequirementsPicker(self, title='')
        self.main_requirements_picker.changed.connect(self._rule_edited)
        box.addWidget(self.main_requirements_picker)
        self.requirements_note = CaptionLabel(self)
        self.requirements_note.hide()
        self.requirements_note.setWordWrap(True)
        box.addWidget(self.requirements_note)
        from echo_workshop.ui.allocation.editor import RoleOptionalSection
        from echo_workshop.ui.allocation.pin_model import FixedItemsPicker

        self.fixed_disclosure = RoleOptionalSection('固定声骸（可选）', self)
        pin_note = CaptionLabel(
            '固定槽只接受该声骸，其他声骸仍可匹配未固定槽；不分配五件，也不占用其他角色。候选范围不变。',
            self,
        )
        pin_note.setWordWrap(True)
        self.fixed_disclosure.content_layout.addWidget(pin_note)
        self.fixed_items_picker = FixedItemsPicker(self)
        self.fixed_items_picker.changed.connect(self._rule_edited)
        self.fixed_disclosure.content_layout.addWidget(self.fixed_items_picker)
        role_box.addWidget(self.fixed_disclosure)
        # One authoritative global settings surface; it is never inserted into
        # a role card. The assistant settings panel mounts these same controls.
        self.global_calculation_settings = QWidget(self)
        self.global_calculation_settings.hide()
        global_box = QVBoxLayout(self.global_calculation_settings)
        global_box.setContentsMargins(0, 0, 0, 0)
        note = CaptionLabel(
            '评分范围作用于当前档案的所有匹配角色；更改后点击开始计算。配装搜索预算只影响整套配装。'
        )
        note.setWordWrap(True)
        global_box.addWidget(note)
        form = QFormLayout()
        self.calculation_level = ComboBox()
        self.calculation_level.addItem('不限等级', userData=0)
        for level in (5, 10, 15, 20, 25):
            self.calculation_level.addItem(f'+{level} 及以上', userData=level)
        form.addRow('最低等级', self.calculation_level)
        self.calculation_locked = CheckBox('评分时排除保护声骸')
        self.calculation_equipped = CheckBox('使用已装备声骸（历史角色归属）')
        form.addRow(self.calculation_locked)
        form.addRow(self.calculation_equipped)
        self.calculation_seconds = QSpinBox()
        self.calculation_seconds.setRange(1, 120)
        self.calculation_seconds.setSuffix(' 秒')
        self.calculation_nodes = QSpinBox()
        self.calculation_nodes.setRange(1, 5000000)
        form.addRow('整套搜索总时间', self.calculation_seconds)
        form.addRow('整套搜索总节点', self.calculation_nodes)
        global_box.addLayout(form)
        self.budget_notice = CaptionLabel(self.global_calculation_settings)
        self.budget_notice.setWordWrap(True)
        global_box.addWidget(self.budget_notice)
        note = CaptionLabel(
            '整套方案始终排除非固定的保护件。时间与节点预算由全部优先级组共用；达到上限显示当前结果，不保证全局最优。'
        )
        note.setWordWrap(True)
        global_box.addWidget(note)
        self._sync_calculation_controls()
        self.reset_calculation_button = PushButton('重置计算选项', self)
        self.reset_calculation_button.clicked.connect(lambda: self.action(self._reset_calculation))
        global_box.addWidget(self.reset_calculation_button)
        for widget, signal in (
            (self.calculation_level, 'currentIndexChanged'),
            (self.calculation_locked, 'toggled'),
            (self.calculation_equipped, 'toggled'),
        ):
            getattr(widget, signal).connect(self._calculation_edited)
        self.calculation_seconds.valueChanged.connect(lambda _: self.action(self._budget_edited))
        self.calculation_nodes.valueChanged.connect(lambda _: self.action(self._budget_edited))

    def _sync_calculation_controls(self):
        options = self._calculation_options
        for widget, setter, value in (
            (
                self.calculation_level,
                'setCurrentIndex',
                self.calculation_level.findData(options.minimum_level),
            ),
            (self.calculation_locked, 'setChecked', options.exclude_locked),
            (self.calculation_equipped, 'setChecked', options.include_equipped),
        ):
            blocked = widget.blockSignals(True)
            getattr(widget, setter)(value)
            widget.blockSignals(blocked)
        from echo_workshop.application.search_budget import allocation_budget

        try:
            budget = allocation_budget(self.task.config, self.profile_box.currentData())
        except (ValueError, KeyError, TypeError) as error:
            self._budget_error = str(error)
        else:
            self._budget_error = ''
            for widget, value in (
                (self.calculation_seconds, budget.seconds),
                (self.calculation_nodes, budget.nodes),
            ):
                blocked = widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(blocked)
        for widget in (self.calculation_seconds, self.calculation_nodes):
            widget.setEnabled(not self._budget_error)
            widget.setToolTip(self._budget_error)
        self.budget_notice.setText('配装搜索预算待修复，原设置保留。' if self._budget_error else '')
        self.budget_notice.setToolTip(self._budget_error)
        self.budget_notice.setVisible(bool(self._budget_error))

    def _budget_edited(self, *_):
        from echo_workshop.application.search_budget import SearchBudget, set_allocation_budget

        budget = SearchBudget(self.calculation_seconds.value(), self.calculation_nodes.value())
        try:
            set_allocation_budget(self.task.config, self.profile_box.currentData(), budget)
        except (ValueError, KeyError, TypeError):
            self._sync_calculation_controls()
            raise
        self.task._panel.feedback.setText('配装搜索预算已保存，下一次生成生效。')

    def _calculation_edited(self, *_):
        from echo_workshop.application.matching.calculation import MatchCalculationOptions

        self._calculation_options = MatchCalculationOptions(
            self.calculation_level.currentData(),
            self.calculation_locked.isChecked(),
            self.calculation_equipped.isChecked(),
            self._calculation_options.seconds,
            self._calculation_options.nodes,
        )
        self.changed()

    def _reset_calculation(self):
        from echo_workshop.application.matching.calculation import MatchCalculationOptions

        if self._rule_dirty:
            raise ValueError('请先修正当前角色要求的错误，再重置计算选项。')
        from echo_workshop.application.search_budget import SearchBudget, set_allocation_budget

        set_allocation_budget(self.task.config, self.profile_box.currentData(), SearchBudget())
        self._calculation_options = MatchCalculationOptions()
        self._calculation_error = ''
        self._sync_calculation_controls()
        self.changed()
        self.status.setText('计算选项已重置；配装优先级、适配要求与装备配置保留，请点击开始计算。')

    def load_rule(self, *_):
        key = self.state.current_role
        if self._rule_dirty:
            baseline_key = self._rule_baseline[0]
            if key != baseline_key:
                blocked = self.participants.blockSignals(True)
                self.participants.setCurrentIndex(self.participants.findData(baseline_key))
                self.participants.blockSignals(blocked)
                self.state.current_role = baseline_key
            return  # Passive refresh or context change never discards typed fields.
        a, b = self.element_rule.blockSignals(True), self.sonata_rule.blockSignals(True)
        try:
            super().load_rule()
            self._rule_baseline = (key, self.element_rule.currentData(), self.sonata_rule.text())
            self._sync_managed_sonatas()
            self._sync_managed_header()
            if hasattr(self, 'role_facts'):
                element = self.element_rule.currentData() or '资料未获取'
                self.role_facts.setText(
                    '角色属性：' + element if key else '先选择角色与模版。角色属性会随资料更新。'
                )
        finally:
            self.element_rule.blockSignals(a)
            self.sonata_rule.blockSignals(b)

    def _rule_edited(self, *_):
        if (
            self._loading
            or self._closing
            or self._saving_rule
            or self._rule_baseline is None
            or self._rule_baseline[0] is None
        ):
            return
        values = (self._rule_baseline[0], self.element_rule.currentData(), self.sonata_rule.text())
        requirements = (
            (
                self.cost_layout_picker.costs(),
                self.main_requirements_picker.main_names(),
                self._draft_fixed_items(),
            )
            if hasattr(self, 'main_requirements_picker')
            else self._requirements_baseline
        )
        self._rule_dirty = (
            values != self._rule_baseline or requirements != self._requirements_baseline
        )
        self._editor = self.edit_controls if self._rule_dirty else None
        if not self._rule_dirty and self._rule_save_error:
            self.status.setText('配置已更新；请点击开始计算。')
        self._rule_save_error = ''
        if self._rule_dirty and not self._autosave_scheduled:
            self._autosave_scheduled = True
            # 合并同一选择动作的多个信号，等控件结束更新后再提交。
            QTimer.singleShot(0, self, self._autosave_rule)
        self.task._panel.refresh_profile_guard()
        self._refresh_controls()

    def _draft_fixed_items(self):
        try:
            return self.fixed_items_picker.item_ids()
        except ValueError:
            # Typed but unselected search is an unsaved draft, never an old pin.
            return tuple(box.currentText() for box in self.fixed_items_picker.boxes)

    def _autosave_rule(self):
        self._autosave_scheduled = False
        if not self._rule_dirty or self._loading or self._closing:
            return
        self._saving_rule = True
        previous = self._preserve_edit_widgets
        self._preserve_edit_widgets = True
        try:
            self.save_rule()
        except (ValueError, KeyError, TypeError, OSError) as error:
            self._pending_rule_draft = None
            if self._rule_save_request is not None:
                self._rule_save_cancel.set()
            self._rule_save_error = '自动保存未完成：' + str(error)
            self.status.setText(self._rule_save_error)
        finally:
            self._saving_rule = False
            self._preserve_edit_widgets = previous
            self.task._panel.refresh_profile_guard()
            self._refresh_controls()

    def _rule_save_context(self):
        return (
            self._rule_save_epoch,
            self.profile_box.currentData(),
            self.participants.currentData(),
            self.element_rule.currentData(),
            self.sonata_rule.text(),
            self.cost_layout_picker.costs(),
            self.main_requirements_picker.main_names(),
            self._draft_fixed_items(),
            id(self.item_picker.catalog),
            self.runtime.weights.active.version,
            id(self._role_requirements),
            id(self._cost_layouts),
            id(self._rules),
        )

    def _queue_rule_save(self, draft):
        self._pending_rule_draft = draft
        if self._rule_save_request is not None:
            self._rule_save_cancel.set()
        else:
            self._start_rule_validation()

    def _start_rule_validation(self):
        draft, self._pending_rule_draft = self._pending_rule_draft, None
        if draft is None or self._closing:
            return
        self._rule_save_cancel = threading.Event()
        self._rule_save_job = begin_job('matching-rule-save')
        self._rule_save_job.stage('validating')
        try:
            self._rule_save_request = self.runtime.pin_checks.start(
                {draft['key']: draft['value']},
                draft['catalog'],
                cancel_event=self._rule_save_cancel,
            )
        except Exception as error:
            self._rule_save_job.finish('failed', error=error)
            raise
        self._active_rule_draft = draft
        if self._rule_save_timer is None:
            self._rule_save_timer = QTimer(self)
            self._rule_save_timer.setInterval(20)
            self._rule_save_timer.timeout.connect(self._poll_rule_validation)
        self._rule_save_timer.start()
        self.status.setText('正在校验角色要求…')

    def _poll_rule_validation(self):
        request = self._rule_save_request
        if request is None or not request.future.done():
            return  # No result()/join()/native call on an unfinished GUI request.
        draft = self._active_rule_draft
        previous = self._preserve_edit_widgets
        self._saving_rule = True
        self._preserve_edit_widgets = True
        try:
            if self._closing or self._rule_save_cancel.is_set():
                raise OperationCancelled('要求校验已取消')
            if draft['context'] != self._rule_save_context():
                raise OperationCancelled('要求上下文已变化')
            errors = self.runtime.pin_checks.response(request)
            if errors:
                _, slot, code = errors[0]
                raise ValueError(pin_error_text(slot, code))
            self._rule_save_job.stage('saving')
            self._commit_rule_save(draft)
        except (OperationCancelled, CancelledError):
            self._rule_save_job.finish('cancelled')
            if not self._closing and self._pending_rule_draft is None:
                self.status.setText('要求校验已取消，草稿保留；请重新编辑确认。')
        except Exception as error:
            if isinstance(error, ComputeFailure) and error.kind in ('cancelled', 'closed'):
                self._rule_save_job.finish('cancelled')
                if not self._closing and self._pending_rule_draft is None:
                    self.status.setText('要求校验已取消，草稿保留；请重新编辑确认。')
            else:
                self._rule_save_job.finish('failed', error=error)
                self._rule_save_error = '自动保存未完成：' + str(error)
                self.status.setText(self._rule_save_error)
        else:
            self._rule_save_job.finish()
        finally:
            self._saving_rule = False
            self._preserve_edit_widgets = previous
            self._rule_save_request = self._active_rule_draft = self._rule_save_job = None
            self._rule_save_timer.stop()
            if not self._closing:
                self.task._panel.refresh_profile_guard()
                self._refresh_controls()
                self._start_rule_validation()

    def stop_worker(self):
        # Host unload may call from its executor: cancel without touching Qt.
        self._rule_save_epoch += 1
        self._pending_rule_draft = None
        if self._rule_save_request is not None:
            self._rule_save_cancel.set()
            self._rule_save_job.finish('cancelled')
        super().stop_worker()

    def save_rule(self):
        if self._calculation_error:
            raise ValueError(self._calculation_error)
        from echo_workshop.core.requirements import RoleRequirements

        key = self.participants.currentData()
        if key not in self.selections():
            raise ValueError('请先选择参与角色。')
        names = tuple(
            name.strip()
            for name in self.sonata_rule.text().replace('，', ',').split(',')
            if name.strip()
        )
        from echo_workshop.core.allocation.models import set_name

        # Legacy count fields round-trip for retained sets, but are not editable
        # or evaluated by single-item matching. Removing a set removes its count.
        allowed = {set_name(name) for name in names}
        counts = tuple(
            (name, count)
            for name, count in self._saved_role_requirements(key).set_requirements
            if set_name(name) in allowed
        )
        value = RoleRequirements(
            self.cost_layout_picker.costs(),
            self.main_requirements_picker.main_names(),
            names,
            counts,
            self.fixed_items_picker.item_ids(),
        )
        from echo_workshop.core.compatibility import CompatibilityRule

        rule = self.runtime.weights.active.compatibility_rule(
            key[0], CompatibilityRule(self.element_rule.currentData(), names)
        )
        self._queue_rule_save(
            dict(
                key=key,
                value=value,
                rule=rule,
                catalog=self.item_picker.catalog,
                context=self._rule_save_context(),
            )
        )

    def _commit_rule_save(self, draft):
        key, value, rule = draft['key'], draft['value'], draft['rule']
        previous = self._role_requirements, self._cost_layouts, self._rules
        self._role_requirements = dict(self._role_requirements)
        self._cost_layouts = dict(self._cost_layouts)
        self._rules = dict(self._rules)
        self._role_requirements[key] = value
        self._cost_layouts[key] = value.costs
        self._rules[key] = rule
        try:
            # 全部校验、序列化完成后只写一次完整配置，失败时恢复内存。
            self.save_preferences()
        except Exception:
            self._role_requirements, self._cost_layouts, self._rules = previous
            raise
        self._rule_baseline = (key, self.element_rule.currentData(), self.sonata_rule.text())
        self._requirements_baseline = (value.costs, value.main_names, value.fixed_item_ids)
        self._requirements_context, self._draft_costs = key, value.costs
        self._requirements_form_state = (
            self.profile_box.currentData(),
            key,
            value.costs,
            value.main_names,
            value.fixed_item_ids,
            id(self.item_picker.catalog),
        )
        self._rule_dirty = False
        self._editor = None
        self._rule_save_error = ''
        self.changed(persist=False)

    def _refresh_resource_selectors(self):
        if not hasattr(self, 'managed_sonatas'):
            return
        version = self.runtime.sonata.active.version
        if version == getattr(self, '_resource_selector_versions', None):
            return
        self._resource_selector_versions = version
        self._sync_managed_sonatas()  # Catalogue only; preserve unsaved choices.
