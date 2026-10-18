# 提供独立多角色声骸匹配页面。
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget
from echo_workshop.ui.widgets.inputs import QComboBox, combo_search_draft, restore_combo_search
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    TableView,
    PushButton,
    PrimaryPushButton,
    isDarkTheme,
)
from echo_workshop.ui.matching.controller import ComparisonContent
from echo_workshop.ui.widgets.workspace import BriefStatus
from echo_workshop.ui.matching.settings import MatchingRoleSettingsController
from echo_workshop.application.matching.feedback import MatchFeedback


def put(layout, widget, stretch=0):
    layout.addWidget(widget, stretch)
    return widget


class ComparisonPage(MatchingRoleSettingsController, ComparisonContent, QWidget):
    _table_type = TableView
    _button_type = PushButton

    def __init__(self, task, parent=None):
        from echo_workshop.application.matching.calculation import MatchCalculationOptions

        self._rule_dirty = False
        self._saving_rule = self._autosave_scheduled = False
        self._rule_save_request = self._pending_rule_draft = self._active_rule_draft = None
        self._rule_save_timer = self._rule_save_job = None
        self._rule_save_epoch = 0
        self._rule_save_error = ''
        self._editor = None
        self._rule_baseline = None
        self._preserve_edit_widgets = False
        self._role_order = self._display_order = ()
        self._priority_links = {}
        self._calculation_options = MatchCalculationOptions()
        self._calculation_error = ''
        self._cost_layouts = {}
        self._role_requirements = {}
        self._cost_picker_state = None
        self._draft_costs = None
        self._requirements_baseline = self._requirements_context = self._requirements_form_state = (
            None
        )
        self._empty_feedback = None
        super().__init__(task, parent)
        self._sync_role_strip()
        self._refresh_controls()
        self.edit_controls.update_theme()

    def build_comparison_ui(self):
        from PySide6.QtWidgets import QStackedWidget
        from echo_workshop.ui.widgets.roles import IdentityCombo
        from echo_workshop.ui.widgets.role_layout import RoleSettingsSurface
        from echo_workshop.ui.widgets.state import ChoiceState, TextState
        from echo_workshop.ui.matching.state import MatchingState, CandidateScope, RuleText
        from echo_workshop.ui.matching.matrix_model import MatrixModel
        from echo_workshop.ui.matching.picker import MatchRoleStrip
        from echo_workshop.ui.matching.cards import MatchResultCards
        from echo_workshop.core.scoring import ELEMENT_NAMES
        from qfluentwidgets import qconfig

        self.setWindowTitle('匹配')
        self.setMinimumSize(0, 0)
        self.state = MatchingState()
        self.profile_box = ChoiceState(self)
        for key, label in self.runtime.inventory.profiles().items():
            self.profile_box.addItem(label, key)
        self.item_picker = CandidateScope(self)
        self.matrix_model = MatrixModel(self)
        self.matrix_model.compact_labels = True
        self.sonata_rule = RuleText(self)
        self.element_rule = QComboBox(self)
        self.element_rule.addItem('未知 / 未指定', '')
        for element in sorted(ELEMENT_NAMES):
            self.element_rule.addItem(element, element)
        self.status = BriefStatus('选择角色，然后点击开始计算。', self)
        self.match_summary = TextState()
        self.view_hint = TextState()
        self.library_scope_label = TextState()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        row = QHBoxLayout()
        self.participants_label = CaptionLabel('参与角色', self)
        row.addWidget(self.participants_label)
        self.participants = IdentityCombo(self)
        self.participants.setEditable(True)
        self.participants.setInsertPolicy(QComboBox.NoInsert)
        self.participants.setMinimumWidth(0)
        self.participants.lineEdit().setPlaceholderText('添加角色 / 模版（可添加多个，支持拼音）')
        self.participants.currentIndexChanged.connect(self.participant_changed)
        self.participants.activated.connect(
            lambda i: self.action(lambda: self.choose_participant(i))
        )
        row.addWidget(self.participants, 1)
        self.compare_button = PrimaryPushButton('开始计算', self)
        self.compare_button.clicked.connect(lambda: self.action(self.start_comparison))
        row.addWidget(self.compare_button)
        self.cancel_button = PushButton('取消计算', self)
        self.cancel_button.clicked.connect(self.cancel_comparison)
        row.addWidget(self.cancel_button)
        outer.addLayout(row)
        self.role_strip = MatchRoleStrip(self, priority_controls=False)
        self.role_strip.focused.connect(lambda key: self.action(lambda: self._focus_role(key)))
        self.role_strip.removed.connect(lambda key: self.action(lambda: self._remove_role(key)))
        self.role_strip.managed.connect(lambda key: self.action(lambda: self._manage_role(key)))
        self.role_strip.reordered.connect(
            lambda keys: self.action(lambda: self._reorder_roles(keys))
        )
        outer.addWidget(self.role_strip)
        self.scope_toolbar = QWidget(self)
        self.scope_actions_layout = QHBoxLayout(self.scope_toolbar)
        self.scope_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.library_select_button = PushButton('选择声骸', self)
        self.library_select_button.clicked.connect(
            lambda: self.action(lambda: self.task._panel.begin_inventory_selection(self))
        )
        self.result_cards = MatchResultCards(self)
        self.result_cards.avatar_cache = self.runtime.avatars
        self.result_cards.mount_selector(self.scope_actions_layout)
        self.scope_actions_layout.addStretch(1)
        row.insertWidget(row.indexOf(self.compare_button), self.library_select_button)
        self.result_cards.selected.connect(self._match_card_selected)
        self.result_cards.pending_review.connect(lambda: self.action(self._open_pending_review))
        outer.addWidget(self.scope_toolbar)
        self.filter_badge = CaptionLabel(self)
        outer.addWidget(self.filter_badge)
        outer.addWidget(self.status)
        self.page_splitter = QSplitter(Qt.Horizontal, self)
        self.results_body = QWidget(self)
        results = QVBoxLayout(self.results_body)
        results.setContentsMargins(0, 0, 0, 0)
        self.result_views = QStackedWidget(self)
        self.empty_view = CaptionLabel('先选择角色', self)
        self.empty_view.setAlignment(Qt.AlignCenter)
        self.empty_view.setWordWrap(True)
        self.result_views.addWidget(self.empty_view)
        self.result_views.addWidget(self.result_cards)
        results.addWidget(self.result_views, 1)
        self.page_splitter.addWidget(self.results_body)
        self.edit_controls = RoleSettingsSurface(self)
        self.managed_avatar = QLabel(self.edit_controls)
        self.managed_avatar.setFixedSize(36, 36)
        self.edit_controls.header_layout.addWidget(self.managed_avatar)
        heading = QWidget(self.edit_controls)
        heading_layout = QVBoxLayout(heading)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        self.managed_role_header = BodyLabel('角色设置', self)
        self.managed_role_header.setWordWrap(True)
        heading_layout.addWidget(self.managed_role_header)
        self.role_facts = CaptionLabel(self)
        heading_layout.addWidget(self.role_facts)
        self.edit_controls.header_layout.addWidget(heading, 1)
        self.managed_close_button = PushButton('返回匹配', self)
        self.managed_close_button.clicked.connect(
            lambda: self.edit_controls.toggle.setChecked(False)
        )
        self.edit_controls.header_layout.addWidget(self.managed_close_button)
        self.role_settings_status = CaptionLabel(self)
        self.role_settings_status.hide()
        self.role_settings_status.setWordWrap(True)
        self.edit_controls.footer_layout.addWidget(self.role_settings_status, 1)
        self.weight_options = PushButton('编辑模版权重', self)
        self.weight_options.clicked.connect(lambda: self.action(self._configure_managed_weights))
        self._build_managed_rules()
        self._build_calculation_controls()
        self.edit_controls.role_layout.addStretch(1)
        self.edit_controls.calculation_layout.addStretch(1)
        self.match_workspace = QStackedWidget(self)
        self.match_workspace.addWidget(self.page_splitter)
        self.match_workspace.addWidget(self.edit_controls)
        outer.addWidget(self.match_workspace, 1)
        self._projecting_cards = False
        self.element_rule.currentIndexChanged.connect(self._rule_edited)
        self.sonata_rule.textChanged.connect(self._rule_edited)
        self.edit_controls.toggle.toggled.connect(self._update_editor_layout)
        self.edit_controls.toggle.hide()
        qconfig.themeChanged.connect(self._sync_managed_header)
        qconfig.themeChangedFinished.connect(self._sync_managed_header)

    def selections(self):
        from echo_workshop.application.matching.calculation import ordered_keys

        return ordered_keys(super().selections(), self._display_order or self._role_order)

    def filter_options(self):
        return dict(super().filter_options(), **self._calculation_options.filters())

    def priority_groups(self):
        from echo_workshop.application.matching.calculation import (
            links_to_groups,
            restrict_priority_groups,
        )

        groups = links_to_groups(
            self._role_order,
            tuple(self._priority_links.get(key, '=') for key in self._role_order[1:]),
        )
        return restrict_priority_groups(groups, super().selections())

    def save_preferences(self):
        if self._calculation_error:
            raise ValueError(self._calculation_error)
        from echo_workshop.application.matching.calculation import normalize_candidate_ids
        from echo_workshop.core.costs import cost_layouts_to_rows
        from echo_workshop.core.requirements import requirements_to_rows

        data = self._preference_data()
        groups = self.priority_groups()
        keys = tuple(key for group in groups for key in group)
        starts = {group[0] for group in groups[1:]}
        data['profiles'][self.profile_box.currentData()]['calculation'] = dict(
            role_order=keys,
            links=['>' if key in starts else '=' for key in keys[1:]],
            display_order=self.selections(),
            options=self._calculation_options.to_dict(),
            cost_layouts=cost_layouts_to_rows(self._cost_layouts),
            role_requirements=requirements_to_rows(self._role_requirements),
            candidate_item_ids=normalize_candidate_ids(self.item_picker.selected_ids()),
        )
        import json

        self.task.config['ComparisonSettingsJSON'] = json.dumps(
            data, ensure_ascii=False, allow_nan=False
        )

    def load_profile(self, *_):
        from echo_workshop.application.matching.calculation import (
            MatchCalculationOptions,
            links_to_groups,
            parse_candidate_ids,
            stable_keys,
        )

        self._role_order, self._display_order, self._priority_links = (), (), {}
        self._cost_layouts = {}
        self._draft_costs = None
        self._cost_picker_state = None
        self._role_requirements = {}
        self._requirements_baseline = self._requirements_context = self._requirements_form_state = (
            None
        )
        self._saved_candidate_ids = None
        self._calculation_options = MatchCalculationOptions()
        self._calculation_error = ''
        try:
            saved = self.preferences()['profiles'].get(self.profile_box.currentData(), {})
            calculation = saved.get('calculation', {})
            if not isinstance(calculation, dict):
                raise ValueError('计算配置格式无效；原配置保留')
            self._saved_candidate_ids = parse_candidate_ids(calculation.get('candidate_item_ids'))
            keys = stable_keys(calculation.get('role_order', saved.get('selections', ())))
            links = calculation.get('links', ['='] * max(0, len(keys) - 1))
            links_to_groups(keys, links)
            self._role_order = keys
            self._display_order = stable_keys(calculation.get('display_order', keys))
            self._priority_links = dict(zip(keys[1:], links))
            self._calculation_options = MatchCalculationOptions.from_dict(
                calculation.get('options', {})
            )
            from echo_workshop.core.costs import cost_layouts_from_rows

            self._cost_layouts = cost_layouts_from_rows(calculation.get('cost_layouts', []))
            from echo_workshop.core.requirements import requirements_from_rows

            self._role_requirements = requirements_from_rows(
                calculation.get('role_requirements', [])
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            self._calculation_error = str(error)
        super().load_profile()
        # A restored scope is explicit input; it must not look like a fresh user edit.
        blocked = self.item_picker.blockSignals(True)
        self.item_picker.set_selection(self._saved_candidate_ids)
        self.item_picker.blockSignals(blocked)
        self._sync_inventory_scope_label()
        if hasattr(self, 'calculation_level'):
            self._sync_calculation_controls()
        self.status.setText(self._calculation_error or '选择角色，然后点击开始计算。')

    def _reorder_roles(self, keys):
        from echo_workshop.application.matching.calculation import stable_keys

        keys = stable_keys(keys)
        previous = self.selections()
        if self._rule_dirty:
            self._sync_role_strip()
            raise ValueError('请先修正当前角色要求的错误，再拖拽角色。')
        if len(keys) != len(previous) or set(keys) != set(previous):
            self._sync_role_strip()
            raise ValueError('参与角色已变化，请重新拖拽。')
        self._display_order = keys
        self.changed()

    def _sync_inventory_scope_label(self):
        ids = self.item_picker.selected_ids()
        text = '全部已确认声骸' if ids is None else f'已选 {len(ids)} 件声骸'
        self.library_select_button.setToolTip(text + '；在声骸库中选择或筛选。')

    def _apply_inventory_ids(self, ids):
        if self._rule_dirty:
            raise ValueError('请先修正角色要求的错误，再选择声骸。')
        self._reset_match_filters()
        self.item_picker.set_selection(ids)
        self._sync_inventory_scope_label()

    def _manage_role(self, key):
        self._focus_role(key)
        if self.participants.currentData() != key:
            raise ValueError('请先修正当前角色要求的错误，再管理其他角色。')
        self.edit_controls.toggle.setChecked(True)

    def _sync_role_strip(self, *_):
        if not hasattr(self, 'role_strip'):
            return
        entries = []
        for key in self.selections():
            try:
                label = self.runtime.weights.active.template(*key)['name']
            except (ValueError, KeyError):
                label = '失效选择：' + '/'.join(key)
            entries.append((key, label))
        avatars = getattr(self.runtime, 'avatars', None)
        avatar_revision = getattr(avatars, 'revision', None)
        strip_state = (
            tuple(entries),
            self.participants.currentData(),
            tuple(self._priority_links.items()),
            avatars,
            avatar_revision,
            isDarkTheme(),
        )
        if strip_state != getattr(self, '_strip_state', None) or avatar_revision is None:
            self._strip_state = strip_state
            self.role_strip.set_roles(
                entries, self.participants.currentData(), self._priority_links, avatars
            )
        for key, _ in entries:
            # Missing pictures keep the existing deferred/retry policy. Cached
            # pictures do not repeatedly rebuild the resource character index.
            if avatars is not None and not self.role_strip._avatar_sources.get(key):
                avatars.request(key[0])
        self._sync_managed_header()
        self._sync_inventory_scope_label()

    def _focus_role(self, key):
        if key not in self.selections():
            return
        self.participants.setCurrentIndex(self.participants.findData(key))
        self._sync_role_strip()  # A guarded dirty draft may reject a change.

    def _remove_role(self, key):
        if self._rule_dirty:
            raise ValueError('请先修正当前角色要求的错误，再移除角色。')
        self.state.roles = tuple(role for role in self.state.roles if role != key)
        if self.state.current_role == key:
            self.state.current_role = self.state.roles[0] if self.state.roles else None
        self.changed()

    def _reset_match_filters(self):
        self.state.filters = dict(
            text='', cost=None, mode='confirmed', fully_tuned=False, max_age_days=0
        )

    def _refresh_filter_badge(self):
        filters = self.state.filters
        labels = []
        if filters.get('cost') is not None:
            labels.append('COST ' + str(filters['cost']))
        if filters.get('text'):
            labels.append(str(filters['text']))
        self.filter_badge.setText('筛选：' + ' · '.join(labels) if labels else '')
        self.filter_badge.setVisible(bool(labels))

    def changed(self, *_, persist=True):
        super().changed(persist=persist)
        self._refresh_filter_badge()
        self._refresh_controls()

    def _refresh_controls(self):
        if not hasattr(self, 'page_splitter'):
            return
        busy = (
            self._future is not None or self._publication is not None
        ) and self._worker_kind != 'catalogue'
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        self.compare_button.setEnabled(
            not busy
            and bool(self.selections())
            and not self._rule_dirty
            and not self._calculation_error
        )
        self.compare_button.setText('正在计算…' if busy else '开始计算')
        full = getattr(self.status, 'full_text', self.status.text())
        normal = full.startswith(
            (
                '仅已收录：',
                '库存复核完成',
                '勾选角色 / 固定模态',
                '选择或条件已变',
                '选择已更新',
                '配置已更新',
                '候选已更新',
                '选择角色并设置优先级',
                '选择角色，然后',
                '已选 ',
            )
        )
        explained = self._empty_feedback is not None and full == self._empty_feedback.text
        self.status.setVisible(
            not self.edit_controls.toggle.isChecked() and not normal and not explained
        )
        self.match_summary.hide()
        self.view_hint.hide()
        self._sync_role_strip()
        self._update_editor_layout()

    def start_comparison(self, *, _allow_empty=False):
        if self._rule_dirty:
            raise ValueError('请先修正当前角色要求的错误，再开始计算。')
        if self._calculation_error:
            raise ValueError(self._calculation_error)
        super().start_comparison(_allow_empty=_allow_empty)
        self._refresh_controls()

    def cancel_comparison(self):
        super().cancel_comparison()
        self._refresh_controls()

    def _completed(self, generation, result, error):
        previous = self._preserve_edit_widgets
        self._preserve_edit_widgets = (
            hasattr(self, 'edit_controls') and self.edit_controls.toggle.isChecked()
        )
        try:
            super()._completed(generation, result, error)
        finally:
            self._preserve_edit_widgets = previous
        self._refresh_controls()

    def sync_participants(self):
        if not self._preserve_edit_widgets:
            # Async publication must not swallow unfinished pinyin/search text.
            box = self.participants
            draft = combo_search_draft(box)
            super().sync_participants()
            restore_combo_search(box, draft)
        self._sync_role_strip()

    def refresh_facts(self):
        super().refresh_facts()
        self._refresh_resource_selectors()
        self._sync_pending_count()
        self._refresh_controls()

    def _sync_pending_count(self):
        """Reuse the inventory reader's count; matching never reads the DB here."""
        inventory = getattr(self.task, 'inventory_window', None)
        status = getattr(getattr(inventory, '_reader', None), 'status', None)
        result = getattr(status, 'result', None)
        count = (
            result.pending_count
            if getattr(status, 'state', '') == 'ready'
            and result is not None
            and result.profile_id == self.profile_box.currentData()
            else None
        )
        self.result_cards.set_pending_count(count)

    def _open_pending_review(self):
        if self._rule_dirty:
            raise ValueError('请先修正当前角色要求的错误，再查看待核对。')
        if self._future is not None or self._publication is not None:
            raise ValueError('正在计算，请完成或取消后再查看待核对。')
        self.task._panel.show_intake_review()

    def apply_role_choice(self):
        if self._rule_dirty:
            raise ValueError('请先修正当前角色要求的错误，再更换参与角色。')
        super().apply_role_choice()
        if hasattr(self, 'edit_controls') and self.selections():
            self.edit_controls.toggle.setChecked(False)

    def remove_role_choice(self):
        if self._rule_dirty:
            raise ValueError('请先修正当前角色要求的错误，再移除角色。')
        super().remove_role_choice()

    def _match_card_selected(self, item_id, key):
        self.state.selected_identity = (item_id, *key) if item_id and key else None
        if self._result_key is not None and not self._projecting_cards:
            if self.result_cards.source.records:
                self._empty_feedback = None
            else:
                role = self.result_cards.current_key or self.state.current_role
                self._empty_feedback = MatchFeedback(
                    '当前声骸不满足角色要求，请检查套装、主词条和固定声骸。', 'role', role
                )
                self.result_cards.message.setText(self._empty_feedback.text)
            self._refresh_controls()
        self.show_details()

    def project_auto_result(self):
        if self._projecting_cards:
            return
        fresh = self._result_key is not None
        model = self.matrix_model
        selected = self.selections()
        self._empty_feedback = None
        if not selected or not fresh or not model.records or not model.selections:
            self._auto_view = 'empty'
            self.result_cards.clear()
            feedback = self._match_feedback
            if not selected:
                feedback = MatchFeedback('先选择角色', 'add-role')
            self._empty_feedback = feedback
            self.empty_view.setText(feedback.text if feedback else '点击开始计算')
            self.result_views.setCurrentWidget(self.empty_view)
            return
        self._projecting_cards = True
        try:
            self._auto_view = 'cards'
            self.result_cards.project(
                model,
                current_key=self.participants.currentData(),
                fresh=True,
                requirements=self.role_requirements(),
            )
            if not self.result_cards.source.records:
                key = self.state.current_role
                if key not in selected:
                    key = selected[0]
                self._empty_feedback = MatchFeedback(
                    '当前声骸不满足角色要求，请检查套装、主词条和固定声骸。', 'role', key
                )
                self.result_cards.message.setText(self._empty_feedback.text)
            self.result_views.setCurrentWidget(self.result_cards)
        finally:
            self._projecting_cards = False

    def show_details(self):
        if hasattr(self, 'match_workspace'):
            self._update_editor_layout()

    def _update_editor_layout(self, *_):
        if not hasattr(self, 'match_workspace'):
            return
        settings = self.edit_controls.toggle.isChecked()
        if settings:
            self._sync_managed_header()
        self.match_workspace.setCurrentWidget(
            self.edit_controls if settings else self.page_splitter
        )
        self.scope_toolbar.setVisible(not settings)
        self.filter_badge.setVisible(not settings and bool(self.filter_badge.text()))
        if settings:
            self.status.hide()
        self.role_settings_status.setText(self._rule_save_error)
        self.role_settings_status.setVisible(bool(self._rule_save_error))

    def apply_surface_palette(self, palette):
        self.setPalette(palette)
        self.managed_sonatas.setPalette(palette)
        self.managed_sonatas.viewport().setPalette(palette)
        self.managed_sonatas.setStyleSheet(
            'QListWidget { background: transparent; border: 1px solid palette(mid); border-radius: 5px; color: palette(text); }'
        )

    def resizeEvent(self, event):
        self._update_editor_layout()
        super().resizeEvent(event)
