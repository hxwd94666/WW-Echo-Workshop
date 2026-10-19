# 管理库存动作编辑和选择状态。
"""Main-thread collection/review UI. Closing it does not stop active collection."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMessageBox, QPushButton

from echo_workshop.application.inventory import InventoryReader
from echo_workshop.application.view_state import capture_hint
from echo_workshop.ui.inventory.evidence import open_evidence
from echo_workshop.ui.inventory.editor import RecordEditor
from echo_workshop.ui.inventory.review_fields import ReviewChoices


class InventoryContent:
    def lifecycle_busy(self):
        reader = getattr(self, '_reader', None)
        return reader is not None and reader.status.state == 'running'

    def lifecycle_draft(self):
        return getattr(self, '_editor', None) is not None

    def request_stop(self):
        self.stop_worker()

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task, self.runtime = task, task.runtime
        self._rows = []
        self._seen_revision = None
        self._seen_backup = 0
        self._action_buttons = []
        self._mutation_buttons = []
        self._reader = InventoryReader(
            self.runtime.inventory, self.runtime.stop_event, queries=self.runtime.inventory_queries
        )
        self._requested_key = None
        self._processed_generation = -1
        self.build_inventory_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.profile_box.currentIndexChanged.connect(self.profile_changed)
        self.filter_box.currentIndexChanged.connect(lambda: self.refresh(force=True))
        self.table.selectionModel().selectionChanged.connect(self.show_details)
        self.read_timer = QTimer(self)
        self.read_timer.setInterval(40)
        self.read_timer.timeout.connect(self.refresh)
        self.read_timer.start()
        self.reload_profiles()
        self.refresh(force=True)

    def _button(self, layout, text, callback):
        button = getattr(self, '_button_type', QPushButton)(text)
        button.clicked.connect(lambda: self.action(callback))
        layout.addWidget(button)
        self._action_buttons.append(button)
        return button

    def action(self, callback):
        if self.task._destroyed or self.runtime._closed:
            return
        if self.runtime.backups.status.busy:
            self.backup_label.setText('已有后台备份作业；请等待完成或取消，再进行编辑')
            return
        try:
            callback()
            self.refresh(force=True)
        except (ValueError, KeyError, OSError) as error:
            QMessageBox.warning(self, '操作未完成，原记录保留', str(error))

    def reload_profiles(self):
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        for key, name in self.runtime.inventory.profiles().items():
            self.profile_box.addItem(name, userData=key)
        saved = self.task.config.get('InventoryProfileID', '')
        index = self.profile_box.findData(saved)
        self.profile_box.setCurrentIndex(index)
        self.profile_box.blockSignals(False)

    def profile_changed(self):
        self.runtime.capture.stop()
        self.task.config['InventoryProfileID'] = self.profile_box.currentData() or ''
        self.refresh(force=True)

    def new_profile(self, *, name=None):
        if name is None:
            name, accepted = QInputDialog.getText(
                self, '新建本地档案', '自行命名；不代表 OCR 已识别账号身份：'
            )
        else:
            accepted = True
        if accepted and name.strip():
            key = self.runtime.inventory.create_profile(name)
            self.task.config['InventoryProfileID'] = key
            self.runtime.capture.stop()
            self.reload_profiles()

    def start_capture(self):
        if (
            not getattr(self, '_intake_ui', False)
            or self.task.config.get('IntakeMode', 'manual') == 'manual'
        ):
            from echo_workshop.application.hotkeys import parse_bindings

            if 'capture' not in parse_bindings(self.task.config.get('CaptureHotkeysJSON', '')):
                prompt = QMessageBox(self)
                prompt.setWindowTitle('先设置收录快捷键')
                prompt.setText(
                    '手动收录仅由快捷键触发。请先设置“收录当前声骸”快捷键，再点击开始收录。'
                )
                settings = prompt.addButton('前往设置', QMessageBox.AcceptRole)
                prompt.addButton('返回', QMessageBox.RejectRole)
                prompt.exec()
                if prompt.clickedButton() is settings:
                    self.task.open_shortcuts(self)
                return
        if not self.task._capture_ready():
            self.capture_help.setText(
                '先在 OKWW 连接鸣潮画面，再开始收录。'
                if getattr(self, '_intake_ui', False)
                else '先在 OKWW 选择鸣潮窗口并启动捕获、取消宿主暂停。保持游戏画面可见，再打开一件声骸详情。已有库存保留。'
            )
            return
        if getattr(self, '_intake_ui', False):
            if self.task.config.get('IntakeMode', 'manual') == 'automatic':
                self.task.start_scan(self.profile_box.currentData())
            else:
                self.task.arm_capture(self.profile_box.currentData())
        else:
            self.task.arm_capture(self.profile_box.currentData())
        self.refresh()

    def pause_capture(self):
        if self.runtime.capture.status.mode == 'automatic':
            self.task.pause_scan()
            self.refresh()
            return
        if self.runtime.capture.status.state == 'paused':
            if not self.task._capture_ready():
                self.capture_help.setText('画面尚未连接，请先在 OKWW 恢复捕获，再继续收录。')
                return
            self.runtime.capture.resume()
        else:
            self.runtime.capture.pause()
        self.refresh()

    def finish_capture(self):
        if self.runtime.capture.status.mode == 'automatic':
            self.task.stop_scan()
        else:
            self.runtime.capture.stop()
        status = self.runtime.capture.status
        self.filter_box.setCurrentIndex(1)
        self._finished_capture_hint = (
            status,
            f'收录已结束：本次新增 {status.added} 件、'
            f'本次需核对 {status.needs_review} 件。已收录记录保留。',
        )
        self.refresh(force=True)

    def _intake_mode_changed(self):
        if self.runtime.capture.status.state == 'stopped':
            self.task.config['IntakeMode'] = self.intake_mode.currentData()
        self.refresh_capture_controls()

    def _scan_level_changed(self):
        if self.runtime.capture.status.state == 'stopped':
            self.task.config['ScanMinimumLevel'] = self.scan_level.currentData()
        self.refresh_capture_controls()

    def refresh_capture_controls(self):
        """Single owner for shared header state, including queued/finished scans."""
        status = self.runtime.capture.status
        busy = status.state != 'stopped'
        hint, _ = capture_hint(status, self.task._capture_ready())
        finished = getattr(self, '_finished_capture_hint', None)
        if (
            not getattr(self, '_intake_ui', False)
            and finished is not None
            and status == finished[0]
            and status.profile_id == (self.profile_box.currentData() or '')
        ):
            # The shared refresh must not erase the legacy stop result with an
            # idle preflight hint (including an empty collection).
            hint = finished[1]
        self.start_button.setVisible(not busy)
        self.pause_button.setVisible(status.state in ('active', 'paused'))
        self.pause_button.setText('继续收录' if status.state == 'paused' else '暂停收录')
        self.stop_button.setVisible(busy)
        self.profile_box.setEnabled(not busy and not self.runtime.backups.status.busy)
        if getattr(self, '_intake_ui', False):
            mode = self.task.config.get('IntakeMode', 'manual')
            mode = mode if mode in ('manual', 'automatic') else 'manual'
            level = str(self.task.config.get('ScanMinimumLevel', '25'))
            if self.scan_level.findData(level) < 0:
                level = '25'
            for control, value in ((self.intake_mode, mode), (self.scan_level, level)):
                if control.currentData() != value:
                    control.blockSignals(True)
                    control.setCurrentIndex(control.findData(value))
                    control.blockSignals(False)
                control.setEnabled(not busy and not self.runtime.backups.status.busy)
            self.scan_level.setVisible(mode == 'automatic')
            self.start_button.setText('开始扫描' if mode == 'automatic' else '开始收录')
            same_scan_profile = self.runtime.scan.profile_id == (
                self.profile_box.currentData() or ''
            )
            if status.mode == 'automatic' and (busy or (mode == 'automatic' and same_scan_profile)):
                scan = self.runtime.scan.status
                self.pause_button.setVisible(busy and scan.state in ('running', 'paused'))
                self.pause_button.setText('继续扫描' if scan.state == 'paused' else '暂停扫描')
                self.stop_button.setText('停止扫描')
                hint = (
                    f'{scan.message} · 已扫描 {scan.captured} · 待解析 {scan.backlog} · 新增 {scan.added} · 疑似关联 {scan.pending}'
                    + (f' · 失败 {scan.failed}' if scan.failed else '')
                )
                if self.runtime.scan.failed_positions:
                    positions = '、'.join(
                        f'第{row + 1}行第{column + 1}列'
                        for row, column in self.runtime.scan.failed_positions
                    )
                    hint += ' · 读取失败位置：' + positions
            else:
                self.stop_button.setText('停止收录')
            if not busy and (
                status.mode != 'automatic' or mode != 'automatic' or not same_scan_profile
            ):
                hint = (
                    '打开声骸背包并回到顶部，点击开始后切回游戏。扫描期间请勿操作鼠标；切出游戏会暂停。'
                    if mode == 'automatic'
                    else '点击开始收录，再回到游戏按收录快捷键；每次按键只收录当前一件。'
                )
                if mode == 'manual' and status.mode == 'manual':
                    hint = status.message
        self.capture_help.setText(hint)
        self.status_label.setText(hint)
        return hint

    def selected(self):
        identity = self.table.currentIndex().data(Qt.UserRole)
        if hasattr(self.table, 'selected_ids'):
            identities = self.table.selected_ids()
            if identity not in identities:
                identity = next(iter(identities), None)
        index = self.inventory_model.row_for_id(identity)
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def cancel_read(self):
        self._reader.cancel()
        self.refresh()

    def stop_worker(self):
        # Host unload can call from its executor, before a queued Qt close.
        dialog = getattr(self, '_quick_score_dialog', None)
        if dialog is not None:
            dialog.stop_worker()
        self.table.proxy.close_job()
        self._reader.close()

    def _mutation_revision(self):
        revision = self.runtime.inventory.try_revision()
        expected = self._seen_revision
        if (
            revision is None
            or expected
            != (self.profile_box.currentData() or '', revision, self.filter_box.currentIndex())
            or self._reader.status.state == 'running'
        ):
            self.refresh()
            raise ValueError('库存视图已变化或正在读取；请等最新视图后重新选择，原记录保留')
        return revision

    def _clear_ready(self):
        return (
            not self.runtime.backups.status.busy
            and self.runtime.capture.status.state == 'stopped'
            and self.runtime.scan.status.state not in ('preparing', 'running', 'paused', 'draining')
        )

    def clear_library(self):
        if not self._clear_ready():
            raise ValueError('请先停止收录或扫描，并等待备份完成，再清空声骸库。')
        profile = self.profile_box.currentData() or ''
        revision = self._mutation_revision()
        counts = self.runtime.inventory.profile_counts(profile, expected_revision=revision)
        if not any(counts.values()):
            return
        name = self.profile_box.currentText()
        answer = QMessageBox.question(
            self,
            '清空声骸库',
            f'清空“{name}”的 {counts["items"]} 件声骸和 {counts["pending"]} 条待判定记录？\n'
            '包括筛选隐藏的记录，并解除当前配装占用。其他档案、历史方案及原图保留；不会改动游戏背包。',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if (
            not self._clear_ready()
            or profile != self.profile_box.currentData()
            or profile != self.task.config.get('InventoryProfileID', '')
        ):
            raise ValueError('档案或收录状态已变化，请重试。')
        import sqlite3

        try:
            self.runtime.inventory.clear_profile(profile, expected_revision=revision)
        except sqlite3.Error as error:
            raise ValueError('清空未完成，原记录保留；请稍后重试。') from error
        self._finished_capture_hint = None
        self.refresh(force=True)

    def _refresh_pending_summary(self):
        rows = getattr(self, '_rows', ())
        safe = issue = linked = 0
        for record, kind, _ in rows:
            if kind != 'item':
                linked += 1
                continue
            if record.review == 'confirmed':
                continue
            quality = self.inventory_model.quality(record)
            if quality.issues:
                issue += 1
            else:
                safe += 1
        if not hasattr(self, 'pending_summary'):
            return
        self.pending_summary.setText(
            f'待核对：可安全确认 {safe} 件 · 需修正 {issue} 件 · 重复/升级 {linked} 件'
            if safe or issue or linked
            else ''
        )
        self.pending_summary.setVisible(bool(safe or issue or linked))
        self.confirm_safe_button.setText(f'确认无问题项（{safe}）')
        self.confirm_safe_button.setVisible(bool(safe))

    def refresh(self, *, force=False):
        try:
            self._refresh(force=force)
        except ValueError:
            if not self.runtime._closed:
                raise
            self.timer.stop()
            self.status_label.setText('插件已卸载；库存与截图仍保留在用户数据目录')

    def _refresh(self, *, force=False):
        if self.runtime._closed or self.task._destroyed:
            self.timer.stop()
            self.read_timer.stop()
            self.status_label.setText('插件已卸载；库存与截图仍保留在用户数据目录')
            return
        backup = self.runtime.backups.status
        count = f' · 当前阶段 {backup.done}/{backup.total}' if backup.total else ''
        from echo_workshop.ui.settings.help import job_text

        self.backup_label.setText(f'备份：{job_text(backup.state)} · {backup.message}{count}')
        if backup.state == 'idle':
            self.backup_label.setText(
                '备份不含图片；可在其他设备恢复。完整备份另含配装方案和插件设置。'
            )
        self.backup_label.setToolTip(backup.message)
        for button in self._action_buttons:
            button.setEnabled(not backup.busy)
        self.cancel_backup_button.setEnabled(backup.busy)
        self.filter_box.setEnabled(not backup.busy)
        if backup.busy:
            if self._reader.status.state == 'running':
                self._reader.cancel()
            self._requested_key = None
            self.cancel_read_button.setEnabled(False)
            self.profile_box.setEnabled(False)
            self.status_label.setText(
                '后台备份进行中；此窗口编辑暂缓，关闭窗口请求取消并等待未提交作业退出'
            )
            return  # Avoid waiting on the DB mutex while the worker commits.
        if backup.job_id != self._seen_backup:
            self._seen_backup = backup.job_id
            if backup.state == 'completed' and backup.kind == 'import':
                self.reload_profiles()
                force = True
        self.refresh_capture_controls()
        profile = self.profile_box.currentData() or ''
        revision = self.runtime.inventory.try_revision()
        if revision is None:
            self.reading_label.setText('库存事务忙；后台读取等待，旧视图不作为最新')
            for button in self._mutation_buttons:
                button.setEnabled(False)
            self.cancel_read_button.setEnabled(self._reader.status.state == 'running')
            return
        key = (profile, revision, self.filter_box.currentIndex())
        if force or key != self._requested_key:
            self._reader.request(profile or '', key[2])
            self.read_timer.start()
            self._requested_key = key
        state = self._reader.status
        if state.state == 'ready' and state.generation != self._processed_generation:
            self._processed_generation = state.generation
            result = state.result
            if (result.profile_id, result.revision, result.mode) == key:
                selected_ids = (
                    self.table.selected_ids() if hasattr(self.table, 'selected_ids') else None
                )
                previous = self.selected()
                previous_id = previous[0].item_id if previous else None
                changed = self.inventory_model.reset_rows(
                    result.rows, claims=result.claims, quality=result.quality
                )
                self._rows = self.inventory_model.rows
                self._refresh_pending_summary()
                index = self.inventory_model.row_for_id(previous_id)
                if selected_ids is not None and changed is not False:
                    self.table.restore_selection(selected_ids, previous_id)
                elif selected_ids is None and index >= 0:
                    self.table.selectRow(index)
                self._seen_revision = key
                self.show_details()
                if getattr(self, '_select_next_after', None):
                    self._select_next_after = None
                    if self._rows:
                        self.table.selectRow(0)
                        QTimer.singleShot(0, lambda: self.action(self.review_next))
                    else:
                        self.capture_help.setText('本次逐件核对已完成；可去比较，或继续收录。')
            else:
                self._requested_key = None  # A changed revision/profile never publishes stale rows.
        self.reading_label.setText(state.message + f' · 显示 {len(self._rows)} 个已收录/待判定实例')
        self.cancel_read_button.setEnabled(state.state == 'running')
        if state.state != 'running':
            self.read_timer.stop()
        fresh = self._seen_revision == key and state.state != 'running'
        for button in self._mutation_buttons:
            button.setEnabled(fresh)
        self.clear_library_button.setEnabled(fresh and self._clear_ready())

    def show_details(self):
        if self.runtime._closed or self.task._destroyed:
            return
        selected = self.selected()
        kind = selected[1] if selected else 'item'
        self.confirm_button.setVisible(kind != 'item')
        self.upgrade_button.setVisible(kind in ('upgrade', 'duplicate'))
        self.ignore_button.setVisible(kind != 'item')
        if not selected:
            self.image.clear()
            self.image.setText('当前筛选没有选中声骸。\n先开始收录，或切换筛选查看已保存记录。')
            self.details.clear()
            return
        record, kind, candidates = selected
        quality = self.inventory_model.quality(record)
        from echo_workshop.application.inventory import game_status_labels
        from echo_workshop.capture.game_status import status_evidence

        game_labels = '、'.join(game_status_labels(record)) or (
            '未装备' if record.game_equipped is False else '装备状态未知'
        )
        fields = status_evidence(record).get('fields', {})
        fields = fields if isinstance(fields, dict) else {}
        observed_times = []
        for field, label in (('game_equipped', '装备'),):
            details = fields.get(field, {})
            if isinstance(details, dict) and details.get('retained') is True:
                observed_times.append(f'{label}沿用：{details.get("observed_at") or "时间未知"}')
        retained_info = ('\n' + '\n'.join(observed_times)) if observed_times else ''
        self.details.setPlainText(
            f'本地实例：{record.item_id}\n收录：{record.captured_at}\n来源：{record.source}\n'
            f'游戏状态：{game_labels}\n状态观察：{record.game_status_updated_at or "尚无可靠观察"}{retained_info}\n'
            f'工坊装备者：{record.owner or ("未装配" if record.owner_state == "unequipped" else "未知")}\n'
            f'本地归属：{dict(equipped="已分配", unequipped="未分配", unknown="待确认", ambiguous="角色待区分")[record.owner_state]}\n'
            f'校验：{", ".join(quality.issues) or "通过"}\n判定：{kind}\n'
            f'候选关联：{", ".join(candidates) or "无"}\n备注：{record.note}\n\n原 OCR：\n{record.raw_text}'
        )
        self.image.clear()
        if hasattr(self.image, 'set_record'):
            from echo_workshop.ui.inventory.card_data import checked_protection_text

            reservation = self.inventory_model.claims.get(record.item_id, '')
            self.image.set_record(
                record,
                kind,
                score_text=checked_protection_text(record, quality, reservation),
                reservation=reservation,
                quality=quality,
            )
            return
        if record.screenshot_ref:
            try:
                pixmap = QPixmap()
                pixmap.loadFromData(self.runtime.evidence.read(record.screenshot_ref), 'PNG')
                self.image.setPixmap(
                    pixmap.scaled(370, 245, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            except (OSError, ValueError):
                self.image.setText('截图缺失或校验失败，请重新核对')
        else:
            self.image.setText('没有截图证据')

    def quick_score_selected(self):
        selected_ids = getattr(self.table, 'selected_ids', None)
        if callable(selected_ids) and len(selected_ids()) != 1:
            raise ValueError('快速评分一次只选择一件声骸。')
        selected = self.selected()
        if selected is None or selected[1] != 'item':
            raise ValueError('请选择一件已核对的声骸进行快速评分。')
        record = selected[0]
        quality = self.inventory_model.quality(record)
        if not quality.usable:
            raise ValueError('请选择一件已核对的声骸进行快速评分。')
        revision = self._mutation_revision()
        # Resolve the stable identity again, but never mutate a record or pretend
        # an observed owner is a saved loadout. One local dialog per inventory.
        try:
            current = self.runtime.inventory.get(record.item_id)
        except KeyError:
            raise ValueError('声骸已删除，请刷新后重新选择。') from None
        if current != record:
            raise ValueError('声骸记录已变化，请刷新后重新选择。')
        from echo_workshop.ui.inventory.quick_score import QuickScoreDialog

        previous = getattr(self, '_quick_score_dialog', None)
        if previous is not None:
            previous.close()
            previous.deleteLater()
        dialog = QuickScoreDialog(
            self.task, record, self, quality=quality, inventory_revision=revision
        )
        self._quick_score_dialog = dialog
        dialog.show()
        dialog.raise_()

    def confirm_selected(self):
        selected = self.selected()
        if not selected:
            return
        record, kind, _ = selected
        revision = self._mutation_revision()
        if kind == 'item':
            self.runtime.inventory.confirm(
                record.item_id,
                expected_revision=revision,
                quality=self.inventory_model.quality(record),
            )
        else:
            editor = RecordEditor(
                record,
                self,
                evidence=self.runtime.evidence,
                choices=ReviewChoices.from_runtime(self.runtime, record),
                quality_check=self.runtime.quality_checks.start,
                quality=self.inventory_model.quality(record),
            )
            editor.setWindowTitle('确认这是独立的另一件声骸（不是合并）')
            if editor.exec() == QDialog.Accepted:
                self.runtime.inventory.resolve_capture(
                    record.item_id,
                    'new',
                    corrected=editor.result_record,
                    expected_revision=revision,
                )

    def discard_selected(self):
        selected = self.selected()
        if not selected:
            return
        record, kind, _ = selected
        if kind == 'item':
            # 库存没有归档状态：已确认声骸只能显式删除，不能隐藏保留。
            raise ValueError('此声骸已入库；如需移除请使用删除。')
        self.runtime.inventory.resolve_capture(
            record.item_id, 'discard', expected_revision=self._mutation_revision()
        )

    def open_selected_evidence(self):
        selected = self.selected()
        if selected:
            open_evidence(selected[0], self.runtime.evidence, self)

    def export_backup(self):
        if self.runtime.backups.status.busy:
            raise ValueError('请等待或取消当前备份作业')
        path, _ = QFileDialog.getSaveFileName(
            self, '导出声骸数据（不含图片，可跨设备恢复）', '', 'ZIP backup (*.zip)'
        )
        if path:
            self.runtime.backups.start('export', path)
            self.refresh()

    def restore_backup(self):
        if self.runtime.backups.status.busy:
            raise ValueError('请等待或取消当前备份作业')
        path, _ = QFileDialog.getOpenFileName(
            self, '恢复本插件备份（不是导入游戏背包）', '', 'ZIP backup (*.zip)'
        )
        if path:
            self.runtime.capture.stop()
            self.runtime.backups.start('import', path)
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.table.proxy.resume()
        if self._reader.status.state == 'closed':
            self._reader = InventoryReader(
                self.runtime.inventory,
                self.runtime.stop_event,
                queries=self.runtime.inventory_queries,
            )
            self._requested_key = None
            self._processed_generation = -1
        self.read_timer.start()
        self.timer.start()
        self.refresh(force=True)

    def closeEvent(self, event):
        self.timer.stop()
        self.read_timer.stop()
        self.stop_worker()
        self.runtime.backups.cancel_and_wait()
        super().closeEvent(event)
