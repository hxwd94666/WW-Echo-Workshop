# 管理方案保存历史查看及当前配装保护作业。
from concurrent.futures import ThreadPoolExecutor
import threading
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QPixmap
from qfluentwidgets import MessageBox
from echo_workshop.ui.matching.controller import _StopToken
from echo_workshop.ui.inventory.evidence import open_evidence
from echo_workshop.application.operations import checkpoint, OperationCancelled
from echo_workshop.core.loadout import protected_item_ids

_RATING_ITEM_ROLE = int(Qt.UserRole) + 20


class SavedPlanController:
    def _start_plan_job(self, kind, arguments):
        with self._worker_lock:
            self._submit_plan_job(kind, arguments)

    def _submit_plan_job(self, kind, arguments):
        if self._closing or self.runtime._closed or self.task._destroyed:
            return
        if (
            self._future is not None
            or self._io_future is not None
            or self._editor is not None
            or getattr(self, '_pending_role_editor', None) is not None
        ):
            raise ValueError('请先结束当前搜索/方案作业')
        self.cancel_current_action()
        self._io_generation += 1
        generation = self._io_generation
        self._io_cancel = threading.Event()
        stop = _StopToken(self._io_cancel, self.runtime)
        self._io_kind, self._io_profile = kind, self.profile.currentData()
        arguments = dict(arguments)
        self._io_arguments = dict(arguments)
        selected = self.saved.currentData()
        commands = self.runtime.plan_commands
        runtime = self.runtime
        expected_revision = arguments.pop(
            '_guard_revision',
            arguments['result'].inventory_revision
            if kind == 'save'
            else arguments.get('inventory_revision'),
        )
        expected_weight = arguments.pop(
            '_guard_weight',
            arguments['result'].weight_version
            if kind == 'save'
            else arguments.get('weight_version'),
        )
        current_write = kind in (
            'save-protect',
            'protect',
            'release-current',
            'remove-current',
            'save-loadout',
            'release-items',
        )
        candidate_ids = self._candidate_ids
        candidate_filters = (
            None if self._candidate_filters is None else dict(self._candidate_filters)
        )
        scope_generation = self._scope_generation
        edits, replacements = dict(self._edit_plans), dict(self._replace_plans)

        def before_commit():
            checkpoint(stop)
            # Current-loadout writes validate both revisions in their transaction
            # before bumping them. This hook runs AFTER that bump: check weight
            # and cancellation only, not the old inventory revision again.
            if (
                not current_write and runtime.inventory.revision != expected_revision
            ) or runtime.weights.active.version != expected_weight:
                raise ValueError('库存/权重已变化，未提交方案/进度；旧文件保留，请重新评估')

        def progress(stage, done, total):
            try:
                self._signals.plan_progress.emit(generation, stage, done, total)
            except RuntimeError:
                pass  # A removed Qt signal owner is not a disk-commit outcome.

        def work():
            try:
                value = commands.execute(
                    kind,
                    arguments,
                    cancel_event=stop,
                    progress=progress,
                    before_commit=before_commit
                    if current_write or kind in ('save', 'mark', 'backup-loadout', 'remove-backup')
                    else None,
                    candidate_ids=candidate_ids,
                    candidate_filters=candidate_filters,
                    scope_generation=scope_generation,
                    edit_plans=edits,
                    replace_plans=replacements,
                )
                error = ''
            except OperationCancelled:
                value, error = (
                    None,
                    '候选准备已取消，原角色配置保留'
                    if kind == 'edit-role'
                    else '方案作业已取消，未发布部分读取/未提交写入',
                )
            except Exception as caught:
                value, error = None, str(caught)
            try:
                self._signals.plan_completed.emit(generation, (value, selected), error)
            except RuntimeError:
                pass

        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='echo-allocation')
        self.status.setText(
            '后台检查完整候选池；可取消，原清单保留'
            if kind == 'precheck'
            else '后台准备角色固定实例；可取消，原角色配置保留'
            if kind == 'edit-role'
            else '后台方案作业：' + kind + '；原清单/进度保留，期间编辑暂缓'
        )
        self._io_future = self._pool.submit(work)
        self.refresh()

    @Slot(int, str, int, int)
    def _plan_progress(self, generation, stage, done, total):
        if generation == self._io_generation and self._io_future is not None and not self._closing:
            self.status.setText(f'{stage}：{done}/{total}；可取消，未完成时保留原清单')

    @Slot(int, object, str)
    def _plan_completed(self, generation, output, error):
        if generation != self._io_generation:
            return
        self._io_future = None
        if self._closing or self.runtime._closed or self.task._destroyed:
            return
        value, selected = output
        if self._io_profile != self.profile.currentData():
            self.status.setText('档案已变；旧作业不更新此档案，已提交文件仍保留')
        elif error:
            self.status.setText(error)
            if self._io_arguments.get('saved_recheck'):
                self.saved_workspace.verification_failed(error, self._io_arguments)
            if self._io_arguments.get('saved_role_view'):
                self.saved_workspace.show_error(error, self._io_arguments.get('view_token'))
        elif self._io_cancel.is_set() and not value.committed:
            self.status.setText('方案作业已取消，旧列表/清单保留')
            if self._io_arguments.get('saved_recheck'):
                self.saved_workspace.verification_failed(
                    '核验已取消，可重新核验。', self._io_arguments
                )
        elif self._dispatch_loadout_job(value):
            pass
        elif value.kind == 'precheck':
            self.action(lambda: self.publish_precheck(value.payload))
        elif value.kind == 'edit-role':
            self.action(lambda: self._open_role_editor(value.payload))
        elif value.kind == 'list':
            self._history_needs_refresh = False
            self.publish_current_loadouts(value.loadouts)
            self.saved.clear()
            for plan_id, date in value.rows:
                self.saved.addItem(date + ' · ' + plan_id[:8], plan_id)
            if selected is not None and self.saved.findData(selected) >= 0:
                self.saved.setCurrentIndex(self.saved.findData(selected))
            if value.errors:
                self.status.setText(
                    '部分历史方案暂未读取，原文件保留；可稍后刷新列表：'
                    + '; '.join(value.errors[:3])
                )
            else:
                self.status.setText(f'历史列表已校验：{len(value.rows)} 个方案；选中项可打开')
        elif value.kind == 'load':
            self.publish_current_loadouts(value.loadouts)
            if self._io_arguments.get('adjust_character'):
                self.action(lambda: self.begin_current_adjustment(value, self._io_arguments))
                self.refresh()
                return
            self._loading = True
            self.roles = list(value.request.roles)
            self._priority_groups = value.request.priority_groups
            self._priority_error = ''
            self.tuned.setChecked(value.request.fully_tuned)
            # Explicit history load restores its fully validated role request;
            # it is not a way to expose results for unrelated unconfirmed roles.
            self.clear_role_draft()
            self._loading = False
            self._result, self._payload, self._records = None, value.payload, value.records
            self._loadout_changes = ()
            self.render_roles()
            self.render_result(value.payload['result'], value.payload['completed'])
            # Viewing a frozen history request never rewrites independent working settings.
        elif value.kind in ('release-current', 'remove-current'):
            self._current_loadouts = None
            self._history_queued = True
            self.status.setText('当前配装已更新；历史方案与手动进度保留。')
        else:
            self._payload = value.payload
            if value.kind in ('save', 'save-protect', 'protect'):
                plan_id = value.payload['plan_id']
                if self.saved.findData(plan_id) < 0:
                    self.saved.addItem(value.payload['created_at'] + ' · ' + plan_id[:8], plan_id)
                self.saved.setCurrentIndex(self.saved.findData(plan_id))
            self.render_result(value.payload['result'], value.payload['completed'])
            if value.kind in ('save-protect', 'protect'):
                self._current_loadouts = None
                self._history_queued = True
            self.status.setText(
                '配装已保存并保护，正在刷新当前配装；请在游戏中手动装备。'
                if value.kind in ('save-protect', 'protect')
                else '方案/手动进度已提交；不修改游戏或库存归属，迟到取消不撤销已保存数据'
            )
        self.refresh()
        if self._history_queued and self._future is None:
            self._history_queued = False
            self.action(self.refresh_saved)

    def publish_current_loadouts(self, snapshot):
        self._current_loadouts = snapshot
        selected = self.current_choices.currentData()
        self.current_choices.blockSignals(True)
        self.current_choices.clear()
        for row in snapshot.current if snapshot is not None else ():
            count = len(protected_item_ids(row))
            state = '待复核' if row['issues'] else f'保护 {count}/5' if count else '未保护'
            self.current_choices.addItem(row['label'] + ' · ' + state, row['character_id'])
        index = self.current_choices.findData(selected)
        if index >= 0:
            self.current_choices.setCurrentIndex(index)
        self.current_choices.blockSignals(False)
        self.update_current_note()

    def current_row(self):
        return (
            next(
                (
                    row
                    for row in self._current_loadouts.current
                    if row['character_id'] == self.current_choices.currentData()
                ),
                None,
            )
            if self._current_loadouts
            else None
        )

    def update_current_note(self, *_):
        row = self.current_row()
        self.current_note.setText(
            '暂无当前配装；保存并保护后显示在这里。'
            if row is None
            else '声骸记录已变化或缺失，请先核对。'
            if row['issues']
            else f'{len(protected_item_ids(row))} 件已保护，其他角色配装时会排除它们。'
            if row['protected']
            else '当前配装未保护，声骸可被后续方案使用。'
        )

    def require_current_snapshot(self):
        snapshot = self._current_loadouts
        revision = self.inventory_revision()
        # A busy probe is unknown, not evidence that the displayed relationship
        # changed. Preview may use its frozen cache; every write still checks
        # both expected revisions inside the repository transaction.
        if (
            snapshot is None
            or snapshot.profile_id != self.profile.currentData()
            or (revision is not None and snapshot.inventory_revision != revision)
        ):
            raise ValueError('库存或配装保护已变化，请刷新已保存配装后重试。')
        return snapshot

    def plan_is_current(self, item_id=None):
        if self._payload is None or self._current_loadouts is None:
            return False
        snapshot = self._current_loadouts
        if (
            snapshot.profile_id != self.profile.currentData()
            or snapshot.inventory_revision != self.inventory_revision()
        ):
            return False
        return any(
            row['plan_id'] == self._payload['plan_id']
            and not row['issues']
            and (item_id is None or item_id in row['item_ids'])
            for row in snapshot.current
        )

    def cancel_current_action(self, *_):
        self._pending_current_action = None
        if hasattr(self, 'confirmation_box'):
            self.confirmation_box.hide()

    def queue_current_action(self, kind, arguments, message):
        if self._future is not None or self._io_future is not None or self._editor is not None:
            raise ValueError('请先结束当前作业或编辑。')
        self._pending_current_action = (kind, dict(arguments), self._scope_generation)
        self.confirmation_text.setText(message)
        self.confirm_current_button.setEnabled(True)
        self.confirmation_box.show()
        self.steps.setCurrentIndex(2)

    def confirm_current_action(self):
        pending = self._pending_current_action
        if pending is None:
            raise ValueError('没有待确认的配装操作。')
        kind, arguments, generation = pending
        if (
            generation != self._scope_generation
            or arguments['profile_id'] != self.profile.currentData()
            or (
                kind in ('release-items', 'remove-backup')
                and arguments['view_token'] != self.saved_workspace.view_token
            )
        ):
            self.cancel_current_action()
            raise ValueError('档案或候选范围已变化，请重新操作。')
        self.cancel_current_action()
        self._start_plan_job(kind, arguments)

    def begin_current_adjustment(self, value, arguments):
        snapshot = self.require_current_snapshot()
        character = arguments['adjust_character']
        row = next((row for row in snapshot.current if row['character_id'] == character), None)
        if row is None or row['plan_id'] != arguments['plan_id']:
            raise ValueError('当前配装已变化，请刷新后重新进入调整。')
        self._loading = True
        self.roles = [role for role in value.request.roles if role.character_id == character]
        from echo_workshop.application.matching.calculation import restrict_priority_groups

        self._priority_groups = (
            restrict_priority_groups(
                value.request.priority_groups, tuple(role.key for role in self.roles)
            )
            if value.request.priority_groups
            else ()
        )
        self._priority_error = ''
        self.tuned.setChecked(value.request.fully_tuned)
        self._loading = False
        self.clear_role_draft()
        self._subset_adjustment = True
        self._result = self._payload = None
        self._records = ()
        self.checklist.setRowCount(0)
        self.set_candidate_scope(
            self._candidate_ids,
            filters=self._candidate_filters,
            label=self._scope_label,
            edit_plans={} if arguments['replacing'] else {character: row['plan_id']},
            replace_plans={character: row['plan_id']} if arguments['replacing'] else {},
        )
        self.render_roles()
        self.steps.setCurrentIndex(1)
        self.status.setText(
            '可修改装备要求后重新生成；旧配装保持保护，提交替换成功后才更新。'
            if arguments['replacing']
            else '调整当前配装：可复用自己的预留声骸，保持原模版。'
        )

    def change_current(self, kind):
        snapshot = self.require_current_snapshot()
        row = self.current_row()
        if row is None:
            raise ValueError('请选择当前配装。')
        arguments = dict(
            profile_id=self.profile.currentData(),
            character_id=row['character_id'],
            expected_revision=snapshot.inventory_revision,
            expected_loadout_revision=snapshot.revision,
            _guard_weight=self.runtime.weights.active.version,
        )
        message = (
            ('解除保护：' if kind == 'release-current' else '移除当前配装：')
            + row['label']
            + '。这会释放其声骸预留；历史方案和手动进度保留，不操作游戏。'
        )
        if kind in ('remove-current', 'release-current'):
            self.cancel_current_action()
            self._pending_current_action = (kind, dict(arguments), self._scope_generation)
            title = '解除保护' if kind == 'release-current' else '移除配装'
            if not MessageBox(title, message, self.window()).exec():
                self.cancel_current_action()
                return
            self.confirm_current_action()
            return
        self.queue_current_action(kind, arguments, message)

    def refresh_saved(self):
        if not self.profile.currentData():
            return
        if (
            self._io_future is not None
            or self._future is not None
            or getattr(self, '_pending_precheck', None) is not None
        ):
            self._history_queued = True
            if self._io_kind == 'list':
                self._io_cancel.set()
            return
        self._history_needs_refresh = True
        self._start_plan_job('list', dict(profile_id=self.profile.currentData()))

    def selected(self):
        item = self.checklist.currentItem()
        item_id = item.data(Qt.UserRole) if item else None
        return next((record for record in self._records if record.item_id == item_id), None)

    def details(self):
        self._detail_rating_lines = ()
        record = self.selected()
        if hasattr(self, 'mark_button') and self._future is None and self._io_future is None:
            self.mark_button.setEnabled(
                record is not None
                and self._payload is not None
                and (self.fresh() or self.plan_is_current(record.item_id))
            )
        self.image.clear()
        if record is None:
            self.detail_text.clear()
            self.image.setText('选择清单中的具体实例')
            return
        text = f'UUID：{record.item_id}\n名称：{record.name}\n套装：{record.sonata}\nCOST {record.cost} / 实际 +{record.level}\n'
        result = (
            self._payload['result']
            if self._payload is not None
            else self._result.to_dict()
            if self._result is not None
            else None
        )
        if result is not None:
            from echo_workshop.application.compute_bridge import rating_result

            for role in result['roles']:
                if record.item_id in role['item_ids']:
                    slot = role['item_ids'].index(record.item_id)
                    score = role['scores'][slot]
                    ratings = tuple(role.get('item_ratings') or ())
                    rating = rating_result(ratings[slot]) if slot < len(ratings) else None
                    if rating is not None:
                        score_line = (
                            f'单件评级：{rating.label} · 保存/生成时当前分 {score / 100:.2f}'
                        )
                        text += score_line + '\n'
                        self._detail_rating_lines = ((score_line, rating),)
                    break
        text += '\n'.join(
            f'{stat.stat_name} {stat.value:g}（原值 {stat.value_text}）'
            for stat in record.main_stats + record.sub_stats
        )
        text += f'\n采集：{record.captured_at}\n历史装配：{record.owner or "未知"}\n归属更新时间：{record.owner_updated_at or "未知"}\n保护：{record.locked}\n备注：{record.note}\n原 OCR：\n{record.raw_text}\n\n方案保存的是历史输入；手动完成标记不修改库存归属、不验证游戏。'
        self.detail_text.setPlainText(text)
        self._update_rating_colours()
        if hasattr(self.image, 'set_record'):
            self.image.set_record(record)
            return
        if record.screenshot_ref:
            try:
                image = QPixmap()
                image.loadFromData(self.runtime.evidence.read(record.screenshot_ref), 'PNG')
                self.image.setPixmap(
                    image.scaled(160, 90, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            except (ValueError, OSError):
                self.image.setText('原始截图缺失或校验失败')
        else:
            self.image.setText('此实例未保存截图')

    def show_evidence(self):
        if self.selected():
            open_evidence(self.selected(), self.runtime.evidence, self)

    def mark(self):
        record = self.selected()
        if (
            record is None
            or self._payload is None
            or (not self.fresh() and not self.plan_is_current(record.item_id))
        ):
            raise ValueError('请选择当前有效方案内实例；历史/过期方案保留但不改进度')
        self._start_plan_job(
            'mark',
            dict(
                plan_id=self._payload['plan_id'],
                item_id=record.item_id,
                completed=record.item_id not in self._payload['completed'],
                inventory_revision=self._payload['result']['inventory_revision'],
                weight_version=self._payload['result']['weight_version'],
                progress_revision=self._payload['progress_revision'],
                _guard_revision=self.runtime.inventory.revision,
                _guard_weight=self.runtime.weights.active.version,
            ),
        )
