# 衔接明确范围的配装备份保存与独立历史预览。
class LoadoutActionController:
    def show_loadout_receipt(self, text, *, warning=False):
        from qfluentwidgets import InfoBar, InfoBarPosition

        self._last_receipt_text = str(text)
        self._last_receipt_warning = warning
        show = InfoBar.warning if warning else InfoBar.success
        show(
            title='',
            content=str(text),
            parent=self.window(),
            position=InfoBarPosition.TOP,
            duration=8000 if warning else 3500,
        )

    def _loadout_busy(self):
        return bool(
            self._future is not None
            or self._io_future is not None
            or self._editor is not None
            or getattr(self, '_pending_precheck', None) is not None
            or getattr(self, '_pending_role_editor', None) is not None
        )

    def _main_loadout_fresh(self):
        if self.fresh():
            return True
        receipt = getattr(self, '_loadout_receipt', None)
        result = self._payload['result'] if self._payload else self._result
        plan_id = (
            result.get('plan_id') if isinstance(result, dict) else getattr(result, 'plan_id', None)
        )
        revision = self.inventory_revision()
        request_matches = False
        if result is not None:
            try:
                data = self.result_data()
                request_matches = (
                    self.result_request_version(data) == self.request_version()
                    and self._result_scope_generation == self._scope_generation
                    and data['weight_version'] == receipt[3]
                )
            except (ValueError, KeyError, TypeError):
                pass
        return bool(
            request_matches
            and receipt
            and receipt[0] == plan_id
            and receipt[1] == self.profile.currentData()
            and (revision is None or receipt[2] == revision)
            and receipt[3] == self.runtime.weights.active.version
        )

    def refresh_loadout_actions(self):
        if not hasattr(self, 'result_cards'):
            return
        result = self.result_data()
        available = bool(result and not self._loadout_busy() and self._main_loadout_fresh())
        complete = bool(result and all(len(row['item_ids']) == 5 for row in result['roles']))
        self.save_button.setEnabled(available)
        self.protect_button.setEnabled(available and complete)
        self.result_cards.set_actions_enabled(backup=available, save=available)
        if (
            available
            and getattr(self, '_loadout_receipt', None)
            and getattr(self.status, 'full_text', self.status.text()).startswith(
                '库存/权重/约束已变化：'
            )
        ):
            self.status.setText('配装已保存，声骸装备者已更新。')

    def _loadout_arguments(self, key=None, context=None, draft=None):
        if self._loadout_busy():
            raise ValueError('请先结束当前作业。')
        if context is None:
            if not self._main_loadout_fresh():
                raise ValueError('数据或装备要求已变化，请重新配装。')
            if self._payload is not None:
                arguments = dict(payload=self._payload)
                result = self._payload['result']
            elif self._result is not None:
                arguments = dict(
                    result=self._result, records=self._records, snapshot=self._snapshot
                )
                result = self._result.to_dict()
            else:
                raise ValueError('请先直接配装。')
        else:
            payload = context['payload']
            if payload['request']['profile_id'] != self.profile.currentData():
                raise ValueError('档案已变化，请重新选择配装。')
            if draft is None:
                arguments, result = dict(payload=payload), payload['result']
            else:
                arguments = dict(
                    result=draft.result, records=draft.records, snapshot=draft.snapshot
                )
                result = draft.result.to_dict()
            key = (context['character_id'], context['modal_id'])
            arguments.update(saved_role_view=True, view_token=context.get('view_token'))
        if key is not None:
            key = tuple(key)
            if not any((row['character_id'], row['modal_id']) == key for row in result['roles']):
                raise ValueError('所选角色不属于当前配装。')
        arguments.update(
            profile_id=self.profile.currentData(),
            characters=None if key is None else (key[0],),
            _guard_revision=self.runtime.inventory.try_revision(),
            _guard_weight=self.runtime.weights.active.version,
        )
        if arguments['_guard_revision'] is None:
            raise ValueError('库存正在读取，请稍后重试。')
        return arguments, result

    def backup_loadout(self, key=None):
        arguments, _ = self._loadout_arguments(key)
        self._start_plan_job('backup-loadout', arguments)

    def save_loadout(self, key=None):
        arguments, result = self._loadout_arguments(key)
        self._submit_loadout_save(arguments, result)

    def _submit_loadout_save(self, arguments, result):
        wanted = set(arguments['characters'] or (row['character_id'] for row in result['roles']))
        if any(
            len(row['item_ids']) != 5 for row in result['roles'] if row['character_id'] in wanted
        ):
            raise ValueError('保存需要完整五件；缺件方案可先备份。')
        snapshot = self.require_current_snapshot()
        if snapshot.inventory_revision != arguments['_guard_revision']:
            raise ValueError('库存已变化，请刷新后重新保存。')
        if 'result' in arguments and self._saved_view:
            if snapshot.inventory_revision != arguments['result'].inventory_revision:
                raise ValueError('库存已变化，请重新打开替换对比。')
        current = [row for row in snapshot.current if row['character_id'] in wanted]
        arguments.update(
            expected_revision=snapshot.inventory_revision,
            expected_loadout_revision=snapshot.revision,
            replace_plans={row['character_id']: row['plan_id'] for row in current},
        )
        # Explicit save is the local equip action; history-only backup is separate.
        # Repository validates both revisions and ownership atomically before commit.
        self._start_plan_job('save-loadout', arguments)

    def open_saved_role(self, plan_id, character_id, modal_id, view_token):
        self._start_plan_job(
            'load',
            dict(
                plan_id=plan_id,
                profile_id=self.profile.currentData(),
                character_id=character_id,
                modal_id=modal_id,
                view_token=view_token,
                saved_role_view=True,
            ),
        )

    def save_saved_role(self, source_context, draft=None):
        arguments, result = self._loadout_arguments(context=source_context, draft=draft)
        self._submit_loadout_save(arguments, result)

    def backup_saved_role(self, source_context, draft=None):
        if (
            source_context['mode'] != 'history'
            or source_context['view_token'] != self.saved_workspace.view_token
        ):
            raise ValueError('备份显示已变化，请重新选择')
        arguments, _ = self._loadout_arguments(context=source_context, draft=draft)
        self._start_plan_job('backup-loadout', arguments)

    def saved_role_operation(self, kind, context, item_ids=()):
        if self._loadout_busy() or not self._saved_view:
            raise ValueError('请先结束作业，再选择已保存配装')
        if context['view_token'] != self.saved_workspace.view_token:
            raise ValueError('当前显示的角色配装已变化，请重新选择')
        if kind == 'remove-backup':
            import hashlib
            from echo_workshop.core.models import canonical_json

            if context['mode'] != 'history':
                raise ValueError('请在备份配装中移除所选快照')
            revision = self.runtime.inventory.try_revision()
            if revision is None:
                raise ValueError('声骸库正在读取，请稍后重试')
            arguments = dict(
                profile_id=self.profile.currentData(),
                plan_id=context['payload']['plan_id'],
                character_id=context['character_id'],
                modal_id=context['modal_id'],
                view_token=context['view_token'],
                saved_role_view=True,
                expected_hash=hashlib.sha256(
                    canonical_json(context['payload']).encode('utf-8')
                ).hexdigest(),
                _guard_revision=revision,
                _guard_weight=self.runtime.weights.active.version,
            )
            self.queue_current_action(
                kind,
                arguments,
                '仅从备份列表移除此角色的快照；不改变当前配装、声骸占用或其他角色。原始快照保留供历史引用。',
            )
            return
        snapshot = self.require_current_snapshot()
        arguments = dict(
            profile_id=self.profile.currentData(),
            character_id=context['character_id'],
            modal_id=context['modal_id'],
            payload=context['payload'],
            plan_id=context['payload']['plan_id'],
            expected_revision=snapshot.inventory_revision,
            expected_loadout_revision=snapshot.revision,
            view_token=context['view_token'],
            saved_role_view=True,
            _guard_revision=snapshot.inventory_revision,
            _guard_weight=self.runtime.weights.active.version,
        )
        if kind == 'release-items':
            if context['mode'] != 'current' or not item_ids:
                raise ValueError('请选择当前配装中需要解除保护的声骸')
            arguments['item_ids'] = tuple(item_ids)
            message = f'解除所选 {len(item_ids)} 件声骸的保护，其它声骸保持不变。'
        else:
            raise ValueError('未知角色配装操作')
        self.queue_current_action(kind, arguments, message)

    def _dispatch_loadout_job(self, value):
        arguments = self._io_arguments
        saved_view = arguments.get('saved_role_view')
        token = arguments.get('view_token')
        if value.kind == 'verify-loadouts':
            self.saved_workspace.publish_verification(value.loadouts, arguments)
            return True
        if value.kind == 'remove-backup':
            self._history_queued = True
            self.saved_workspace._signature = self.saved_workspace._detail_signature = None
            self.saved_workspace._details.pop(
                (self.profile.currentData(), arguments['plan_id']), None
            )
            self.saved_workspace._drafts.pop((token[1], token[2], token[3], token[4]), None)
            self.show_loadout_receipt('已移除此角色备份，当前配装及声骸占用未改变。')
            return True
        if value.kind == 'release-items':
            if value.loadouts is not None:
                self.publish_current_loadouts(value.loadouts)
            self._history_queued = True
            self.saved_workspace.publish_role_operation(
                value.kind,
                token,
                len(arguments.get('item_ids', ())),
                readback=value.loadouts is not None,
            )
            return True
        if saved_view and value.kind == 'load':
            self.publish_current_loadouts(value.loadouts)
            self.saved_workspace.publish_detail(value, token)
            return True
        if value.kind in ('replacement-candidates', 'preview-replacement'):
            method = (
                self.saved_workspace.publish_candidates
                if value.kind == 'replacement-candidates'
                else self.saved_workspace.publish_preview
            )
            method(value.payload, token)
            return True
        if value.kind not in ('backup-loadout', 'save-loadout'):
            return False
        current_committed = getattr(value, 'current_committed', None)
        if value.loadouts is not None:
            self.publish_current_loadouts(value.loadouts)
        elif value.kind == 'save-loadout' and current_committed is not False:
            self._current_loadouts = None
        payload = value.payload
        plan_id = payload['plan_id']
        if self.saved.findData(plan_id) < 0:
            self.saved.addItem(payload['created_at'] + ' · ' + plan_id[:8], plan_id)
        self._history_queued = True
        if (
            value.kind == 'save-loadout'
            and current_committed is not False
            and not saved_view
            and value.loadouts is not None
            and value.loadouts.inventory_revision == arguments['expected_revision'] + 1
            and value.loadouts.revision == arguments['expected_loadout_revision'] + 1
        ):
            source = getattr(value, 'source_payload', None)
            source_id = (
                source['plan_id']
                if source
                else (self._payload['plan_id'] if self._payload else self._result.plan_id)
            )
            self._loadout_receipt = (
                source_id,
                self.profile.currentData(),
                value.loadouts.inventory_revision,
                arguments['_guard_weight'],
            )
        if saved_view and current_committed is not False:
            publish = (
                self.saved_workspace.publish_backup
                if value.kind == 'backup-loadout'
                else self.saved_workspace.publish_saved
            )
            publish(
                plan_id,
                arguments['characters'][0],
                payload['result']['roles'][0]['modal_id'],
                token,
            )
        count = len(payload['result']['roles'])
        self.status.setText(
            value.notice
            if current_committed is False
            else f'已备份 {count} 个角色配装，未更改装备者。'
            if value.kind == 'backup-loadout'
            else f'已保存 {count} 个角色配装并更新声骸装备者。'
        )
        rejected = current_committed is False
        text = (
            value.notice
            if rejected
            else f'已备份 {count} 个角色配装'
            if value.kind == 'backup-loadout'
            else f'已保存 {count} 个角色配装，工坊装备者已更新。'
        )
        if value.kind == 'save-loadout' and not rejected and value.loadouts is None:
            text += '正在刷新，请勿重复保存。'
        self.show_loadout_receipt(
            text, warning=rejected or (value.kind == 'save-loadout' and value.loadouts is None)
        )
        return True
