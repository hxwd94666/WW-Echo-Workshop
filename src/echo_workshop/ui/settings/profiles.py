# 管理档案新建删除重命名和迁移。
"""Compact account management; selected account is separate from active account."""

import logging
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMessageBox, QLineEdit
from qfluentwidgets import CaptionLabel, PushButton
from echo_workshop.ui.widgets.inputs import QComboBox
from echo_workshop.ui.widgets.state import update_text, update_tooltip


class ProfileManagementController:
    def build_profile_management(self, section):
        from echo_workshop.application.profiles import ProfileSettingsService

        self.profile_settings = ProfileSettingsService(
            self.task.config, self.runtime.inventory, self.runtime.data_dir
        )
        self._profile_job = None
        self._known_profiles = {}
        self._profile_rename_target = None
        self.profile_manage_box = QComboBox(section.content)
        self.profile_manage_box.setEditable(True)
        self.profile_manage_box.setInsertPolicy(QComboBox.NoInsert)
        self.profile_manage_box.setMinimumHeight(32)
        self.profile_manage_box.setToolTip('选择待管理档案；支持拼音搜索，不会切换当前使用的档案。')
        row = QHBoxLayout()
        row.addWidget(self.profile_manage_box, 1)
        self.delete_profile_button = PushButton('删除档案', section.content)
        self.delete_profile_button.clicked.connect(self.delete_managed_profile)
        row.addWidget(self.delete_profile_button)
        section.content_layout.addLayout(row)
        self.current_profile_label = CaptionLabel(section.content)
        section.content_layout.addWidget(self.current_profile_label)
        row = QHBoxLayout()
        self.profile_rename_edit = QLineEdit(section.content)
        self.profile_rename_edit.setMaxLength(80)
        self.profile_rename_edit.setPlaceholderText('所选档案名称')
        self.rename_profile_button = PushButton('重命名', section.content)
        self.rename_profile_button.clicked.connect(self.rename_profile)
        row.addWidget(self.profile_rename_edit, 1)
        row.addWidget(self.rename_profile_button)
        section.content_layout.addLayout(row)
        row = QHBoxLayout()
        self.profile_name = QLineEdit(section.content)
        self.profile_name.setMaxLength(80)
        self.profile_name.setPlaceholderText('新档案名称')
        self.create_profile_button = PushButton('新建档案', section.content)
        self.create_profile_button.clicked.connect(self.create_profile)
        row.addWidget(self.profile_name, 1)
        row.addWidget(self.create_profile_button)
        section.content_layout.addLayout(row)
        row = QHBoxLayout()
        self.export_profile_button = PushButton('导出此档案', section.content)
        self.import_profile_button = PushButton('导入档案', section.content)
        self.export_profile_button.clicked.connect(self.export_managed_profile)
        self.import_profile_button.clicked.connect(self.import_managed_profile)
        row.addWidget(self.export_profile_button)
        row.addWidget(self.import_profile_button)
        row.addStretch(1)
        section.content_layout.addLayout(row)
        self.profile_status = CaptionLabel(section.content)
        self.profile_status.setWordWrap(True)
        old = self.proxy.inventory_window.backup_label
        old.hide()
        section.content_layout.addWidget(self.profile_status)
        self.profile_status.hide()
        section.content_layout.addWidget(self.proxy.inventory_window.cancel_backup_button)
        self.proxy.inventory_window.cancel_backup_button.hide()
        self.data_location_hint = CaptionLabel('数据保存在本机', section.content)
        self.data_location_hint.setWordWrap(True)
        self.data_location_hint.setToolTip(
            '插件更新会保留数据；重装或更换运行目录前请导出档案。\n' + str(self.runtime.data_dir)
        )
        section.content_layout.addWidget(self.data_location_hint)
        self.profile_manage_box.currentIndexChanged.connect(self._sync_profile_editor)
        self.proxy.inventory_window.profile_box.currentIndexChanged.connect(
            self._sync_profile_editor
        )
        self._consume_delete_receipts()
        self._consume_profile_receipts()
        # Startup receipts can change the active ID even when the repository
        # catalog was already loaded. Project it onto the shared header too.
        self._resync_profile_pages(self.proxy.inventory_window.profile_box.currentData())

    def reload_managed_profiles(self, preferred=None, *, preserve_draft=False):
        selected = preferred or self.profile_manage_box.currentData()
        draft = self.profile_rename_edit.text()
        draft_identity = self._profile_rename_target
        has_draft = preserve_draft and draft != getattr(self, '_profile_rename_baseline', '')
        profiles = self.runtime.inventory.profiles()
        box = self.profile_manage_box
        blocked = box.blockSignals(True)
        box.clear()
        for key, name in profiles.items():
            box.addItem(name, userData=key)
        index = box.findData(selected)
        box.setCurrentIndex(index if index >= 0 else 0)
        box.blockSignals(blocked)
        self._known_profiles = profiles
        self._managed_profiles_revision = self.runtime.inventory.try_revision()
        self._sync_managed_editor(force=True)
        if has_draft and self.profile_manage_box.currentData() == draft_identity:
            self.profile_rename_edit.setText(draft)

    def _managed_identity(self):
        box = self.profile_manage_box
        if box.currentIndex() < 0 or box.currentText() != box.itemText(box.currentIndex()):
            raise ValueError('请先从列表选择一个档案。')
        key = box.currentData()
        if key not in self._known_profiles:
            raise ValueError('档案已变化，请重新选择。')
        return key

    def _sync_managed_editor(self, *_, force=False, revision=...):
        if not hasattr(self, 'profile_manage_box'):
            return
        combo = self.profile_manage_box
        identity = combo.currentData()
        name = self._known_profiles.get(identity, '')
        active = self.proxy.inventory_window.profile_box.currentText()
        update_text(self.current_profile_label, '管理：' + name + ' · 当前：' + active)
        update_tooltip(
            self.current_profile_label, '此处选择待管理档案；在工坊顶部切换当前使用的档案。'
        )
        previous = getattr(self, '_profile_rename_baseline', '')
        if (
            force
            or identity != self._profile_rename_target
            or self.profile_rename_edit.text() == previous
        ):
            self._profile_rename_target = identity
            self._profile_rename_baseline = name
            update_text(self.profile_rename_edit, name)
            self._profile_rename_revision = (
                self.runtime.inventory.try_revision() if revision is ... else revision
            )

    def _refresh_managed_controls(self, reason, *, revision=...):
        if not hasattr(self, 'profile_manage_box'):
            return
        if revision is ...:
            revision = self.runtime.inventory.try_revision()
        if (
            not reason
            and revision is not None
            and revision != getattr(self, '_managed_profiles_revision', None)
        ):
            profiles = self.runtime.inventory.profiles()
            if profiles != self._known_profiles:
                self._resync_profile_pages(
                    self.profile_manage_box.currentData(), preserve_draft=True
                )
            else:
                # Inventory changes alone must not reset an in-progress rename.
                self._managed_profiles_revision = revision
        for control in (
            self.profile_manage_box,
            self.export_profile_button,
            self.import_profile_button,
        ):
            control.setEnabled(not reason)
            help_text = (
                '选择待管理档案；支持拼音搜索，不会切换当前使用档案。'
                if control is self.profile_manage_box
                else '导出声骸、全部配装及此档案的匹配配置，不含截图和全局设置。'
                if control is self.export_profile_button
                else '导入按档案 ID 合并，已有数据保留；同名的不同档案不会互相覆盖。'
            )
            update_tooltip(control, reason or help_text)
        last = len(self._known_profiles) <= 1
        self.delete_profile_button.setEnabled(not reason and not last)
        update_tooltip(
            self.delete_profile_button,
            reason
            or (
                '至少保留一个档案；可先新建其他档案。'
                if last
                else '删除前自动留存无截图档案包，可重新导入。'
            ),
        )

    def _profile_action_ready(self):
        reason = self.profile_mutation_error()
        if reason:
            self.feedback.setText(reason.replace('管理档案', '管理档案数据'))
            self.feedback.show()
            return False
        return True

    def _profile_action_error(self, error):
        self._profile_message('操作未完成：' + str(error))

    def _profile_message(self, text):
        self._profile_notice = (self.runtime.backups.status.job_id, text)
        self.profile_status.setText(text)
        self.profile_status.show()

    def _rename_managed_profile(self):
        if not self._profile_action_ready():
            return
        try:
            identity = self._managed_identity()
            if identity != self._profile_rename_target:
                raise ValueError('请重新填写所选档案名称。')
            revision = self._profile_rename_revision
            if revision is None:
                raise ValueError('声骸库正在读取，请稍后保存名称。')
            name = self.profile_rename_edit.text().strip()
            changed = self.runtime.inventory.rename_profile(
                identity, name, expected_revision=revision
            )
            boxes = [self.profile_manage_box]
            for page in self._owned_pages.values():
                boxes.extend(getattr(page, attr, None) for attr in ('profile_box', 'profile'))
            for box in boxes:
                if box is None:
                    continue
                index = box.findData(identity)
                if index >= 0:
                    blocked = box.blockSignals(True)
                    box.setItemText(index, name)
                    box.blockSignals(blocked)
            self._known_profiles[identity] = name
            self._sync_managed_editor(force=True)
            self._profile_message('名称已保存，声骸和配装不变。' if changed else '名称未变化。')
        except (ValueError, OSError) as error:
            self._profile_action_error(error)
            self._profile_rename_revision = self.runtime.inventory.try_revision()

    def export_managed_profile(self):
        if not self._profile_action_ready():
            return
        try:
            identity = self._managed_identity()
            path, _ = QFileDialog.getSaveFileName(
                self, '导出所选档案（不含截图）', 'echo-profile.zip', '档案数据 (*.zip)'
            )
            if not path or not self._profile_action_ready():
                return
            self._start_profile_job('profile-export', path, dict(profile_id=identity))
        except (ValueError, OSError) as error:
            self._profile_action_error(error)

    def import_managed_profile(self):
        if not self._profile_action_ready():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, '导入档案（兼容旧声骸库和完整备份）', '', '档案数据 (*.zip)'
        )
        if not path or not self._profile_action_ready():
            return
        try:
            self._start_profile_job('profile-preview', path, {})
        except (ValueError, OSError) as error:
            self._profile_action_error(error)

    def delete_managed_profile(self):
        if not self._profile_action_ready():
            return
        try:
            identity = self._managed_identity()
            if len(self.runtime.inventory.profiles()) <= 1:
                raise ValueError('请至少保留一个档案。')
            revision = self.runtime.inventory.try_revision()
            if revision is None:
                raise ValueError('声骸库正在读取，请稍后删除。')
            counts = self.runtime.inventory.profile_counts(identity, expected_revision=revision)
            answer = QMessageBox.question(
                self,
                '删除档案',
                f'删除“{self._known_profiles[identity]}”及其 {counts["items"]} 件声骸、待核对与当前配装？\n'
                '其他档案不变。删除前会自动保存无截图档案包，可用“导入档案”找回。',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes or not self._profile_action_ready():
                return
            self._start_profile_job(
                'profile-delete',
                self.runtime.base_data_dir / 'deleted-profiles',
                dict(profile_id=identity, expected_revision=revision),
            )
        except (ValueError, OSError) as error:
            self._profile_action_error(error)

    def _start_profile_job(self, kind, path, request):
        job_id = self.runtime.backups.start(kind, path, workspace_request=request)
        self._profile_notice = None
        self._profile_job = (job_id, kind, str(path), request)
        self.refresh_profile_guard()

    def _apply_account_settings(self, settings=(), *, removed=None, active=None):
        return self.profile_settings.apply(
            settings, removed=removed, active=active, cancel_event=self.runtime.stop_event
        )

    def _repair_active_profile(self, profiles, preferred=None):
        return self.profile_settings.repair_active(
            profiles,
            preferred=preferred,
            visible=self.proxy.inventory_window.profile_box.currentData(),
            cancel_event=self.runtime.stop_event,
        )

    def _consume_profile_receipts(self):
        report = self.profile_settings.consume_import_receipts(cancel_event=self.runtime.stop_event)
        for message in report.deferred:
            self._profile_message(message)
        return report

    def _resync_profile_pages(self, selected, *, activate=False, preserve_draft=False):
        profiles = self.runtime.inventory.profiles()
        if not profiles:
            raise ValueError('请至少保留一个档案。')
        current = selected if activate else self.task.config.get('InventoryProfileID')
        inventory = self.proxy.inventory_window
        if current not in profiles:
            visible = inventory.profile_box.currentData()
            current = visible if visible in profiles else next(iter(profiles))
        config_error = None
        try:
            self._repair_active_profile(profiles, current)
        except (ValueError, OSError, KeyError) as error:
            # The database catalog is authoritative even if Config cannot be
            # published. Keep the config/receipt intact and show the real IDs.
            config_error = error
        for page in self._owned_pages.values():
            box = getattr(page, 'profile_box', getattr(page, 'profile', None))
            if box is None:
                continue
            previous = box.currentData()
            target = (
                current if page is inventory else (previous if previous in profiles else current)
            )
            blocked = box.blockSignals(True)
            box.clear()
            for key, name in profiles.items():
                box.addItem(name, userData=key)
            box.setCurrentIndex(box.findData(target))
            box.blockSignals(blocked)
            if previous != target:
                if page is inventory:
                    # Do not call profile_changed: it writes Config again and
                    # can abort catalog refresh after a committed DB deletion.
                    from PySide6.QtCore import QModelIndex

                    page.table.clearSelection()
                    page.table.setCurrentIndex(QModelIndex())
                    page._accepted_profile = target
                    page.refresh(force=True)
                else:
                    page.load_profile()
        self.reload_managed_profiles(
            selected if selected in profiles else current, preserve_draft=preserve_draft
        )
        logging.getLogger('ok.EchoInventoryProfiles').info(
            'profile catalog synchronized count=%s active=%s config_pending=%s',
            len(profiles),
            current,
            config_error is not None,
        )
        if config_error is not None:
            self._profile_message('档案列表已同步；当前档案设置保存待重试：' + str(config_error))

    def _consume_delete_receipts(self):
        report = self.profile_settings.consume_delete_receipts(cancel_event=self.runtime.stop_event)
        for message in report.deferred:
            self._profile_message(message)
        return report

    def poll_profile_jobs(self):
        job = getattr(self, '_profile_job', None)
        if job is None:
            return
        state = self.runtime.backups.status
        if state.busy:
            return
        if state.job_id != job[0]:
            # Another legacy job replaced the single status slot. Reconcile
            # committed receipts, never replay an uncertain mutation.
            self._profile_job = None
            self._consume_delete_receipts()
            self._consume_profile_receipts()
            self._resync_profile_pages(self.profile_manage_box.currentData(), preserve_draft=True)
            logging.getLogger('ok.EchoInventoryProfiles').warning(
                'profile job status superseded kind=%s; reconciled catalog and receipts', job[1]
            )
            return
        self._profile_job = None
        if state.state != 'completed':
            return
        try:
            if job[1] == 'profile-preview':
                preview = state.payload
                names = '、'.join(name for _, name in preview.profiles)
                warning = '\n' + '\n'.join(preview.warnings) if preview.warnings else ''
                answer = QMessageBox.question(
                    self,
                    '确认导入档案',
                    f'导入：{names}\n按档案 ID 合并，已有声骸、方案进度和配置保留，不按同名覆盖。'
                    '\n导入后打开该档案；全局快捷键与自定义权重不会被替换。' + warning,
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer == QMessageBox.Yes and self._profile_action_ready():
                    revision = self.runtime.inventory.try_revision()
                    if revision is None:
                        raise ValueError('声骸库正在读取，请稍后导入。')
                    self._start_profile_job(
                        'profile-import',
                        job[2],
                        dict(
                            expected_revision=revision,
                            expected_archive_sha256=preview.source_sha256,
                        ),
                    )
                else:
                    self._profile_message('已取消导入，原数据不变。')
            elif job[1] == 'profile-import':
                report = state.payload
                target = report.profiles[0][0]
                try:
                    self._apply_account_settings(report.settings_json, active=target)
                finally:
                    self._resync_profile_pages(target, activate=True)
                self._consume_profile_receipts()
            elif job[1] == 'profile-delete':
                removed = job[3]['profile_id']
                try:
                    self._apply_account_settings(removed=removed)
                finally:
                    self._resync_profile_pages(self.profile_manage_box.currentData())
                self._consume_delete_receipts()
        except (ValueError, OSError, KeyError) as error:
            self._profile_message(
                (
                    '操作未完成：'
                    if job[1] == 'profile-preview'
                    else '档案数据已处理；设置或界面更新待重试：'
                )
                + str(error)
                + '。原导出文件保留。'
            )
            logging.getLogger('ok.EchoInventoryProfiles').warning(
                'profile job GUI follow-up deferred kind=%s error=%s', job[1], type(error).__name__
            )
