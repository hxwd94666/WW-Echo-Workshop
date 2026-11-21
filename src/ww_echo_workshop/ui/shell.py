# 装配三模式导航和公共助手工具栏。
"""One embedded assistant, one runtime, lazy business pages and flat settings."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QEvent, QTimer, SignalInstance
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from ww_echo_workshop.ui.widgets.controls import (
    CaptionLabel,
    TitleLabel,
)
from PySide6.QtWidgets import QPushButton
from ww_echo_workshop.ui.settings.profiles import ProfileManagementController
from ww_echo_workshop.ui.settings.controller import AssistantSettingsController
from ww_echo_workshop.ui.widgets.navigation import ModeNavigation
from ww_echo_workshop.ui.widgets.state import update_text, update_tooltip
from ww_echo_workshop.ui.widgets.workshop_palette import DARK, workshop_palette, workshop_styles

if TYPE_CHECKING:
    from ww_echo_workshop.application.runtime import PluginRuntime
    from ww_echo_workshop.ui.settings.controller import SettingsServices


class HostTheme(Protocol):
    changed: SignalInstance

    def palette(self) -> QPalette: ...


@dataclass(frozen=True)
class AssistantServices:
    runtime: 'PluginRuntime'
    closed: Callable[[], bool]
    set_profile: Callable[[str], None]
    build_settings: Callable[['EchoAssistantPanel'], 'SettingsServices']
    pages: Mapping[str, Callable[['EchoAssistantPanel'], QWidget]]
    host_theme: HostTheme | None = None


class EchoAssistantPanel(ProfileManagementController, AssistantSettingsController, QWidget):
    def __init__(self, services: AssistantServices, parent=None):
        super().__init__(parent)
        self.setObjectName('echoAssistantPanel')
        self.setAutoFillBackground(True)
        self.services, self.runtime = services, services.runtime
        self._workshop_colors = None
        self._palette_timer = QTimer(self)
        self._palette_timer.setSingleShot(True)
        self._palette_timer.timeout.connect(self._apply_workshop_palette)
        if parent is not None:
            parent.installEventFilter(self)
        QApplication.instance().paletteChanged.connect(self._schedule_workshop_palette)
        if services.host_theme is not None:
            services.host_theme.changed.connect(self._schedule_workshop_palette)
        self._apply_workshop_palette()
        self._owned_pages = {}
        self._stopped = False
        self._match_inventory_session = None
        self._inventory_confirmation = None
        self._match_origin = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        header = self.header = QHBoxLayout()
        header.addWidget(TitleLabel('声骸工坊'))
        self.settings_button = QPushButton('设置')
        self.settings_button.clicked.connect(self.show_settings)
        header.addWidget(self.settings_button)
        header.addStretch()
        self.capture_status = CaptionLabel('未开始收录')
        self.capture_status.setWordWrap(False)
        header.addWidget(self.capture_status)
        outer.addLayout(header)
        self.navigation = ModeNavigation()
        for key, text in (
            ('workspace', '工作台'),
            ('comparison', '匹配'),
            ('allocation', '配装'),
            ('inventory', '声骸库'),
        ):
            self.navigation.addItem(key, text, onClick=lambda checked, k=key: self.navigate(k))
        outer.addWidget(self.navigation)
        self.toolbar = QHBoxLayout()
        outer.addLayout(self.toolbar)
        self.feedback = CaptionLabel()
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        outer.addWidget(self.feedback)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        inventory = self._page('inventory')
        # Reuse exactly the capture controls owned by the inventory controller.
        header.insertWidget(2, inventory.profile_box)
        inventory.profile_box.setVisible(True)
        self.toolbar.addStretch()
        header.addWidget(inventory.intake_mode)
        header.addWidget(inventory.scan_level)
        inventory.intake_mode.show()  # Same controls are mounted once in the shared toolbar.
        self.scan_failures_button = QPushButton('处理未收录失败')
        self.scan_failures_button.clicked.connect(
            lambda: self.inventory_page.action(self.inventory_page.show_scan_failures)
        )
        self.toolbar.addWidget(self.scan_failures_button)
        for button in (inventory.start_button, inventory.pause_button, inventory.stop_button):
            header.addWidget(button)
        self._avatar_revision = -1
        self.settings_services = self.services.build_settings(self)
        self.settings_page = self._build_settings()
        self.stack.addWidget(self.settings_page)
        self._activate('workspace')
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start()
        # Parent deletion is distinct from page hiding. Join before QObject dies.
        self.destroyed.connect(lambda *_: self.stop_worker())
        self.refresh_status()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (
            QEvent.PaletteChange,
            QEvent.StyleChange,
        ):
            self._schedule_workshop_palette()
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ApplicationFontChange and hasattr(self, '_palette_timer'):
            self._schedule_workshop_palette()

    def _schedule_workshop_palette(self, *_):
        # OKWW publishes the Qt palette and stylesheet in separate steps.
        self._palette_timer.start(0)

    def _apply_workshop_palette(self):
        parent = self.parentWidget()
        if self.services.host_theme is not None:
            host = self.services.host_theme.palette()
        else:
            host = parent.palette() if parent is not None else QApplication.palette()
        palette, colors = workshop_palette(host)
        if self._workshop_colors != colors:
            self._workshop_colors = colors
            self.setProperty('workshopDark', colors is DARK)
        styles = workshop_styles(colors)
        if self.styleSheet() != styles:
            self.setStyleSheet(styles)
        if self.palette() != palette:
            self.setPalette(palette)
        for surface in getattr(self, '_settings_surfaces', ()):
            surface.setPalette(palette)
        for page in getattr(self, '_owned_pages', {}).values():
            if hasattr(page, 'apply_surface_palette'):
                page.apply_surface_palette(palette)

    def _page(self, name):
        if name not in self._owned_pages:
            page = self.services.pages[name](self)
            self._owned_pages[name] = page
            self.stack.addWidget(page)
        return self._owned_pages[name]

    @property
    def inventory_page(self):
        return self._owned_pages.get('inventory')

    def _show_inventory_feedback(self, message):
        if message:
            self.feedback.setText(message)
        self.feedback.setVisible(bool(message))

    def _set_inventory_profile(self, profile_id):
        self.services.set_profile(profile_id)

    def closed(self):
        return self._stopped or self.services.closed()

    def _inventory_profile_changed(self):
        self.feedback.hide()
        current = next(
            (
                name
                for name, page in self._owned_pages.items()
                if page is self.stack.currentWidget()
            ),
            None,
        )
        if current and current != 'inventory':
            # The shared selector is visible in all modes.
            self.navigate(current)

    def _open_inventory_route(self):
        self.navigate('inventory')
        return self.stack.currentWidget() is self._owned_pages['inventory']

    def _activate(self, name):
        if self.closed():
            return None
        page = self._page(name)
        self._business_route = name
        self.stack.setCurrentWidget(page)  # Never close() a page on navigation.
        self.navigation.setCurrentItem(name)
        self.navigation.show()
        self.settings_button.setText('设置')
        return page

    def _open_profile_route(self, name, profile):
        """Switch the explicit profile without ending work in the current one."""
        if profile not in self.runtime.inventory.profiles():
            raise ValueError('档案已不存在，请重新选择。')
        reason = self.profile_switch_error(profile)
        if reason:
            raise ValueError(reason)
        capture = self.runtime.capture.status
        if capture.state != 'stopped' and capture.profile_id != profile:
            raise ValueError('另一档案正在收录，请先明确结束，再切换。')
        page = self._owned_pages.get(name)
        field = 'profile' if name == 'allocation' else 'profile_box'
        if page is not None and getattr(page, field).currentData() != profile:
            selector = getattr(page, field)
            if (
                not selector.isEnabled()
                or getattr(page, '_future', None) is not None
                or getattr(page, '_io_future', None) is not None
            ):
                raise ValueError('目标窗口正在处理，请完成或取消作业后再切换档案。')
            if name == 'allocation' and page._role_draft is not None and page.incoming.count():
                raise ValueError('配装仍有待配置角色，请先处理或取消草稿，再切换档案。')
        page = self._activate(name)
        if page is None:
            raise ValueError('插件已停止，请重新打开后切换档案。')
        selector = getattr(page, field)
        if selector.currentData() != profile:
            index = selector.findData(profile)
            if index < 0:
                selector.blockSignals(True)
                selector.addItem(self.runtime.inventory.profiles()[profile], userData=profile)
                selector.blockSignals(False)
                index = selector.findData(profile)
            if name == 'inventory':
                selector.blockSignals(True)
                selector.setCurrentIndex(index)
                selector.blockSignals(False)
                page.profile_changed()
            else:
                selector.setCurrentIndex(index)
        self._set_inventory_profile(profile)
        return page

    def navigate(self, name):
        if self._stopped:
            return
        try:
            if self._match_inventory_session is not None and name != 'inventory':
                self.cancel_inventory_selection()
            profile = self.inventory_page.profile_box.currentData()
            self._open_profile_route(name, profile)
            self.feedback.hide()
        except (ValueError, KeyError, OSError) as error:
            self.feedback.setText(str(error))
            self.feedback.show()
            current = next(
                (k for k, p in self._owned_pages.items() if p is self.stack.currentWidget()), None
            )
            if current:
                self.navigation.setCurrentItem(current)

    def _help_navigate(self, route):
        if self.has_edit_draft() or self._match_inventory_session is not None:
            return '请先保存或取消当前编辑、选择，再切换模式。'
        if any(self._page_rule_dirty(page) for page in self._owned_pages.values()):
            return '请先保存或取消角色要求，再切换模式。'
        self.navigate(route)
        if self.stack.currentWidget() is not self._owned_pages.get(route):
            return self.feedback.text() or '请先完成当前操作。'
        return ''

    def begin_inventory_selection(self, requester):
        from PySide6.QtCore import Qt
        from ww_echo_workshop.application.matching.inventory import (
            InventoryViewState,
            MatchInventorySession,
        )

        if self._match_inventory_session is not None:
            self._activate('inventory')
            return
        inventory = self.inventory_page
        route = next(
            (
                name
                for name in ('comparison', 'allocation')
                if self._owned_pages.get(name) is requester
            ),
            None,
        )
        if route is None:
            raise ValueError('请从匹配或直接配装打开声骸选择。')
        profile = (
            requester.profile if route == 'allocation' else requester.profile_box
        ).currentData()
        if profile != inventory.profile_box.currentData():
            raise ValueError('请先切换到调用页面使用的档案，再选择声骸。')
        if self.has_edit_draft() or getattr(requester, '_rule_dirty', False):
            raise ValueError('请先保存或取消当前编辑，再选择候选声骸。')
        if route == 'allocation':
            reason = requester.inventory_selection_error()
            if reason:
                raise ValueError(reason)
            previous = requester._candidate_ids
            context = requester.inventory_selection_context()
        else:
            if (
                requester._future is not None and requester._worker_kind != 'catalogue'
            ) or requester._publication is not None:
                raise ValueError('正在计算，请完成或取消后再选择声骸。')
            previous, context = requester.item_picker.selected_ids(), None
        inventory.refresh()
        if inventory._reader.status.state != 'ready':
            raise ValueError('声骸库正在读取，请稍后再选择。')
        inventory.search_timer.stop()
        inventory._apply_search()
        current = inventory.table.currentIndex()
        state = InventoryViewState(
            profile,
            inventory.search_box.text(),
            inventory.filter_drawer.filters(),
            frozenset(inventory.table.selected_ids()),
            current.data(Qt.UserRole) if current.isValid() else None,
            inventory.table.verticalScrollBar().value(),
        )
        inventory.filter_drawer.close_panel()
        self._match_origin = requester
        self._match_origin_context = context
        self._match_inventory_session = MatchInventorySession(
            profile, previous, state, return_route=route
        )
        inventory.set_match_selection_mode(True)
        self._activate('inventory')
        self.refresh_profile_guard()

    def finish_inventory_selection(self, *, filtered=False):
        from ww_echo_workshop.application.diagnostics import begin_job

        job = begin_job('ui.match_inventory_apply')
        try:
            counts = self._finish_inventory_selection(filtered=filtered, _job=job)
        except Exception as error:
            job.finish('failed', error=error)
            raise
        else:
            if not counts.get('deferred'):
                job.finish(counters=counts)

    def _finish_inventory_selection(self, *, filtered=False, _job=None, _confirmation=None):
        session = self._match_inventory_session
        inventory, requester = self.inventory_page, self._match_origin
        if session is None or requester is None:
            raise ValueError('请先从匹配或配装打开声骸选择。')
        profile = inventory.profile_box.currentData()
        session.ensure_profile(profile)
        if session.return_route == 'allocation':
            requester.check_inventory_selection_context(self._match_origin_context)
        elif requester.profile_box.currentData() != profile or requester._rule_dirty:
            raise ValueError('匹配档案或适配草稿已变化，请先取消选择后重新打开。')
        if inventory._reader.status.state != 'ready':
            raise ValueError('声骸库正在读取，请稍后应用选择。')
        if _confirmation is None:
            previous = getattr(self, '_inventory_confirmation', None)
            if previous is not None:
                previous.cancel()
            # Capture actual checks before a new asynchronous filter changes rows.
            inventory._sync_match_selection()
            selected_ids = frozenset(session.selected_ids)
        else:
            selected_ids = _confirmation.selected_ids
        inventory.search_timer.stop()
        inventory._apply_search()
        if filtered and not inventory.has_candidate_filter():
            raise ValueError('尚未设置筛选条件；请先设置筛选，或明确点击“全选”后确定勾选。')
        inventory._sync_match_selection()
        # Applying a filter freezes actual instance IDs, never a live query.
        visible = inventory._visible_match_ids()
        if visible is None:
            if inventory.table.proxy.error or inventory.table.proxy.closed:
                raise ValueError(inventory.table.proxy.error or '声骸视图已关闭，请重新打开。')
            from ww_echo_workshop.ui.inventory.selection import PendingInventorySelection

            pending = self._inventory_confirmation = PendingInventorySelection(
                self, filtered=filtered, selected_ids=selected_ids, job=_job
            )
            pending.start()
            return dict(selected=0, visible=0, deferred=True)
        ids = visible if filtered else selected_ids
        if session.return_route == 'allocation':
            requester.apply_inventory_selection(ids)
        else:
            requester._apply_inventory_ids(ids)
        # A failed requester guard leaves the selection draft cancellable.
        if filtered:
            session.apply_filtered(profile, visible)
        else:
            session.apply_selected(profile)
        route = session.return_route
        self._restore_inventory_selection_view(session.view_state)
        self._activate(route)
        return dict(selected=len(ids), visible=len(visible))

    def cancel_inventory_selection(self):
        session = self._match_inventory_session
        if session is None:
            return
        from ww_echo_workshop.application.diagnostics import begin_job

        job = begin_job('ui.match_inventory_cancel')
        try:
            selected = len(session.selected_ids)
            state = session.cancel(self.inventory_page.profile_box.currentData())
            route = session.return_route
            self._restore_inventory_selection_view(state)
            self._activate(route)
        except Exception as error:
            job.finish('failed', error=error)
            raise
        else:
            job.finish('cancelled', counters=dict(selected=selected))

    def _restore_inventory_selection_view(self, state):
        pending = getattr(self, '_inventory_confirmation', None)
        if pending is not None:
            pending.cancel()
        inventory = self.inventory_page
        self._match_inventory_session = self._match_origin = None
        self._match_origin_context = None
        inventory._match_selection_restoring = True
        try:
            inventory.search_timer.stop()
            blocked = inventory.search_box.blockSignals(True)
            inventory.search_box.setText(state.search)
            inventory.search_box.blockSignals(blocked)
            inventory.filter_drawer.close_panel()
            inventory.filter_drawer._applied = state.filters
            inventory.library_filters.set_filters(state.filters)
            inventory._search_inventory_controller()
            inventory.table.restore_selection(state.selected_ids, state.current_id)
            inventory.table.verticalScrollBar().setValue(state.scroll)
            inventory.set_match_selection_mode(False)
        finally:
            inventory._match_selection_restoring = False
        self.refresh_profile_guard()

    def profile_switch_error(self, target):
        """Inspect all pages, including hidden editors; never discard a draft."""
        session = self._match_inventory_session
        if session is not None and session.active and target != session.profile_id:
            return '正在选择候选声骸，请先应用或取消选择，再切换档案。'
        inventory = self.inventory_page
        accepted = inventory._accepted_profile if inventory is not None else target
        if self.runtime.backups.status.busy and target != accepted:
            return '数据作业进行中，请结束后再切换档案。'
        capture = self.runtime.capture.status
        if capture.state != 'stopped' and capture.profile_id != target:
            return '请先结束当前收录，再切换档案。'
        for name, page in self._owned_pages.items():
            selector = page.profile if name == 'allocation' else page.profile_box
            # The inventory signal fires after the selection changed. Its
            # accepted profile records the actual draft/read context.
            current = getattr(page, '_accepted_profile', selector.currentData())
            if current == target:
                continue
            if self._page_draft(page):
                return '还有未保存的编辑，请保存或取消后再切换档案。'
            if self._page_busy(page):
                return '比较或配装正在处理，请完成或取消后再切换档案。'
        return ''

    @staticmethod
    def _page_busy(page):
        method = getattr(page, 'lifecycle_busy', None)
        if callable(method):
            return bool(method())
        return bool(
            getattr(page, '_future', None) is not None
            or getattr(page, '_io_future', None) is not None
        )

    @staticmethod
    def _page_draft(page):
        method = getattr(page, 'lifecycle_draft', None)
        if callable(method):
            return bool(method())
        return getattr(page, '_editor', None) is not None or (
            getattr(page, '_role_draft', None) is not None and page.incoming.count()
        )

    @staticmethod
    def _page_rule_dirty(page):
        method = getattr(page, 'lifecycle_rule_dirty', None)
        if callable(method):
            return bool(method())
        return bool(getattr(page, '_rule_dirty', False))

    def has_edit_draft(self):
        return any(self._page_draft(page) for page in self._owned_pages.values())

    def profile_mutation_error(self, *, ignore_backup=False):
        if self.closed():
            return '插件已停止，请重新打开后管理档案。'
        if getattr(self, '_profile_job', None) is not None and not ignore_backup:
            return '档案数据正在处理，请稍后再管理档案。'
        if self.runtime.capture.status.state != 'stopped':
            return '请先停止收录或扫描，再管理档案。'
        if self.runtime.scan.status.state in ('preparing', 'running', 'paused', 'draining'):
            return '扫描尚未结束，请结束后再管理档案。'
        if not ignore_backup and self.runtime.backups.status.busy:
            return '备份或恢复正在进行，请完成或取消后再管理档案。'
        if self._match_inventory_session is not None:
            return '请先完成或取消声骸选择，再管理档案。'
        for page in self._owned_pages.values():
            if self._page_busy(page):
                return '请先结束当前计算或确认操作，再管理档案。'
            if self._page_draft(page) or self._page_rule_dirty(page):
                return '请先保存或取消当前编辑，再管理档案。'
        if getattr(getattr(self, 'repair', None), 'future', None) is not None:
            return '配置修复正在进行，请结束后再管理档案。'
        return ''

    def refresh_profile_guard(self):
        inventory = self.inventory_page
        if inventory is None:
            return
        reason = self.profile_mutation_error()
        revision = None if self.closed() else self.runtime.inventory.try_revision()
        inventory.profile_box.setEnabled(not reason)
        if hasattr(self, 'create_profile_button'):
            for control in (
                self.create_profile_button,
                self.profile_name,
                self.rename_profile_button,
                self.profile_rename_edit,
            ):
                control.setEnabled(not reason)
                update_tooltip(control, reason or '改名不改变档案 ID、声骸或已保存配装。')
            self._sync_profile_editor(revision=revision)
        # A backup must not mask an independent draft or calculation. The
        # cancel button lives outside these gated operation surfaces so it
        # stays available even when both kinds of activity are present.
        other_activity = bool(self.profile_mutation_error(ignore_backup=True))
        if 'recovery' in self.settings_sections:
            self.settings_sections['recovery'].content.setEnabled(not other_activity)
        self._refresh_managed_controls(reason, revision=revision)
        for button in getattr(self, 'data_backup_buttons', {}).values():
            button.setEnabled(not reason)
            update_tooltip(button, reason.replace('管理档案', '进行数据操作') if reason else '')
        inventory.cancel_backup_button.setEnabled(self.runtime.backups.status.busy)

    def _sync_profile_editor(self, *_, force=False, revision=...):
        return self._sync_managed_editor(force=force, revision=revision)

    def rename_profile(self):
        return self._rename_managed_profile()

    def create_profile(self):
        inventory = self.inventory_page
        self.refresh_profile_guard()
        reason = self.profile_mutation_error()
        if reason:
            self.feedback.setText(reason)
            self.feedback.show()
            return
        name = self.profile_name.text().strip()
        if not name:
            self.feedback.setText('请输入档案名称。')
            self.feedback.show()
            return
        before = inventory.profile_box.currentData()
        inventory.action(lambda: inventory.new_profile(name=name))
        if inventory.profile_box.currentData() != before:
            self.profile_name.clear()
            self._resync_profile_pages(inventory.profile_box.currentData(), activate=True)
            self._profile_message('档案已创建并打开。')

    def refresh_status(self):
        if self.closed():
            self.timer.stop()
            return
        revision = self.runtime.avatars.revision
        if revision != self._avatar_revision:
            self._avatar_revision = revision
            self.inventory_page.table.viewport().update()
            self.inventory_page.image.update()
            comparison = self._owned_pages.get('comparison')
            if comparison is not None:
                comparison._sync_role_strip()
        state = self.runtime.capture.status
        weights = getattr(self, 'weights', None)
        if weights is not None and (
            weights.resource_controls.isVisible() or weights.advanced_controls.isVisible()
        ):
            weights.refresh()
        # An inline repair stops its local timer when hidden. Consume a
        # completed receipt here too, otherwise the stale Future blocks Data
        # forever until the user happens to reopen Advanced.
        repair = getattr(self, 'repair', None)
        if repair is not None and repair.future is not None:
            repair.poll()
        self.poll_profile_jobs()
        self._refresh_backup_feedback()
        workspace = self._owned_pages.get('workspace')
        if workspace is not None:
            workspace.live_card.refresh_weights()
            workspace.refresh_summary()
        label = {
            'stopped': '收录就绪',
            'armed': '等待收录快捷键',
            'paused': '已暂停',
            'active': '收录中',
            'single': '收录中',
        }.get(state.state, '收录状态更新中')
        capture_text = f'{label} · 新增 {state.added}'
        inventory = self.inventory_page
        intake = inventory.services.intake
        inventory.refresh_capture_controls()
        inventory.cancel_backup_button.setVisible(self.runtime.backups.status.busy)
        inventory.profile_box.setVisible(True)
        self.refresh_profile_guard()
        busy = state.state != 'stopped'
        self.settings_scan_level.setEnabled(not busy and not self.runtime.backups.status.busy)
        level = intake.minimum_level
        index = self.settings_scan_level.findData(level)
        if index >= 0 and self.settings_scan_level.currentIndex() != index:
            self.settings_scan_level.blockSignals(True)
            self.settings_scan_level.setCurrentIndex(index)
            self.settings_scan_level.blockSignals(False)
        scan = self.runtime.scan.status
        same_scan_profile = self.runtime.scan.profile_id == (
            inventory.profile_box.currentData() or ''
        )
        if state.mode == 'automatic' and (
            busy or (same_scan_profile and intake.mode == 'automatic')
        ):
            label = {
                'preparing': '准备扫描',
                'running': '扫描中',
                'paused': '扫描已暂停',
                'draining': '正在解析',
                'finished': '扫描完成',
                'interrupted': '扫描已停止',
            }.get(scan.state, '未扫描')
            capture_text = f'{label} · {scan.captured} 件'
            if scan.backlog:
                capture_text += f' · 解析 {scan.backlog}'
        elif state.mode == 'manual' and busy:
            backlog = intake.manual_backlog
            capture_text = (
                f'{label} · 新增 {state.added} · 解析队列 {backlog} · 待核对 {state.needs_review}'
            )
        elif not busy:
            if intake.mode == 'automatic' and intake.ready:
                capture_text = '扫描时请勿操作鼠标'
        self.scan_failures_button.setVisible(same_scan_profile and scan.failed > 0)
        self.scan_failures_button.setEnabled(
            not busy
            and not self.runtime.backups.status.busy
            and inventory._editor is None
            and self._match_inventory_session is None
        )
        update_tooltip(self.capture_status, inventory.capture_help.text())
        if not intake.ready and state.state == 'stopped':
            capture_text = '未连接游戏 · 可计算'
        update_text(self.capture_status, capture_text)

    def show_intake_review(self):
        self.navigate('inventory')
        inventory = self.inventory_page
        if self.stack.currentWidget() is not inventory:
            return

        def review():
            from ww_echo_workshop.application.diagnostics import begin_job

            job = begin_job('ui.review')
            try:
                inventory.show_pending_review()
            except Exception as error:
                job.finish('failed', error=error)
                raise
            else:
                job.finish(counters=dict(visible=inventory.table.proxy.rowCount()))

        inventory.action(review)

    def _refresh_backup_feedback(self):
        state = self.runtime.backups.status
        label = self.profile_status
        notice = getattr(self, '_profile_notice', None)
        if notice is not None and notice[0] == state.job_id:
            label.setText(notice[1])
            label.show()
            return
        if state.state == 'idle':
            label.hide()
            return
        label.show()
        from ww_echo_workshop.ui.settings.help import job_text

        name = {
            'profile-export': '档案导出',
            'profile-preview': '档案检查',
            'profile-import': '档案导入',
            'profile-delete': '档案删除',
            'export': '声骸库导出',
            'import': '声骸库恢复',
            'inspect-workspace': '副本预览',
        }.get(state.kind, '数据作业')
        counts = f' · {state.done}/{state.total}' if state.total else ''
        label.setText(name + ' · ' + job_text(state.state) + counts + ' · ' + state.message)

    def stop_worker(self):
        """No Qt calls: task unload may run off the GUI thread."""
        self._stopped = True
        for page in tuple(self._owned_pages.values()):
            request = getattr(page, 'request_stop', None)
            if callable(request):
                request()
            else:
                page.stop_worker()
        repair = getattr(self, 'repair', None)
        if repair is not None:
            repair.stop_worker()

    def closeEvent(self, event):
        pending = self._inventory_confirmation
        if pending is not None:
            pending.cancel()
        self.timer.stop()
        self.stop_worker()
        for page in tuple(self._owned_pages.values()):
            page.close()
        for name in ('shortcuts', 'weights', 'workspace', 'repair'):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.close()
        super().closeEvent(event)
