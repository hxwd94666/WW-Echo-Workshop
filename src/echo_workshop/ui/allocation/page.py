# 提供独立五件配装流程和结果页面。
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    TableView,
    PushButton,
    PrimaryPushButton,
)
from echo_workshop.ui.allocation.controller import AllocationContent
from echo_workshop.ui.widgets.layout import open_profile_window
from echo_workshop.ui.allocation.notices import AllocationNotice
from echo_workshop.ui.allocation.grouped_results import GroupedLoadoutResults
from echo_workshop.ui.allocation.roles import AllocationRoleController
from echo_workshop.ui.allocation.actions import LoadoutActionController


def put(layout, widget, stretch=0):
    layout.addWidget(widget, stretch)
    return widget


class AllocationPage(LoadoutActionController, AllocationRoleController, AllocationContent, QWidget):
    _table_type = TableView
    _button_type = PushButton

    def __init__(self, task, parent=None):
        self._saved_view = False
        self._saved_plan_summaries = {}
        self._saved_summaries_profile = None
        self._viewing_history = False
        self._history_workspace_roles = None
        super().__init__(task, parent)
        self._reload_allocation_picker()
        self.render_roles()
        self.refresh()
        panel = getattr(task, '_panel', None)
        if panel is not None:
            self.apply_surface_palette(panel.palette())

    def build_allocation_ui(self):
        from types import SimpleNamespace
        from PySide6.QtWidgets import QStackedWidget, QComboBox as NativeCombo
        from echo_workshop.ui.allocation.workspace import LoadoutPriorityEditor, EditorHost
        from echo_workshop.ui.allocation.history import SavedLoadoutsWorkspace
        from echo_workshop.ui.widgets.roles import IdentityCombo
        from echo_workshop.ui.widgets.state import (
            ChoiceState,
            TableState,
            TextState,
            ToggleState,
            ActionState,
            RecordPreviewState,
        )

        self.setWindowTitle('配装')
        self.setMinimumSize(0, 0)
        self._precheck = self._pending_precheck = self._role_draft = None
        self._generate_after_check = False
        self._workspace_key = None
        self._working_role_order = ()
        self.profile = ChoiceState(self)
        for key, label in self.runtime.inventory.profiles().items():
            self.profile.addItem(label, key)
        self.steps = ChoiceState(self)
        self.steps.addItems(['', '装备要求', '方案结果'])
        self.roles_table = TableState(4, self)
        self.checklist = TableState(5, self)
        self.checklist.itemSelectionChanged.connect(self.details)
        self.incoming = ChoiceState(self)
        self.incoming_box = ActionState()
        self.pending_notice = TextState()
        self.current_choices = ChoiceState(self)
        self.current_choices.currentIndexChanged.connect(self.update_current_note)
        self.saved = ChoiceState(self)
        self.current_note = TextState()
        self.current_buttons = []
        self.load_button = ActionState()
        self.refresh_history_button = ActionState()
        self.precheck_hint = TextState()
        self.precheck_text = TextState()
        self.precheck_panel = SimpleNamespace(toggle=ToggleState(self))
        self.summary = TextState()
        self.detail_text = TextState()
        self.outcome = TextState()
        self.image = RecordPreviewState()
        self.evidence_button = ActionState('查看原图')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self.status = AllocationNotice('', self)
        self.scope_notice = TextState('候选范围：' + self._scope_label)
        self.role_add_bar = QWidget(self)
        add = QHBoxLayout(self.role_add_bar)
        add.setContentsMargins(0, 0, 0, 0)
        add.setSpacing(8)
        add.addWidget(CaptionLabel('参与角色', self))
        self.allocation_role_picker = IdentityCombo(self)
        self.allocation_role_picker.setEditable(True)
        self.allocation_role_picker.setMinimumWidth(0)
        self.allocation_role_picker.setInsertPolicy(NativeCombo.NoInsert)
        self.allocation_role_picker.lineEdit().setPlaceholderText(
            '添加角色 / 模版（可添加多个，支持拼音）'
        )
        self.allocation_role_picker.activated.connect(
            lambda i: self.action(lambda: self._choose_allocation_role(i))
        )
        self.solve_button = PrimaryPushButton('直接配装', self)
        self.solve_button.clicked.connect(lambda: self.action(self.generate_from_precheck))
        self.cancel_button = PushButton('取消配装', self)
        self.cancel_button.clicked.connect(self.cancel_job)
        add.addWidget(self.allocation_role_picker, 1)
        add.addWidget(self.solve_button)
        add.addWidget(self.cancel_button)
        outer.addWidget(self.role_add_bar)
        self.role_navigation = LoadoutPriorityEditor(self)
        self.priority_editor = self.role_navigation
        self.role_navigation.selected.connect(
            lambda key: self.action(lambda: self._workspace_role_clicked(key))
        )
        self.role_navigation.managed.connect(
            lambda key: self.action(lambda: self._edit_allocation_role(key))
        )
        self.role_navigation.removed.connect(
            lambda key: self.action(lambda: self._remove_allocation_role(key))
        )
        self.role_navigation.priority_changed.connect(
            lambda key, value: self.action(lambda: self._edit_allocation_priority(key, value))
        )
        self.role_navigation.reordered.connect(
            lambda keys: self.action(lambda: self._reorder_allocation_roles(keys))
        )
        outer.addWidget(self.role_navigation)
        self.scope_toolbar = QWidget(self)
        self.flow_header = QHBoxLayout(self.scope_toolbar)
        self.flow_header.setContentsMargins(0, 0, 0, 0)
        self.flow_header.setSpacing(8)
        self.result_cards = GroupedLoadoutResults(
            self, avatar_cache=self.runtime.avatars, panel_service=self.runtime.panel
        )
        self.result_cards.role_selected.connect(self._result_role_changed)
        self.result_cards.selected.connect(self._select_visible_result_card)
        self.result_cards.changes_requested.connect(
            lambda key: self.action(lambda: self.show_loadout_changes(key))
        )
        self.result_cards.replace_requested.connect(
            lambda key, identity: self.action(lambda: self.replace_result(key, identity))
        )
        self.result_cards.backup_requested.connect(
            lambda key: self.action(lambda: self.backup_loadout(key))
        )
        self.result_cards.save_requested.connect(
            lambda key: self.action(lambda: self.save_loadout(key))
        )
        self.flow_header.addStretch(1)
        self.whole_inventory_button = PushButton('选择声骸', self)
        self.whole_inventory_button.clicked.connect(
            lambda: self.action(lambda: self.task._panel.begin_inventory_selection(self))
        )
        add.insertWidget(add.indexOf(self.solve_button), self.whole_inventory_button)
        self.history_button = PushButton('已保存配装', self)
        self.history_button.clicked.connect(self._show_history)
        add.insertWidget(add.indexOf(self.solve_button), self.history_button)
        self.scope_toolbar.hide()
        outer.addWidget(self.status)
        self.confirmation_box = QWidget(self)
        confirmation = QVBoxLayout(self.confirmation_box)
        self.confirmation_text = CaptionLabel(self.confirmation_box)
        self.confirmation_text.setWordWrap(True)
        confirmation.addWidget(self.confirmation_text)
        row = QHBoxLayout()
        self.confirm_current_button = PushButton('确认', self)
        self.confirm_current_button.clicked.connect(
            lambda: self.action(self.confirm_current_action)
        )
        self.cancel_current_button = PushButton('取消', self)
        self.cancel_current_button.clicked.connect(self.cancel_current_action)
        row.addWidget(self.confirm_current_button)
        row.addWidget(self.cancel_current_button)
        row.addStretch(1)
        confirmation.addLayout(row)
        outer.addWidget(self.confirmation_box)
        self.confirmation_box.hide()
        self.pages = QStackedWidget(self)
        for _ in range(2):
            body = QWidget(self.pages)
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            self.pages.addWidget(body)
        empty = self.pages.widget(0).layout()
        self.requirements_prompt = CaptionLabel('选择角色后，点击齿轮设置装备要求。', self)
        self.requirements_prompt.setAlignment(Qt.AlignCenter)
        self.requirements_prompt.setWordWrap(True)
        empty.addWidget(self.requirements_prompt, 1)
        results = self.pages.widget(1).layout()
        self.empty_result_note = CaptionLabel('正在准备配装…', self)
        self.empty_result_note.setAlignment(Qt.AlignCenter)
        results.addWidget(self.empty_result_note)
        results.addWidget(self.result_cards, 1)
        self.review_button = PushButton('核对0件', self)
        self.review_button.clicked.connect(lambda: self.action(lambda: self.open_remedy('review')))
        self.outcome_label = CaptionLabel('', self)
        self.outcome_label.setWordWrap(False)
        outer.addWidget(self.pages, 1)
        self.editor_area = EditorHost(self)
        self.editor_area.hide()
        outer.addWidget(self.editor_area, 1)
        self.result_toolbar = QWidget(self)
        footer = QHBoxLayout(self.result_toolbar)
        footer.setContentsMargins(0, 0, 0, 0)
        self.tuned = ToggleState(self, checked=True)
        self.tuned.toggled.connect(self.changed)
        self.check_button = ActionState()
        self.save_actions = QWidget(self)
        save = QVBoxLayout(self.save_actions)
        save.setContentsMargins(0, 0, 0, 0)
        save.setSpacing(6)
        self.save_guide = TextState()
        self.result_actions = QHBoxLayout()
        self.result_actions.setContentsMargins(0, 0, 0, 0)
        self.mark_button = ActionState()
        from echo_workshop.ui.allocation.action_labels import explain, APPLY_TIP, BACKUP_TIP

        self.save_button = PushButton('备份全部方案', self)
        explain(self.save_button, BACKUP_TIP)
        self.save_button.clicked.connect(lambda: self.action(self.backup_loadout))
        self.protect_button = PrimaryPushButton('全部设为配装', self)
        explain(self.protect_button, APPLY_TIP)
        self.protect_button.clicked.connect(lambda: self.action(self.save_loadout))
        self._changes_dialog = None
        self.result_actions.addWidget(self.review_button)
        self.result_actions.addWidget(self.outcome_label)
        self.result_actions.addStretch(1)
        self.result_actions.addWidget(self.save_button)
        self.result_actions.addWidget(self.protect_button)
        save.addLayout(self.result_actions)
        footer.addWidget(self.save_actions)
        # Batch result actions belong above the result, never at the page foot.
        outer.insertWidget(outer.indexOf(self.pages), self.result_toolbar)
        self.saved_workspace = SavedLoadoutsWorkspace(self, self)
        self.saved_workspace.hide()
        outer.addWidget(self.saved_workspace, 1)
        self.steps.currentIndexChanged.connect(
            lambda i: self.pages.setCurrentIndex(1 if i == 2 else 0)
        )
        self.steps.currentIndexChanged.connect(lambda _: self._sync_step_controls())
        self.steps.setCurrentIndex(1)

    def _select_visible_result_card(self, item_id):
        if not self._saved_view:
            self.select_result_card(item_id)

    def select_result_card(self, item_id):
        if not item_id:
            self.checklist.clearSelection()
            self.checklist.setCurrentCell(-1, -1)
            self.details()
            return
        for row in range(self.checklist.rowCount()):
            item = self.checklist.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == item_id:
                self.checklist.setCurrentCell(row, 0)
                return

    def render_result(self, result, completed):
        super().render_result(result, completed)
        if hasattr(self, 'result_cards'):
            self.result_cards.set_result(
                result, self._records, completed, snapshot=self.rating_snapshot()
            )
            self.result_cards.set_protection(self._current_loadouts)
            self._sync_result_actions()

    def _sync_result_actions(self):
        self.save_button.setText('备份全部方案')
        self.protect_button.setText('全部设为配装')
        self.refresh_loadout_actions()
        self.result_cards.set_changes_enabled(
            (change.key for change in getattr(self, '_loadout_changes', ())),
            enabled=not self._loadout_busy(),
        )

    def show_loadout_changes(self, key):
        from echo_workshop.ui.allocation.changes import LoadoutChangesDialog

        if self._loadout_busy() or self._saved_view:
            raise ValueError('请先返回当前配装结果并结束作业。')
        if not isinstance(key, tuple) or len(key) != 2 or key not in self.result_cards.groups:
            raise ValueError('该角色已不在当前配装结果中，请重新选择。')
        changes = tuple(
            change for change in getattr(self, '_loadout_changes', ()) if change.key == key
        )
        if len(changes) != 1:
            raise ValueError('该角色暂无本次变动，请先直接配装。')
        if self._changes_dialog is not None:
            self._changes_dialog.close()
            self._changes_dialog.deleteLater()
        self._changes_dialog = LoadoutChangesDialog(
            changes, self.window(), avatar_cache=self.runtime.avatars
        )
        self._changes_dialog.setWindowTitle(changes[0].label + ' · 配装变动')
        self._changes_dialog.open()

    def inventory_selection_error(self):
        return self._priority_edit_error()

    def inventory_selection_context(self):
        return (
            self.profile.currentData(),
            tuple(self.roles),
            self._priority_groups,
            tuple(self.incoming.itemData(i) for i in range(self.incoming.count())),
            self._scope_generation,
        )

    def check_inventory_selection_context(self, context):
        reason = self.inventory_selection_error()
        if reason:
            raise ValueError(reason)
        if context != self.inventory_selection_context():
            raise ValueError('配装角色、要求或候选范围已变化，请取消后重新选择声骸。')

    def apply_inventory_selection(self, ids):
        reason = self.inventory_selection_error()
        if reason:
            raise ValueError(reason)
        from echo_workshop.application.matching.inventory import _ids

        ids = _ids(ids)
        # Frozen user-selected IDs replace the old matching presentation query.
        # Explicit empty stays empty; pins/constraints/protection still apply.
        self.set_candidate_scope(
            ids,
            filters=None,
            label=f'已选择 {len(ids)} 件声骸',
            edit_plans=self._edit_plans,
            replace_plans=self._replace_plans,
        )
        self._invalidate_generated_view()
        self.changed()
        self.steps.setCurrentIndex(1)
        self.status.setText('声骸选择已更新，请重新生成方案。')

    def _invalidate_generated_view(self):
        self._result = self._payload = self._snapshot = None
        self._records = ()
        self._result_reservations = None
        self._loadout_changes = ()
        self._precheck = None
        self.precheck_text.clear()
        self.checklist.setRowCount(0)
        self.summary.clear()
        self.outcome.clear()
        if hasattr(self, 'outcome_label'):
            self.outcome_label.clear()
        self.steps.setCurrentIndex(1)

    def _result_role_changed(self, key):
        self.role_navigation.select(key, emit=False)
        self._focus_workspace_role(key)

    def apply_surface_palette(self, palette):
        self.setPalette(palette)
        for index in range(self.pages.count()):
            self.pages.widget(index).setPalette(palette)

    def _requirements_confirmed(self):
        if not self.roles or self.incoming.count() or self._editor is not None:
            return False
        try:
            self.request()
        except (ValueError, KeyError, TypeError):
            return False
        return True

    def _sync_step_controls(self):
        result = self._result is not None or self._payload is not None
        self.save_actions.setVisible(result)
        self.result_toolbar.setVisible(result and not self._saved_view and self._editor is None)
        self.empty_result_note.setText(
            '正在准备配装…'
            if self._generate_after_check or self._future is not None or self._io_future is not None
            else '尚未生成配装，请调整装备要求或选择声骸。'
        )
        if self.incoming.count():
            self.requirements_prompt.setText('点击角色旁的齿轮，设置装备要求后直接配装。')
        elif self.roles:
            self.requirements_prompt.setText('点击“直接配装”查看方案。')
        else:
            self.requirements_prompt.setText('先选择参与角色。')

    def _show_history(self):
        if self._editor is not None:
            self.status.setText('请先保存或取消当前装备要求编辑。')
            return
        self._saved_view = True
        self.saved_workspace.confirmation_layout.addWidget(self.confirmation_box)
        self.saved_workspace.project(force=True)
        self._apply_saved_visibility()
        self.action(self.refresh_saved)

    def _hide_history(self):
        self.cancel_current_action()
        if self._viewing_history:
            self._restore_working_requirements()
            self.steps.setCurrentIndex(1)
        self._saved_view = False
        if hasattr(self, 'saved_workspace'):
            self.layout().insertWidget(self.layout().indexOf(self.pages), self.confirmation_box)
            self._apply_saved_visibility()
            self.refresh()
            self.select_result_card(self.result_cards.selected_id)

    def _apply_saved_visibility(self):
        if not hasattr(self, 'saved_workspace'):
            return
        saved = self._saved_view
        editing = self._editor is not None
        self.role_navigation.setVisible(
            not saved and bool(self.role_navigation.buttons) and not editing
        )
        for widget in (self.role_add_bar, self.pages):
            widget.setVisible(not saved and not editing)
        self.status.setVisible(not saved and not editing and self.status.has_notice())
        self.saved_workspace.setVisible(saved)
        self._sync_step_controls()

    def queue_current_action(self, kind, arguments, message):
        super().queue_current_action(kind, arguments, message)
        labels = {
            'release-current': '确认解除保护',
            'remove-current': '确认移出',
            'remove-backup': '确认移除备份',
            'protect': '确认更新配装',
            'save-protect': '确认更新配装',
        }
        self.confirm_current_button.setText(labels.get(kind, '确认'))

    def begin_current_adjustment(self, value, arguments):
        self._viewing_history = False
        self._history_workspace_roles = None
        super().begin_current_adjustment(value, arguments)
        self._hide_history()

    def publish_current_loadouts(self, snapshot):
        super().publish_current_loadouts(snapshot)
        if hasattr(self, 'result_cards'):
            self.result_cards.set_protection(snapshot)
        if hasattr(self, 'saved_workspace'):
            self.saved_workspace.project()

    def _restore_working_requirements(self):
        if not self._viewing_history:
            return
        working = self._history_workspace_roles
        roles, groups, tuned = working['roles'], working['groups'], working['tuned']
        self._viewing_history = False
        self._history_workspace_roles = None
        self._loading = True
        self.roles = list(roles)
        self._priority_groups = groups
        self._priority_error = working['priority_error']
        self._subset_adjustment = working['subset']
        self._removed_role_ids = set(working['removed'])
        self._role_draft = working['draft']
        self._role_cost_layouts = dict(working['costs'])
        self._role_requirements_cache = dict(working['requirements'])
        self._role_requirement_conflicts = dict(working['conflicts'])
        self.incoming.clear()
        for label, key in working['incoming']:
            self.incoming.addItem(label, key)
        self.incoming_box.setVisible(bool(working['incoming']))
        self.tuned.setChecked(tuned)
        self._loading = False
        self._workspace_key = roles[0].key if roles else None
        self._invalidate_generated_view()
        self.render_roles()
        self.refresh()

    def changed(self, *_):
        self._pending_role_editor = None
        self._generate_after_check = False
        self._pending_precheck = None
        super().changed()

    def load_profile(self, *_):
        self._pending_role_editor = None
        self._generate_after_check = False
        self._pending_precheck = None
        self._viewing_history = False
        self._history_workspace_roles = None
        super().load_profile()
        self._workspace_key = self.roles[0].key if self.roles else None
        self._sync_workspace_roles()

    def generate_from_precheck(self):
        if self.incoming.count():
            raise ValueError('请先点击角色旁的齿轮设置装备要求。')
        self.steps.setCurrentIndex(2)
        if not self.precheck_fresh():
            # Publish may be delivered during start_precheck's UI refresh.
            self._generate_after_check = True
            try:
                self.start_precheck()
            except BaseException:
                self._generate_after_check = False
                raise
            self.precheck_panel.toggle.setChecked(False)
            return
        self._generate_after_check = False
        if self._precheck[4]['missing_pins']:
            raise ValueError('固定件缺失；先修改固定选择，再生成。')
        self.start()

    def publish_precheck(self, value):
        accepted = super().publish_precheck(value)
        if accepted is None:
            return None  # Busy is not stale; retry preserves explicit intent.
        requested = self._generate_after_check
        self._generate_after_check = False
        if accepted and requested:
            report = self._precheck[4]
            if report['missing_pins'] or any(
                not pool for row in report['roles'] for pool in row['slot_ids']
            ):
                self.precheck_hint.setText('候选有缺口，请修改要求或继续收录。')
                self.status.setText('候选不足，未开始生成；请编辑要求、核对声骸或继续收录。')
                self.refresh()
                return True
            # The receipt was just validated. Search still acquires its own
            # current snapshot and validates result revisions before publish.
            self.action(self.start)
        return accepted

    def cancel_job(self):
        self._pending_role_editor = None
        self._generate_after_check = False
        self._pending_precheck = None
        super().cancel_job()
        self.refresh()

    def _plan_completed(self, generation, output, error):
        if (
            generation == self._io_generation
            and not error
            and output[0] is not None
            and output[0].kind == 'load'
            and not self._io_arguments.get('adjust_character')
            and not self._io_arguments.get('saved_role_view')
            and not self._io_cancel.is_set()
            and self._io_profile == self.profile.currentData()
            and not self._viewing_history
            and not self._closing
            and not self.runtime._closed
            and not self.task._destroyed
        ):
            self._history_workspace_roles = dict(
                roles=tuple(self.roles),
                groups=self._priority_groups,
                tuned=self.tuned.isChecked(),
                priority_error=self._priority_error,
                subset=self._subset_adjustment,
                removed=frozenset(self._removed_role_ids),
                draft=self._role_draft,
                costs=dict(self._role_cost_layouts),
                requirements=dict(self._role_requirements_cache),
                conflicts=dict(self._role_requirement_conflicts),
                incoming=tuple(
                    (self.incoming.itemText(i), self.incoming.itemData(i))
                    for i in range(self.incoming.count())
                ),
            )
            self._viewing_history = True
        if generation == self._io_generation and (
            error or self._io_cancel.is_set() or self._io_profile != self.profile.currentData()
        ):
            self._generate_after_check = False
        if (
            generation == self._io_generation
            and not error
            and output[0] is not None
            and output[0].kind == 'list'
            and not self._io_cancel.is_set()
            and self._io_profile == self.profile.currentData()
            and not self._closing
            and not self.runtime._closed
            and not self.task._destroyed
        ):
            self._saved_plan_summaries = {row['plan_id']: row for row in output[0].summaries}
            self._saved_summaries_profile = self._io_profile
        super()._plan_completed(generation, output, error)
        if (
            generation == self._io_generation
            and not error
            and output[0] is not None
            and output[0].kind == 'list'
            and not output[0].errors
            and self.status.text().startswith('历史列表已校验')
        ):
            self.status.clear()
        if hasattr(self, 'saved_workspace'):
            self.saved_workspace.project()

    def refresh(self):
        with self.refresh_projection():
            self._refresh_page()

    def _refresh_page(self):
        super().refresh()
        if not hasattr(self, 'role_navigation'):
            return
        if getattr(self, '_workspace_avatar_revision', None) != self.runtime.avatars.revision:
            self._sync_workspace_roles()
            self.result_cards.avatar_cache = self.runtime.avatars
        if self._result is None and self._payload is None:
            self.result_cards.set_result(None)
            self._loadout_changes = ()
        busy = (
            self._future is not None
            or self._io_future is not None
            or getattr(self, '_pending_precheck', None) is not None
            or getattr(self, '_pending_role_editor', None) is not None
        )
        editing = self._editor is not None
        self.whole_inventory_button.setEnabled(not busy and not editing)
        self.allocation_role_picker.setEnabled(not busy and not editing)
        if getattr(self, '_allocation_picker_version', None) != self.runtime.weights.active.version:
            self._reload_allocation_picker()
        self.history_button.setEnabled(not busy and not editing)
        self.role_navigation.setEnabled(not editing)
        self.role_navigation.set_editable(not bool(self._priority_edit_error()))
        history_view = getattr(self, '_viewing_history', False)
        self.tuned.setEnabled(not busy and not editing and not history_view)
        if editing:
            self.profile.setEnabled(False)
        self.solve_button.setText('直接配装')
        self.solve_button.setEnabled(
            not busy and not editing and not history_view and self._requirements_confirmed()
        )
        self.cancel_button.setVisible(busy)
        self.cancel_button.setText('取消配装')
        self.result_cards.setVisible(self._result is not None or self._payload is not None)
        self.empty_result_note.setVisible(self._result is None and self._payload is None)
        self._sync_step_controls()
        self._sync_result_actions()
        self._focus_workspace_role(self._workspace_key)
        report = (
            self._precheck[4]
            if self._precheck and (self.precheck_fresh() or self._main_loadout_fresh())
            else None
        )
        pending_count = report['pending'] if report else 0
        self.review_button.setText(f'核对{pending_count}件')
        self.review_button.setVisible(bool(pending_count))
        if hasattr(self, 'saved_workspace'):
            self.saved_workspace.project()
            self._apply_saved_visibility()

    def open_remedy(self, kind):
        window = open_profile_window(
            self.task, 'inventory', self.parentWidget(), self.profile.currentData()
        )
        if kind == 'review':
            if hasattr(window, 'show_pending_review'):
                window.show_pending_review()
            else:
                window.filter_box.setCurrentIndex(1)
        else:
            window.compact_status.setText('打开游戏中的声骸详情，再开始收录。')
