# 管理配装要求生成和保存作业。
"""Explicit constraints -> background solve -> saved retrieval/move checklist."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import QTableWidgetItem

from echo_workshop.core.allocation.models import AllocationRequest, RoleRequest
from echo_workshop.core.loadout import protected_item_ids
from echo_workshop.application.allocation_preferences import allocation_settings, pending_role_keys
from echo_workshop.ui.matching.controller import _StopToken
from echo_workshop.ui.allocation.results import AllocationUX
from echo_workshop.ui.allocation.projection import AllocationProjection
from echo_workshop.application.matching.scope import select_match_records
from echo_workshop.ui.widgets.layout import stat_text
from qfluentwidgets import isDarkTheme, qconfig
from echo_workshop.ui.widgets.rating_style import rating_colour
from echo_workshop.ui.allocation.plans import SavedPlanController

_RATING_ITEM_ROLE = int(Qt.UserRole) + 20


def names(text):
    return tuple(value.strip() for value in text.replace('，', ',').split(',') if value.strip())


class _Signals(QObject):
    completed = Signal(int, object, str)
    plan_completed = Signal(int, object, str)
    plan_progress = Signal(int, str, int, int)


class _AllocationPayload(tuple):
    def __new__(cls, result, records, weights, reservations, scope_generation):
        value = super().__new__(cls, (result, records, weights))
        value.reservations = reservations
        value.scope_generation = scope_generation
        return value


class AllocationContent(SavedPlanController, AllocationProjection, AllocationUX):
    def lifecycle_busy(self):
        return bool(
            self._future is not None
            or self._io_future is not None
            or self._pending_current_action is not None
            or getattr(self, '_pending_precheck', None) is not None
            or getattr(self, '_pending_role_editor', None) is not None
        )

    def lifecycle_draft(self):
        return self._editor is not None or (self._role_draft is not None and self.incoming.count())

    def lifecycle_rule_dirty(self):
        return getattr(self, '_rule_dirty', False)

    def request_stop(self):
        self.stop_worker()

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task, self.runtime = task, task.runtime
        self.roles = []
        self._result = self._payload = self._snapshot = None
        self._loadout_changes = ()
        self._records = ()
        self._candidate_ids = None
        self._candidate_filters = None
        self._scope_label = '整个当前档案声骸库'
        self._subset_adjustment = False
        self._scope_generation = 0
        self._result_scope_generation = 0
        self._result_reservations = None
        self._edit_plans = {}
        self._replace_plans = {}
        self._priority_groups = ()
        self._priority_error = ''
        self._role_cost_layouts = {}
        self._role_requirements_cache = {}
        self._role_requirement_conflicts = {}
        self._removed_role_ids = set()
        self._summary_rating_lines = ()
        self._detail_rating_lines = ()
        self._current_loadouts = None
        self._pending_current_action = None
        self._io_arguments = {}
        self._pool = self._future = None
        self._cancel = threading.Event()
        self._generation = 0
        self._closing = self._loading = False
        self._worker_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._io_future = None
        self._io_generation = 0
        self._io_cancel = threading.Event()
        self._io_kind = ''
        self._io_profile = None
        self._history_queued = False
        self._history_needs_refresh = True
        self._edit_buttons = []
        self._editor = None
        self._signals = _Signals(self)
        self._signals.completed.connect(self._completed, Qt.QueuedConnection)
        self._signals.plan_completed.connect(self._plan_completed, Qt.QueuedConnection)
        self._signals.plan_progress.connect(self._plan_progress, Qt.QueuedConnection)
        self.build_allocation_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.profile.setCurrentIndex(self.profile.findData(task.config.get('InventoryProfileID')))
        self.profile.currentIndexChanged.connect(self.load_profile)
        self.load_profile()
        qconfig.themeChangedFinished.connect(self._update_rating_colours)

    def action(self, fn):
        if self.runtime._closed or self.task._destroyed:
            return
        try:
            fn()
        except (ValueError, KeyError, OSError, TypeError) as error:
            self.status.setText(str(error))
            if getattr(self, '_saved_view', False):
                self.saved_workspace.show_error(str(error))

    def preferences(self):
        return allocation_settings(self.task.config)

    def seed_role_requirements(self, editor, key):
        value = self._role_requirements_cache.get(key)
        costs = value.costs if value is not None else self._role_cost_layouts.get(key)
        if costs is not None:
            editor.apply_cost_layout(costs)
        if value is not None:
            for index, main in enumerate(value.main_names):
                editor.slots.cellWidget(index, 1).setText(', '.join(main))
            editor.sonatas.setText(', '.join(value.allowed_sonatas))
            editor.requirements.setText(
                '; '.join(f'{name}={count}' for name, count in value.set_requirements)
            )
            issues = editor._cost_layout_conflicts()
            editor.error.setText(
                '请调整固定件后保存：' + '；'.join(issues)
                if issues
                else '主词条与套装要求已带入；确认固定件和目标后保存。'
            )

    def remember_saved_cost_layout(self, role):
        """A detailed save belongs only to this allocation workspace."""
        from echo_workshop.core.requirements import RoleRequirements

        value = RoleRequirements.from_role(role)
        self._role_cost_layouts[role.key] = value.costs
        self._role_requirements_cache[role.key] = value
        self._role_requirement_conflicts.pop(role.key, None)

    def configure_pending_role(self):
        if self._role_draft is None or self.profile.currentData() != self._role_draft[0]:
            raise ValueError('导入选择属于原档案，请切回该档案再配置。')
        key = self.incoming.currentData()
        if key is None:
            raise ValueError('请选择待配置角色。')
        self.runtime.weights.active.template(*key)
        existing = next(
            (index for index, role in enumerate(self.roles) if role.character_id == key[0]), None
        )
        if existing is not None:
            if self.roles[existing].key != key:
                raise ValueError('此角色已有另一模版；先编辑或移除原配置，不自动替换。')
            self.roles_table.setCurrentCell(existing, 0)
            original = self.roles[existing]
            self._start_plan_job(
                'edit-role',
                dict(
                    profile_id=self.profile.currentData(),
                    snapshot=self.runtime.weights.active,
                    roles=tuple(self.roles),
                    editing=True,
                    index=existing,
                    role=original,
                    seed_rule=self._role_draft[2].get(key),
                ),
            )
            return
        if self._editor is not None:
            raise ValueError('请先结束当前角色编辑。')
        self._start_plan_job(
            'edit-role',
            dict(
                profile_id=self.profile.currentData(),
                snapshot=self.runtime.weights.active,
                roles=tuple(self.roles),
                editing=False,
                index=-1,
                role=None,
                seed_key=key,
                seed_rule=self._role_draft[2].get(key),
            ),
        )

    def clear_role_draft(self):
        self._role_draft = None
        self.incoming.clear()
        self.incoming_box.hide()
        self.update_role_summaries()

    def set_candidate_scope(
        self, item_ids, *, filters=None, label='', edit_plans=None, replace_plans=None
    ):
        """An empty explicit scope remains empty, including during adjustment."""
        self._candidate_ids = None if item_ids is None else frozenset(item_ids)
        self._candidate_filters = None if filters is None else dict(filters)
        self._scope_label = label or (
            '整个当前档案声骸库'
            if filters is None and item_ids is None
            else '当前筛选结果'
            if item_ids is None
            else f'勾选的 {len(self._candidate_ids)} 件声骸'
        )
        self._edit_plans, self._replace_plans = dict(edit_plans or {}), dict(replace_plans or {})
        self._scope_generation += 1
        self._cancel.set()
        self._io_cancel.set()
        self._precheck = None
        self.cancel_current_action()
        if hasattr(self, 'scope_notice'):
            self.scope_notice.setText('候选范围：' + self._scope_label)

    def changed(self, *_):
        if self._loading or self._closing:
            return
        if self._priority_error:
            self.status.setText(self._priority_error)
            return  # Keep malformed saved preferences until an explicit handoff/load.
        if getattr(self, '_viewing_history', False):
            # A validated frozen plan is read-only; working settings stay untouched.
            self.status.setText('正在查看历史方案；请先返回当前配装再修改设置。')
            return
        from echo_workshop.application.matching.calculation import normalize_candidate_ids

        self._cancel.set()
        self._io_cancel.set()
        self.cancel_current_action()
        self.render_roles()
        try:
            data = self.preferences()
            rows = [role.to_dict() for role in self.roles]
            saved_profile = data['profiles'].get(self.profile.currentData(), {})
            if self._subset_adjustment:
                active = {role.character_id for role in self.roles}
                old = saved_profile.get('roles', [])
                rows += [
                    row
                    for row in old
                    if row.get('character_id') not in active
                    and row.get('character_id') not in self._removed_role_ids
                ]
            data['profiles'][self.profile.currentData()] = dict(
                roles=rows,
                fully_tuned=self.tuned.isChecked(),
                pending_roles=pending_role_keys(
                    tuple(self.incoming.itemData(i) for i in range(self.incoming.count())), rows
                ),
                candidate_item_ids=normalize_candidate_ids(self._candidate_ids),
            )
            if 'search_budget' in saved_profile:
                data['profiles'][self.profile.currentData()]['search_budget'] = dict(
                    saved_profile['search_budget']
                )
            groups = self._priority_groups
            if (
                self._subset_adjustment
                and (self._edit_plans or self._replace_plans)
                and saved_profile.get('priority_groups')
            ):
                # Editing a historical subset must not flatten the untouched
                # working participants' priority groups when they are retained.
                keys_by_character = {
                    row['character_id']: (row['character_id'], row['modal_id']) for row in rows
                }
                groups = tuple(
                    tuple(keys_by_character[key[0]] for key in group if key[0] in keys_by_character)
                    for group in saved_profile['priority_groups']
                )
            if groups:
                from echo_workshop.application.matching.calculation import restrict_priority_groups

                data['profiles'][self.profile.currentData()]['priority_groups'] = (
                    restrict_priority_groups(
                        groups, tuple((row['character_id'], row['modal_id']) for row in rows)
                    )
                )
            self.task.config['AllocationSettingsJSON'] = json.dumps(
                data, ensure_ascii=False, allow_nan=False
            )
            self.status.setText('约束已保存；请生成新方案，已有方案不会被覆盖')
        except (ValueError, KeyError, TypeError) as error:
            self.status.setText(str(error))
        self.refresh()

    def load_profile(self, *_):
        self._cancel.set()
        self._io_cancel.set()
        self._history_needs_refresh = True
        self._loading = True
        self.roles = []
        self.incoming.clear()
        self._role_draft = None
        self._role_cost_layouts = {}
        self._role_requirements_cache = {}
        self._role_requirement_conflicts = {}
        self._subset_adjustment = False
        self._removed_role_ids = set()
        self._priority_groups = ()
        self._priority_error = ''
        self._result = self._payload = None
        self._loadout_changes = ()
        self._records = ()
        self.set_candidate_scope(None)
        self._result_reservations = None
        self._current_loadouts = None
        self.current_choices.clear()
        self.checklist.setRowCount(0)
        self.summary.clear()
        self.outcome.clear()
        if hasattr(self, 'outcome_label'):
            self.outcome_label.clear()
        self._precheck = None
        self.precheck_text.clear()
        self.precheck_hint.setText('先检查本档案候选；统计不证明配齐或达标。')
        saved_candidates = None
        try:
            saved = self.preferences()['profiles'].get(self.profile.currentData(), {})
            self.roles = [RoleRequest.from_dict(row) for row in saved.get('roles', [])]
            if 'priority_groups' in saved:
                validated = AllocationRequest.from_dict(
                    dict(
                        profile_id=self.profile.currentData(),
                        roles=saved.get('roles', []),
                        fully_tuned=saved.get('fully_tuned', True),
                        priority_groups=saved['priority_groups'],
                    )
                )
                self._priority_groups = validated.priority_groups
            pending = pending_role_keys(saved.get('pending_roles', []), self.roles)
            for key in pending:
                self.incoming.addItem('/'.join(key), key)
            self._sync_pending_role_draft()
            self.tuned.setChecked(saved.get('fully_tuned', True))
            from echo_workshop.application.matching.calculation import parse_candidate_ids

            saved_candidates = parse_candidate_ids(saved.get('candidate_item_ids'))
            self.status.setText('添加/编辑角色约束后生成方案；未知适配不参与分配')
        except (ValueError, KeyError, TypeError) as error:
            self._priority_error = (
                '配装配置无效：' + str(error) + '；请重新确认装备要求，或加载有效的历史方案。'
            )
            self.status.setText(self._priority_error)
        self._loading = False
        # The saved candidate scope is explicit input, restored before rendering.
        self.set_candidate_scope(saved_candidates)
        self.render_roles()
        self.refresh_saved()
        self.refresh()

    def render_roles(self):
        snapshot = self.runtime.weights.active
        self.roles_table.setRowCount(len(self.roles))
        for index, role in enumerate(self.roles):
            try:
                label = snapshot.template(*role.key)['name']
            except (KeyError, ValueError):
                label = '失效选择：' + '/'.join(role.key)
            strings = (
                label,
                f'{role.target:.2f} / ≤{role.cost_budget}',
                role.rule.element
                + ' / '
                + '、'.join(role.rule.sonatas)
                + '\n'
                + (
                    '、'.join(f'{name} 至少 {count} 件' for name, count in role.set_requirements)
                    or '不要求套装件数'
                ),
                '\n'.join(
                    f'C{slot.cost}: {"、".join(slot.main_names) or "不限主词条"} / {"固定一件" if slot.fixed_item_id else "不固定"}'
                    for slot in role.slots
                ),
            )
            for column, text in enumerate(strings):
                self.roles_table.setItem(index, column, QTableWidgetItem(text))
        self._template_version = snapshot.version
        self.update_role_summaries()

    def edit_role(self, editing):
        index = self.roles_table.currentRow()
        if editing and index < 0:
            raise ValueError('请选择一个角色配置')
        if self._editor is not None:
            raise ValueError('请先结束当前角色编辑')
        self._start_plan_job(
            'edit-role',
            dict(
                profile_id=self.profile.currentData(),
                snapshot=self.runtime.weights.active,
                roles=tuple(self.roles),
                editing=editing,
                index=index,
                role=self.roles[index] if editing else None,
            ),
        )

    def validate_current_role_edit(self, before, after):
        if before.character_id in self._edit_plans and before.key != after.key:
            raise ValueError('调整保持原角色和模版；切换模版请从“替换当前配装”进入。')
        if before.character_id in self._replace_plans and before.character_id != after.character_id:
            raise ValueError('替换当前配装保持原角色；其他角色请在配装页添加。')

    def remove_role(self):
        index = self.roles_table.currentRow()
        if index < 0:
            raise ValueError('请选择一个角色配置')
        self.roles.pop(index)
        self.changed()

    def start(self):
        with self._worker_lock:
            self._start_search()

    def _start_search(self):
        if self._closing or self.runtime._closed or self.task._destroyed:
            return
        if self._future is not None or self._io_future is not None or self._editor is not None:
            raise ValueError('请先结束当前搜索/方案作业')
        request = self.request()
        snapshot = self.runtime.weights.active
        self.changed()
        self._generation += 1
        generation = self._generation
        self._cancel = threading.Event()
        stop = _StopToken(self._cancel, self.runtime)
        from echo_workshop.application.search_budget import allocation_budget

        budget = allocation_budget(self.task.config, self.profile.currentData())
        candidate_ids, scope_generation = self._candidate_ids, self._scope_generation
        candidate_filters = (
            None if self._candidate_filters is None else dict(self._candidate_filters)
        )
        edits, replacements = dict(self._edit_plans), dict(self._replace_plans)
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='echo-allocation')
        self.status.setText('后台搜索中；固定库存修订和权重版本，不执行 OCR/装备操作')

        def work():
            try:
                reservations = self.runtime.inventory.loadout_snapshot(
                    request.profile_id, cancel_event=stop
                )
                filtered = (
                    reservations.records
                    if candidate_filters is None
                    else self.runtime.inventory_queries.filter_records(
                        reservations.records, cancel_event=stop, **candidate_filters
                    )
                )
                records = select_match_records(filtered, candidate_ids, cancel_event=stop)
                result = self.runtime.allocation.solve(
                    records,
                    request,
                    inventory_revision=reservations.inventory_revision,
                    snapshot=snapshot,
                    cancel_event=stop,
                    seconds=budget.seconds,
                    max_nodes=budget.nodes,
                    reservations=reservations,
                    edit_plans=edits,
                    replace_plans=replacements,
                )
                value, error = (
                    _AllocationPayload(result, records, snapshot, reservations, scope_generation),
                    '',
                )
                from echo_workshop.application.loadout_diff import plan_changes

                value.change_details = plan_changes(
                    result, reservations, snapshot, self.runtime.scores, stop
                )
            except Exception as caught:
                value, error = None, str(caught)
            try:
                self._signals.completed.emit(generation, value, error)
            except RuntimeError:
                pass

        self._future = self._pool.submit(work)
        self.refresh()

    @Slot(int, object, str)
    def _completed(self, generation, value, error):
        if generation != self._generation:
            return
        self._future = None
        if self._closing or self.runtime._closed or self.task._destroyed:
            return
        if error:
            self.status.setText('搜索结束：' + error)
        else:
            result, records, snapshot = value
            if (
                self._cancel.is_set()
                or not self.fresh(result)
                or getattr(value, 'scope_generation', self._scope_generation)
                != self._scope_generation
            ):
                self.status.setText('库存/权重/约束已变化，旧搜索结果已丢弃；请重新生成')
            else:
                self._result, self._records, self._snapshot, self._payload = (
                    result,
                    records,
                    snapshot,
                    None,
                )
                self._result_reservations = getattr(value, 'reservations', None)
                self._loadout_changes = getattr(value, 'change_details', ())
                self._result_scope_generation = self._scope_generation
                self.render_result(result.to_dict(), [])
        self.refresh()
        if self._history_queued:
            self._history_queued = False
            self.action(self.refresh_saved)

    def rating_snapshot(self):
        # Loaded plans keep their own weights even if a previous generated
        # result left _snapshot behind or live resources have since changed.
        return self._payload.get('weight_snapshot') if self._payload is not None else self._snapshot

    def _update_rating_colours(self, *_):
        # Style only the frozen metadata already rendered; no grading/IO here.
        for editor, rows in (
            (self.summary, self._summary_rating_lines),
            (self.detail_text, self._detail_rating_lines),
        ):
            for text, rating in rows:
                cursor = editor.document().find(text)
                if not cursor.isNull():
                    fmt = QTextCharFormat()
                    fmt.setForeground(QColor(rating_colour(rating, isDarkTheme())))
                    cursor.mergeCharFormat(fmt)
        for row in range(self.checklist.rowCount()):
            item = self.checklist.item(row, 3)
            if item is not None and item.data(_RATING_ITEM_ROLE) is not None:
                item.setForeground(
                    QColor(rating_colour(item.data(_RATING_ITEM_ROLE), isDarkTheme()))
                )

    def render_result(self, result, completed):
        from echo_workshop.application.compute_bridge import rating_result

        rows = result['roles']
        status = (
            '搜索完成：在本候选/约束/插件指标内最优（含上界剪枝）'
            if result['exhaustive']
            else '限时/节点受限：仅当前最佳合法方案，未证明全局最优'
        )
        self.status.setText(status + f'；节点 {result["nodes"]}；{result["elapsed"]:.3f} 秒')
        text = '插件目标分 = 五件单件当前分之和（+25 主词条口径，不标称官方整套分）\n'
        text += '目标为 0 不启用门槛；达标后的增益优先让给其他同级角色。\n'
        text += f'库存修订 {result["inventory_revision"]}；策略 {result["policy_version"]}\n'
        summary_ratings = []
        for row in rows:
            total = row['total_cents'] / 100
            rating = rating_result(row.get('rating')) if len(row['item_ids']) == 5 else None
            grade = f'；声骸评级 {rating.label}' if rating is not None else ''
            score_line = f'{row["label"]}：{total:.2f}；{len(row["item_ids"])} 件' + grade
            text += score_line + '\n'
            if rating is not None:
                summary_ratings.append((score_line, rating))
            text += '\n'.join(row['reasons']) + '\n'
        self.summary.setPlainText(text)
        self._summary_rating_lines = tuple(summary_ratings)
        self.summary.setToolTip('')
        self.show_outcome(result)
        records = {record.item_id: record for record in self._records}
        self.checklist.blockSignals(True)
        self.checklist.setRowCount(sum(len(row['item_ids']) for row in rows))
        index = 0
        for role in rows:
            item_ratings = tuple(role.get('item_ratings') or ())
            for slot, (item_id, score) in enumerate(zip(role['item_ids'], role['scores']), 1):
                record = records[item_id]
                main = record.main_stats[0] if record.main_stats else None
                rating = (
                    rating_result(item_ratings[slot - 1]) if slot <= len(item_ratings) else None
                )
                grade = f' · {rating.label}' if rating is not None else ''
                texts = (
                    f'{role["label"]} / 槽 {slot}',
                    f'{record.name}\n{stat_text(main)}' if main else record.name,
                    f'{record.sonata} / C{record.cost}',
                    f'{score / 100:.2f}' + grade + f'\n历史：{record.owner or "未知"}',
                    '用户已标记完成' if item_id in completed else '待手动换装',
                )
                for column, text in enumerate(texts):
                    widget = QTableWidgetItem(text)
                    widget.setData(Qt.UserRole, item_id)
                    widget.setToolTip(item_id)
                    if column == 3:
                        widget.setData(_RATING_ITEM_ROLE, rating)
                    self.checklist.setItem(index, column, widget)
                index += 1
        self.checklist.blockSignals(False)
        self.details()
        self._update_rating_colours()

    def cancel_job(self):
        self._cancel.set()
        self._io_cancel.set()
        self._history_queued = False
        self.status.setText('已请求取消；等待检查点，已提交的保存/进度保持完成')

    def refresh(self):
        if self.runtime._closed or self.task._destroyed:
            # start/show may hold the submission lock while executor cleanup
            # owns the stop lock and waits for submission to finish. Do not
            # synchronously join from inside that lock: close on the GUI queue.
            QTimer.singleShot(0, self.close)
            return
        busy = (
            self._future is not None
            or self._io_future is not None
            or getattr(self, '_pending_precheck', None) is not None
            or getattr(self, '_pending_role_editor', None) is not None
        )
        self.refresh_allocation_ux(busy)
        self.solve_button.setEnabled(not busy)
        self.load_button.setEnabled(not busy)
        self.refresh_history_button.setEnabled(not busy)
        self.cancel_button.setText('取消搜索 / 方案作业')
        self.cancel_button.setEnabled(busy)
        for widget in (self.profile, self.saved, self.tuned, *self._edit_buttons):
            widget.setEnabled(not busy)
        if busy:
            self.save_button.setEnabled(False)
            self.mark_button.setEnabled(False)
            self.protect_button.setEnabled(False)
            self.confirm_current_button.setEnabled(False)
            for button in self.current_buttons:
                button.setEnabled(False)
            return  # No Qt DB reads while the worker holds a read/commit lock.
        if self.runtime.weights.active.version != getattr(self, '_template_version', None):
            self.render_roles()
        fresh = self.fresh()
        current = self.plan_is_current()
        revision = self.inventory_revision()
        snapshot_fresh = (
            self._current_loadouts is not None
            and self._current_loadouts.profile_id == self.profile.currentData()
            and (revision is None or self._current_loadouts.inventory_revision == revision)
        )
        for button in self.current_buttons:
            button.setEnabled(
                snapshot_fresh and self.current_row() is not None and self._editor is None
            )
        self.confirm_current_button.setEnabled(
            self._pending_current_action is not None and self._editor is None
        )
        self.save_button.setEnabled(fresh and self._result is not None and self._payload is None)
        selected = self.selected()
        self.mark_button.setEnabled(
            self._payload is not None
            and selected is not None
            and (fresh or self.plan_is_current(selected.item_id))
        )
        result = self.result_data()
        complete = result is not None and all(len(row['item_ids']) == 5 for row in result['roles'])
        protected = (
            {
                row['character_id']
                for row in self._current_loadouts.current
                if self._payload
                and row['plan_id'] == self._payload['plan_id']
                and set(row['item_ids']) <= set(protected_item_ids(row))
                and not row['issues']
            }
            if snapshot_fresh
            else set()
        )
        already_protected = (
            complete and {row['character_id'] for row in result['roles']} <= protected
        )
        self.protect_button.setEnabled(
            complete
            and not already_protected
            and (snapshot_fresh if self._payload else fresh)
            and self._editor is None
        )
        self._all_result_protected = already_protected
        if (
            revision is not None
            and (self._result is not None or self._payload is not None)
            and not fresh
            and not current
        ):
            self.status.setText(
                '库存/权重/约束已变化：当前仅作历史找回，保存/换装标记暂停；请重新生成，旧方案和进度保留'
            )
        elif current and getattr(self.status, 'full_text', self.status.text()).startswith(
            '库存/权重/约束已变化：'
        ):
            self.status.setText('当前配装已保存；配装保护与手动装备进度分别管理。')

    def showEvent(self, event):
        with self._worker_lock:
            if not self.runtime._closed and not self.task._destroyed:
                self._closing = False
                self.timer.start()
                self.refresh()
                if self._history_needs_refresh and self._future is None and self._io_future is None:
                    self.action(self.refresh_saved)
        super().showEvent(event)

    def stop_worker(self):
        # Close submission before queued UI timers or worker receipts may run.
        with self._stop_lock:
            with self._worker_lock:
                self._closing = True
                self._cancel.set()
                self._io_cancel.set()
                self._generation += 1
                self._io_generation += 1
                self._history_queued = False
                self._pending_current_action = None
                pool = self._pool
            if hasattr(self, 'saved_workspace'):
                self.saved_workspace.stop_projection()
            if pool is not None:
                # Page/host close must not wait for a long search; the worker
                # observes the cancellation token and the runtime close path.
                pool.shutdown(wait=False, cancel_futures=True)
            with self._worker_lock:
                self._pool = None
                self._future = self._io_future = None

    def closeEvent(self, event):
        if self._editor is not None and not self._editor.reject():
            event.ignore()
            return
        self.timer.stop()
        self.stop_worker()
        self.cancel_current_action()
        self.status.setText('窗口作业已结束；已提交方案/进度保留，请刷新/打开历史核对')
        super().closeEvent(event)
