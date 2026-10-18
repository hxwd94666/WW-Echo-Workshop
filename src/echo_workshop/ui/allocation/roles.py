# 管理配装角色要求优先级及保存取消的内嵌表单。
from echo_workshop.core.allocation.models import AllocationRequest
from echo_workshop.application.allocation_preferences import pending_role_keys
from echo_workshop.ui.widgets.inputs import combo_search_draft, restore_combo_search


class AllocationRoleController:
    def _sync_workspace_roles(self):
        if not hasattr(self, 'role_navigation'):
            return
        pending = {tuple(self.incoming.itemData(i)) for i in range(self.incoming.count())}
        configured = [role.key for role in self.roles]
        keys = configured + [
            tuple(self.incoming.itemData(i))
            for i in range(self.incoming.count())
            if tuple(self.incoming.itemData(i)) not in configured
        ]
        order = tuple(key for group in self._priority_groups for key in group)
        if not order:
            order = (
                getattr(self, '_working_role_order', ())
                if not getattr(self, '_viewing_history', False)
                else ()
            )
        keys = [key for key in order if key in keys] + [key for key in keys if key not in order]
        self.role_navigation.set_entries(
            ((key, key in pending) for key in keys),
            self.runtime.weights.active,
            current=self._workspace_key,
            avatars=self.runtime.avatars,
            groups=self._priority_groups,
        )
        self._workspace_avatar_revision = self.runtime.avatars.revision
        self._workspace_key = self.role_navigation.current_key
        self._focus_workspace_role(self._workspace_key)
        self.pending_notice.setText(f'待确认 {len(pending)} 个角色；点击齿轮设置装备要求。')
        self.requirements_prompt.setText('点击齿轮设置装备要求。' if keys else '选择角色开始配装。')

    def _sync_pending_role_draft(self):
        keys = pending_role_keys(
            tuple(self.incoming.itemData(index) for index in range(self.incoming.count())),
            self.roles,
        )
        profile = self.profile.currentData()
        previous = self._role_draft
        rules = previous[2] if previous is not None and previous[0] == profile else {}
        self._role_draft = (
            (profile, keys, {key: value for key, value in rules.items() if key in keys})
            if keys
            else None
        )
        self.incoming_box.setVisible(bool(keys))
        return keys

    def _priority_edit_error(self):
        if self._closing or self.runtime._closed or self.task._destroyed:
            return '配装页面已停止，请重新打开。'
        if (
            self._future is not None
            or self._io_future is not None
            or getattr(self, '_pending_precheck', None) is not None
            or getattr(self, '_pending_role_editor', None) is not None
            or getattr(self, '_pending_current_action', None) is not None
        ):
            return '请先完成或取消当前作业与确认操作，再修改配装优先级。'
        if self._editor is not None:
            return '请先保存或取消装备要求编辑，再修改配装优先级。'
        if self.runtime.backups.status.busy:
            return '请先完成或取消数据作业，再修改配装优先级。'
        if getattr(self, '_saved_view', False):
            return '请先返回当前配装，再调整新方案的配装优先级。'
        if self._viewing_history:
            return '正在查看历史方案；请先返回当前配装再修改。'
        return ''

    def _allocation_priority_state(self):
        from echo_workshop.application.matching.calculation import restrict_priority_groups

        keys = tuple(key for key, _ in self.role_navigation.strip._entries or ())
        groups = restrict_priority_groups(self._priority_groups, keys)
        levels = {key: index for index, group in enumerate(groups) for key in group}
        links = tuple(
            '>' if levels[key] != levels[previous] else '=' for previous, key in zip(keys, keys[1:])
        )
        return keys, links

    def _edit_allocation_priority(self, key, value):
        from echo_workshop.application.matching.calculation import links_to_groups

        reason = self._priority_edit_error()
        if reason:
            self._sync_workspace_roles()
            raise ValueError(reason)
        keys, previous = self._allocation_priority_state()
        if key not in keys[1:] or value not in ('=', '>'):
            raise ValueError('请选择相邻配装角色的优先级关系。')
        links = list(previous)
        links[keys.index(key) - 1] = value
        self._commit_allocation_priorities(links_to_groups(keys, links), keys)

    def _reorder_allocation_roles(self, keys):
        from echo_workshop.application.matching.calculation import stable_keys, links_to_groups

        reason = self._priority_edit_error()
        if reason:
            self._sync_workspace_roles()
            raise ValueError(reason)
        keys = stable_keys(keys)
        previous, links = self._allocation_priority_state()
        if len(keys) != len(previous) or set(keys) != set(previous):
            self._sync_workspace_roles()
            raise ValueError('配装角色已变化，请重新拖拽。')
        self._commit_allocation_priorities(links_to_groups(keys, links), keys)

    def _commit_allocation_priorities(self, groups, order):
        from echo_workshop.application.matching.calculation import restrict_priority_groups

        roles = tuple(
            next(role for role in self.roles if role.key == key)
            for key in order
            if any(role.key == key for role in self.roles)
        )
        try:
            if roles and not self.incoming.count():
                AllocationRequest(
                    self.profile.currentData(),
                    roles,
                    self.tuned.isChecked(),
                    restrict_priority_groups(groups, tuple(role.key for role in roles)),
                )
        except (ValueError, KeyError, TypeError):
            # A rejected relation must never leave a chip showing an unsaved value.
            self._sync_workspace_roles()
            raise
        self._priority_groups = groups
        self._priority_error = ''
        self.roles = list(roles)
        self._working_role_order = tuple(order)
        # Drop only this view's generated/loaded evidence, never its saved
        # history file, current-loadout protection or candidate scope/pins.
        self._invalidate_generated_view()
        self.changed()
        self.status.setText('优先级已更新，请重新生成方案。')

    def _focus_workspace_role(self, key):
        self._workspace_key = tuple(key) if key is not None else None
        if hasattr(self, 'allocation_role_picker'):
            picker = self.allocation_role_picker
            # 正在输入的角色搜索不被后台刷新顶掉；无草稿时才同步为当前工作区角色。
            if not restore_combo_search(picker, combo_search_draft(picker)):
                blocked = picker.blockSignals(True)
                picker.setCurrentIndex(picker.findData(self._workspace_key))
                picker.blockSignals(blocked)
        role = next((role for role in self.roles if role.key == self._workspace_key), None)
        if role is not None:
            self.roles_table.setCurrentCell(self.roles.index(role), 0)

    def _reload_allocation_picker(self):
        if not hasattr(self, 'allocation_role_picker'):
            return
        picker = self.allocation_role_picker
        draft = combo_search_draft(picker)
        previous = picker.currentData()
        blocked = picker.blockSignals(True)
        picker.clear()
        for label, (character, modal, _) in self.runtime.weights.active.options.items():
            picker.addItem(label, (character, modal))
        picker.setCurrentIndex(picker.findData(previous) if previous else -1)
        picker.blockSignals(blocked)
        restore_combo_search(picker, draft)
        self._allocation_picker_version = self.runtime.weights.active.version

    def _choose_allocation_role(self, index):
        if index < 0:
            return
        self._add_allocation_role(self.allocation_role_picker.itemData(index))

    def _add_allocation_role(self, key):
        reason = self._priority_edit_error()
        if reason:
            raise ValueError(reason)
        key = tuple(key)
        template = self.runtime.weights.active.template(*key)
        pending = self._sync_pending_role_draft()
        selected = tuple(role.key for role in self.roles) + pending
        existing = next((value for value in selected if value[0] == key[0]), None)
        if existing is not None:
            self._workspace_key = existing
            self._sync_workspace_roles()
            if existing != key:
                raise ValueError('此角色已参加配装；请先移除，再选择其他模版。')
            self.status.setText('已选中此角色；点击齿轮设置装备要求。')
            return
        pending_role_keys((*pending, key), self.roles)
        self.incoming.addItem(template['name'], key)
        self._sync_pending_role_draft()
        self._workspace_key = key
        self._working_role_order = tuple(dict.fromkeys((*self._working_role_order, *selected, key)))
        self._invalidate_generated_view()
        self.changed()
        self.status.setText('角色已加入；点击齿轮设置装备要求。')

    def _workspace_role_clicked(self, key):
        self._focus_workspace_role(key)
        self.result_cards.scroll_to_role(key)

    def _edit_allocation_role(self, key):
        reason = self._priority_edit_error()
        if reason:
            raise ValueError(reason)
        self._role_editor_return = self.steps.currentIndex(), self._workspace_key
        self._focus_workspace_role(key)
        self._edit_workspace_role()

    def _remove_allocation_role(self, key):
        from echo_workshop.application.matching.calculation import restrict_priority_groups

        reason = self._priority_edit_error()
        if reason:
            self._sync_workspace_roles()
            raise ValueError(reason)
        key = tuple(key)
        remaining = [role for role in self.roles if role.key != key]
        pending = self.incoming.findData(key)
        if len(remaining) == len(self.roles) and pending < 0:
            raise ValueError('角色已变化，请重新选择。')
        self.roles = remaining
        self._removed_role_ids.add(key[0])
        if pending >= 0:
            self.incoming.removeItem(pending)
        pending_keys = self._sync_pending_role_draft()
        self._working_role_order = tuple(
            value for value in self._working_role_order if value != key
        )
        keys = tuple(role.key for role in remaining) + pending_keys
        self._priority_groups = (
            restrict_priority_groups(self._priority_groups, keys)
            if keys and self._priority_groups
            else ()
        )
        self._workspace_key = keys[0] if keys else None
        self._role_cost_layouts.pop(key, None)
        self._role_requirements_cache.pop(key, None)
        self._role_requirement_conflicts.pop(key, None)
        self._edit_plans.pop(key[0], None)
        self._replace_plans.pop(key[0], None)
        if not remaining:
            self._edit_plans.clear()
            self._replace_plans.clear()
        self._invalidate_generated_view()
        self.changed()
        self.steps.setCurrentIndex(1)
        self.status.setText('角色已移除；已保存方案和配装保护保留。')

    def _edit_workspace_role(self):
        key = self._workspace_key
        if key is None:
            raise ValueError('请先添加参与角色。')
        self._managed_role_editor_key = key
        pending = self.incoming.findData(key)
        if pending >= 0:
            self.incoming.setCurrentIndex(pending)
            self.configure_pending_role()
            return
        row = next((i for i, role in enumerate(self.roles) if role.key == key), None)
        if row is None:
            raise ValueError('此角色配置已变化，请重新选择。')
        self.roles_table.setCurrentCell(row, 0)
        self.edit_role(True)

    def render_roles(self):
        super().render_roles()
        self._sync_workspace_roles()

    def update_role_summaries(self):
        self._sync_workspace_roles()

    def _open_role_editor(self, context, *, receipt=None, retries=0):
        import logging
        from PySide6.QtCore import QTimer
        from echo_workshop.ui.widgets.editors import InlineRoleEditor

        receipt = receipt or (self._io_generation, self._io_cancel)

        def active():
            return (
                not self._closing
                and not self.runtime._closed
                and not self.task._destroyed
                and receipt[0] == self._io_generation
                and not receipt[1].is_set()
                and context['profile_id'] == self.profile.currentData()
                and context['snapshot'].version == self.runtime.weights.active.version
                and context['roles'] == tuple(self.roles)
                and context.get('scope_generation', self._scope_generation)
                == self._scope_generation
            )

        def clear_pending():
            pending = getattr(self, '_pending_role_editor', None)
            if pending is not None and pending['receipt'] == receipt:
                self._pending_role_editor = None

        if not active() or self._editor is not None:
            clear_pending()
            return
        revision = self.runtime.inventory.try_revision()
        if revision is None:
            if retries == 0:
                logging.getLogger('ok.EchoAllocationRoles').info(
                    'role editor receipt waiting generation=%s reason=inventory-busy', receipt[0]
                )
            if retries < 80:
                self._pending_role_editor = dict(receipt=receipt)
                self.status.setText('正在等待库存读取，可取消。')
                self.refresh()
                QTimer.singleShot(
                    25,
                    self,
                    lambda: self._open_role_editor(context, receipt=receipt, retries=retries + 1),
                )
            else:
                clear_pending()
                self.status.setText('库存繁忙，请稍后重新打开装备要求。')
                self.refresh()
            return
        clear_pending()
        if revision != context['revision']:
            self.status.setText('数据已变化，请重新打开装备要求。')
            self.refresh()
            return
        editor = InlineRoleEditor(
            context['snapshot'],
            role=context['role'],
            parent=self,
            catalog=context['catalog'],
            seed_key=context.get('seed_key'),
            seed_rule=context.get('seed_rule'),
            evidence=self.runtime.evidence,
        )
        self.seed_role_requirements(
            editor, context['role'].key if context['role'] is not None else context.get('seed_key')
        )
        if hasattr(editor, 'set_resources'):
            editor.set_resources(
                avatars=self.runtime.avatars,
                sonata=self.runtime.sonata.active,
                effects=self.runtime.sonata_effects.active,
            )
        managed = getattr(self, '_managed_role_editor_key', None)
        identity = context['role'].key if context['role'] is not None else context.get('seed_key')
        if managed is not None and managed == identity:
            editor.role_picker.hide()
            editor.role_picker.setEnabled(False)
        editor.mark_confirmed_state()
        self._managed_role_editor_key = None
        self._editor = editor
        self.editor_area.setWidget(editor)
        self.pages.hide()
        self.role_navigation.hide()
        self.role_add_bar.hide()
        self.result_toolbar.hide()
        self.editor_area.show()
        return_state = getattr(
            self, '_role_editor_return', (self.steps.currentIndex(), self._workspace_key)
        )
        self._role_editor_return = None

        def finish(saved=False):
            self._editor = None
            self.editor_area.takeWidget()
            editor.deleteLater()
            self.editor_area.hide()
            if self._closing or self.runtime._closed or self.task._destroyed:
                return
            self.pages.show()
            self.result_toolbar.show()
            self.role_add_bar.show()
            if not saved and return_state is not None:
                step, self._workspace_key = return_state
                self.steps.setCurrentIndex(step)
            self._sync_workspace_roles()
            self.refresh()

        def save():
            if not active():
                editor.error.setText('数据已变化，未保存；取消后重新打开编辑。')
                return
            revision = self.runtime.inventory.try_revision()
            if revision is None:
                editor.error.setText('库存正在读取，请稍后再保存。')
                return
            if revision != context['revision']:
                editor.error.setText('数据已变化，未保存；取消后重新打开编辑。')
                return
            values = list(self.roles)
            if context['editing']:
                try:
                    self.validate_current_role_edit(context['role'], editor.value)
                except ValueError as error:
                    editor.error.setText(str(error))
                    return
                values[context['index']] = editor.value
            else:
                values.append(editor.value)
            try:
                AllocationRequest(self.profile.currentData(), tuple(values), self.tuned.isChecked())
            except (ValueError, KeyError) as error:
                editor.error.setText('请检查要求：' + str(error))
                return
            from echo_workshop.application.matching.calculation import restrict_priority_groups

            self.roles = values
            self._removed_role_ids.discard(editor.value.character_id)
            if self._priority_groups:
                self._priority_groups = restrict_priority_groups(
                    self._priority_groups, tuple(role.key for role in values)
                )
            self._workspace_key = editor.value.key
            self._invalidate_generated_view()
            self.remember_saved_cost_layout(editor.value)
            index = self.incoming.findData(editor.value.key)
            if index >= 0:
                self.incoming.removeItem(index)
            self._sync_pending_role_draft()
            self.changed()
            self.steps.setCurrentIndex(1)
            finish(saved=True)

        editor.saveRequested.connect(save)
        editor.cancelled.connect(finish)
        self.refresh()
