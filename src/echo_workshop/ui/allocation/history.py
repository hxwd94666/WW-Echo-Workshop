# 按角色索引查看保存配装并管理未提交的单槽替换预览。
"""Saved-role details are independent from the main allocation workspace."""

from datetime import datetime

from PySide6.QtCore import Qt, QSize, QTimer, QThread, QMetaObject
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    isDarkTheme,
    qconfig,
    themeColor,
)

from echo_workshop.resources.search import search_matches
from echo_workshop.core.filters import is_protected
from echo_workshop.ui.allocation.history_grid import SavedLoadoutGrid
from echo_workshop.ui.allocation.saved_role_actions import SavedRoleActions
from echo_workshop.ui.allocation.saved_refresh import SavedRefresh
from echo_workshop.ui.allocation.replacement import ReplacementDialog
from echo_workshop.ui.allocation.history_projection import SavedListProjection
from echo_workshop.ui.widgets.rating import HistoryScoreDelegate, HISTORY_SCORE_ROLE, lookup_avatar
from echo_workshop.ui.widgets.workspace import LoadoutCards


def display_time(value):
    try:
        return (
            datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            .astimezone()
            .strftime('%Y-%m-%d %H:%M')
        )
    except (ValueError, TypeError):
        return str(value)[:16].replace('T', ' ')


def _identity(row):
    return row['plan_id'], row['character_id'], row['modal_id']


class SavedLoadoutsWorkspace(SavedRefresh, SavedListProjection, SavedRoleActions, QWidget):
    """One sidebar role, one frozen five-card detail, no working-draft mutation."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.page = controller
        self.mode = 'current'
        self._selection = {'current': None, 'history': None}
        self._entries = {}
        self._details = {}
        self._drafts = {}
        self._signature = self._detail_signature = None
        self._projecting = False
        self._generation = 0
        self._load_requested = None
        self._dialog = None
        self._replacement_options = None
        self._pending_saved_identity = None
        self._error = ''
        self._button_timer = QTimer(self)
        self._button_timer.setSingleShot(True)
        self._button_timer.timeout.connect(self._sync_buttons)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        header = QHBoxLayout()
        self.back_button = PushButton('返回', self)
        self.back_button.clicked.connect(self.page._hide_history)
        self.cancel_button = PushButton('取消读取', self)
        self.cancel_button.clicked.connect(self.page.cancel_job)
        header.addWidget(self.cancel_button)
        self.build_verification(header)
        self.status = CaptionLabel(self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.splitter = QSplitter(Qt.Horizontal, self)
        self.sidebar = QWidget(self.splitter)
        left = QVBoxLayout(self.sidebar)
        left.setContentsMargins(0, 0, 8, 0)
        self.tabs = SegmentedWidget(self.sidebar)
        for key, label in (('current', '当前配装'), ('history', '备份配装')):
            self.tabs.addItem(key, label, onClick=lambda checked, key=key: self.set_mode(key))
        self.tabs.setCurrentItem(self.mode)
        left.addWidget(self.tabs)
        self.search = LineEdit(self.sidebar)
        self.search.setPlaceholderText('搜索角色、模版或日期')
        self.search.textChanged.connect(lambda: self.project(force=True))
        left.addWidget(self.search)
        self.list = QListWidget(self.sidebar)
        self.list.setItemDelegate(HistoryScoreDelegate(self.list))
        self.list.setWordWrap(True)
        self.list.setSpacing(5)
        self.list.setIconSize(QSize(30, 30))
        self.list.currentItemChanged.connect(self._selected)
        self.list.itemClicked.connect(self._retry_selected)
        left.addWidget(self.list, 1)
        self.detail = QWidget(self.splitter)
        right = QVBoxLayout(self.detail)
        right.setContentsMargins(8, 0, 0, 0)
        title = QHBoxLayout()
        self.avatar = QLabel(self.detail)
        self.avatar.setFixedSize(32, 32)
        self.avatar.setAlignment(Qt.AlignCenter)
        title.addWidget(self.avatar)
        self.title = BodyLabel('选择一项配装', self.detail)
        self.title.setWordWrap(True)
        title.addWidget(self.title)
        right.addLayout(title)
        self.stack = QStackedWidget(self.detail)
        self.empty = QWidget(self.stack)
        empty = QVBoxLayout(self.empty)
        empty.addStretch()
        self.empty_label = BodyLabel('选择左侧角色，查看配装。', self.empty)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        empty.addWidget(self.empty_label)
        self.empty_action_button = PushButton('去生成配装', self.empty)
        self.empty_action_button.clicked.connect(self.page._hide_history)
        empty.addWidget(self.empty_action_button, alignment=Qt.AlignHCenter)
        empty.addStretch()
        self.stack.addWidget(self.empty)
        self.detail_cards = LoadoutCards(
            self.stack,
            grid_type=SavedLoadoutGrid,
            summary_layout=title,
            panel_service=self.page.runtime.panel,
        )
        title.addStretch(1)
        self.detail_cards.cards.avatar_cache = self.page.runtime.avatars
        self.detail_cards.selected.connect(self._card_selected)
        self.detail_cards.cards.selectionModel().selectionChanged.connect(
            lambda *_: self._schedule_buttons()
        )
        self.stack.addWidget(self.detail_cards)
        right.addWidget(self.stack, 1)
        self.action_note = CaptionLabel(self.detail)
        self.action_note.setWordWrap(False)
        actions = QHBoxLayout()
        actions.addWidget(self.action_note, 1)
        actions.addLayout(header)
        self.replace_button = PushButton('替换优化', self.detail)
        self.replace_button.clicked.connect(self._replace)
        from echo_workshop.ui.allocation.action_labels import (
            explain,
            APPLY_TIP,
            BACKUP_TIP,
            REPLACE_TIP,
        )

        explain(self.replace_button, REPLACE_TIP)
        title.addWidget(self.replace_button)
        self.discard_button = PushButton('取消预览', self.detail)
        self.discard_button.clicked.connect(self._discard_preview)
        actions.addWidget(self.discard_button)
        self.release_button = PushButton('解除保护', self.detail)
        explain(
            self.release_button,
            '只解除所选声骸的保护，其余声骸保持不变；不删除当前配装，不修改游戏。',
        )
        self.release_button.clicked.connect(self._release_selected)
        title.addWidget(self.release_button)
        self.remove_button = PushButton('移除配装', self.detail)
        explain(self.remove_button, '仅移除当前选择的角色配装；确认前不改变数据。')
        self.remove_button.clicked.connect(self._remove)
        title.addWidget(self.remove_button)
        actions.addStretch(1)
        self.backup_button = PushButton('另存备份', self.detail)
        explain(self.backup_button, BACKUP_TIP)
        self.backup_button.clicked.connect(self._backup)
        actions.addWidget(self.backup_button)
        self.save_button = PrimaryPushButton('设为当前配装', self.detail)
        explain(self.save_button, APPLY_TIP)
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.save_button)
        actions.addWidget(self.back_button)
        right.addLayout(actions)
        self.confirmation_layout = QVBoxLayout()
        self.confirmation_layout.setContentsMargins(0, 0, 0, 0)
        right.addLayout(self.confirmation_layout)
        outer.addWidget(self.splitter, 1)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([270, 900])
        self._update_palette()
        qconfig.themeChangedFinished.connect(self._theme_changed)

    @property
    def view_token(self):
        row = self.selection()
        if row is None:
            return None
        return (self.mode, self.page.profile.currentData(), *_identity(row), self._generation)

    def _cache_key(self):
        row = self.selection()
        return (self.page.profile.currentData(), row['plan_id']) if row else None

    def _draft_key(self):
        row = self.selection()
        return (self.page.profile.currentData(), *_identity(row)) if row else None

    def _busy(self):
        p = self.page
        return (
            p._future is not None
            or p._io_future is not None
            or p._editor is not None
            or getattr(p, '_pending_precheck', None) is not None
            or getattr(p, '_pending_role_editor', None) is not None
        )

    def project(self, *, force=False):
        if not self._lifecycle_active() or (not force and not self.isVisible()):
            return
        with self.page.refresh_projection():
            self._project(force=force)

    def _project(self, *, force=False):
        self._poll_verification()
        rows = self._rows()
        profile = self.page.profile.currentData()
        signature = (
            self.mode,
            profile,
            self._rows_revision,
            self.search.text(),
        )
        if force or signature != self._signature:
            self._signature = signature
            self._projecting = True
            scroll = self.list.verticalScrollBar().value()
            old_selection = self._selection[self.mode]
            try:
                selected = self._pending_saved_identity or self._selection[self.mode]
                self.list.clear()
                self._entries = {}
                for row in rows:
                    identity = _identity(row)
                    date = display_time(row.get('created_at', row.get('updated_at', '')))
                    label = row['label']
                    if not search_matches(
                        label + ' ' + date + ' ' + row['plan_id'], self.search.text()
                    ):
                        continue
                    state = (
                        date + f' · 保护 {self.protection_count(row)}/5'
                        if self.mode == 'current'
                        else date
                        + ' · '
                        + {
                            'backup': '手动备份',
                            'current': '保存版本',
                            'generated': '生成快照',
                            'legacy': '旧版快照',
                        }.get(row.get('origin'), '历史快照')
                    )
                    if row.get('issues'):
                        state += ' · 待复核'
                    item = QListWidgetItem(label + '\n' + state, self.list)
                    item.setData(Qt.UserRole, identity)
                    rating = row.get('rating_label')
                    total = row.get('total_cents')
                    scored = type(total) is int and total >= 0
                    item.setData(HISTORY_SCORE_ROLE, ((total / 100, rating),) if scored else ())
                    item.setSizeHint(QSize(230, 90 if scored else 68))
                    item.setIcon(self._role_icon(row['character_id']))
                    self._entries[identity] = row
                    if identity == selected:
                        self.list.setCurrentItem(item)
                        if identity == self._pending_saved_identity:
                            self._pending_saved_identity = None
                if (
                    self.list.currentItem() is None
                    and self.list.count()
                    and self._pending_saved_identity is None
                ):
                    self.list.setCurrentRow(0)
            finally:
                self._projecting = False
            self._list_icon_revision = self._icon_revision()
            self._selected(self.list.currentItem(), None)
            if self._selection[self.mode] == old_selection:
                self.list.verticalScrollBar().setValue(scroll)
        self._refresh_icons()
        self._project_detail()
        self._sync_buttons()
        self._verification_notice()
        self._maybe_load()

    def protection_count(self, row):
        live, claims = self._live_inventory()
        return sum(
            is_protected(live[item], claims.get(item)) for item in row['item_ids'] if item in live
        )

    def selection(self):
        return self._entries.get(self._selection[self.mode])

    def _selected(self, item, _previous):
        if self._projecting:
            return
        with self.page.refresh_projection():
            self._select_item(item)

    def _select_item(self, item):
        if self._projecting:
            return
        identity = item.data(Qt.UserRole) if item is not None else None
        if identity != self._selection[self.mode]:
            self.page.cancel_current_action()
            self._selection[self.mode] = identity
            self._generation += 1
            self._load_requested = None
            self._error = ''
            self._detail_signature = None
        row = self.selection()
        if row is not None:
            combo = self.page.current_choices if self.mode == 'current' else self.page.saved
            index = combo.findData(
                row['character_id'] if self.mode == 'current' else row['plan_id']
            )
            combo.setCurrentIndex(index)
        self._project_detail()
        self._sync_buttons()
        self._maybe_load()

    def set_mode(self, mode):
        if mode not in ('current', 'history') or self._busy() or self._dialog is not None:
            return
        self.page.cancel_current_action()
        self.mode = mode
        self.tabs.setCurrentItem(mode)
        self._generation += 1
        self._load_requested = None
        self._error = ''
        self._signature = self._detail_signature = None
        self.project(force=True)

    def _retry_selected(self, item):
        if (
            not self._busy()
            and not self.current_detail()
            and item.data(Qt.UserRole) == self._selection[self.mode]
        ):
            self._load_requested = None
            self._error = ''
            self._detail_signature = None
            self.project()

    def _maybe_load(self):
        row = self.selection()
        if (
            not self.isVisible()
            or row is None
            or self.current_detail()
            or self._busy()
            or self._load_requested == self.view_token
        ):
            return
        self._load_requested = self.view_token
        self.page.action(
            lambda: self.page.open_saved_role(
                row['plan_id'], row['character_id'], row['modal_id'], self.view_token
            )
        )

    def publish_detail(self, value, token):
        if token != self.view_token or value.payload is None:
            return
        payload = value.payload
        row = self.selection()
        if (
            payload['plan_id'] != row['plan_id']
            or payload['request']['profile_id'] != self.page.profile.currentData()
        ):
            return
        if not any(
            (item['character_id'], item['modal_id']) == (row['character_id'], row['modal_id'])
            for item in payload['result']['roles']
        ):
            self.show_error('保存的配装中未找到此角色。', token)
            return
        self._details[self._cache_key()] = (payload, tuple(value.records))
        self._details_revision = getattr(self, '_details_revision', 0) + 1
        while len(self._details) > 16:
            del self._details[next(iter(self._details))]
        self._error = ''
        self._detail_signature = None
        self.project()

    def current_detail(self):
        return self._cache_key() in self._details

    def detail_context(self):
        row = self.selection()
        value = self._details.get(self._cache_key())
        if row is None or value is None:
            return None
        payload, records = value
        return dict(
            payload=payload,
            records=records,
            character_id=row['character_id'],
            modal_id=row['modal_id'],
            view_token=self.view_token,
            mode=self.mode,
        )

    def _project_detail(self):
        row, context = self.selection(), self.detail_context()
        draft = self._drafts.get(self._draft_key())
        signature = (
            self.view_token,
            id(context['payload']) if context else None,
            id(draft),
            self.page.runtime.avatars.revision,
            getattr(self.page._current_loadouts, 'inventory_revision', None),
            getattr(self.page._current_loadouts, 'revision', None),
            self._error,
        )
        if signature == self._detail_signature:
            return
        self._detail_signature = signature
        self.status.setText(self._error)
        self.status.setVisible(bool(self._error))
        if row is None:
            self.title.setText('当前配装' if self.mode == 'current' else '备份配装')
            self.detail_cards.result_hint.hide()
            self.avatar.clear()
            text = (
                '正在更新配装列表…'
                if self._pending_saved_identity
                else '没有匹配的配装。'
                if self.search.text()
                else '暂无保存配装。'
            )
            self.empty_label.setText(text)
            self.empty_action_button.setVisible(
                not self.search.text() and not self._pending_saved_identity
            )
            self.stack.setCurrentWidget(self.empty)
            self.action_note.clear()
            return
        self.title.setText(row['label'])
        pixmap = lookup_avatar(self.page.runtime.avatars, row['character_id'])
        if pixmap is None:
            request = getattr(self.page.runtime.avatars, 'request', None)
            if request is not None:
                request(row['character_id'])
        self.avatar.setPixmap(
            pixmap.scaled(self.avatar.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if pixmap is not None
            else QPixmap()
        )
        if pixmap is None:
            self.avatar.setText(row['label'][:1])
        if context is None:
            self.detail_cards.result_hint.hide()
            self.empty_label.setText(
                '读取未完成，请重选左侧角色。' if self._error else '正在读取配装…'
            )
            self.empty_action_button.hide()
            self.stack.setCurrentWidget(self.empty)
            self.action_note.clear()
            return
        payload = context['payload']
        if draft is None:
            result, records, snapshot = (
                payload['result'],
                context['records'],
                payload['weight_snapshot'],
            )
        else:
            result, records, snapshot = draft.result.to_dict(), draft.records, draft.snapshot
        role = next(
            item
            for item in result['roles']
            if (item['character_id'], item['modal_id']) == (row['character_id'], row['modal_id'])
        )
        self.title.setText(role['label'])
        self._set_saved_role(role, records, snapshot, _identity(row))
        self.detail_cards.result_hint.show()
        self.stack.setCurrentWidget(self.detail_cards)
        self.action_note.setText(self.selection_note())

    def _card_selected(self, _identity):
        self._schedule_buttons()

    def _replace(self):
        context, identity = self.detail_context(), self.selected_item_id()
        if (
            self._busy()
            or context is None
            or not identity
            or self._drafts.get(self._draft_key()) is not None
        ):
            return
        row = next(
            item
            for item in context['payload']['result']['roles']
            if (item['character_id'], item['modal_id'])
            == (context['character_id'], context['modal_id'])
        )
        slot = row['item_ids'].index(identity)
        self.page.action(
            lambda: self.page._start_plan_job(
                'replacement-candidates',
                dict(
                    payload=context['payload'],
                    character_id=context['character_id'],
                    slot_index=slot,
                    view_token=self.view_token,
                    saved_role_view=True,
                ),
            )
        )

    def publish_candidates(self, options, token):
        if token != self.view_token:
            return
        if self._dialog is not None:
            self._dialog.close()
        self._replacement_options = options
        dialog = self._dialog = ReplacementDialog(
            options,
            self.window(),
            avatar_cache=self.page.runtime.avatars,
            queries=self.page.runtime.inventory_queries,
        )
        dialog.preview_requested.connect(
            lambda item_id: self.page.action(
                lambda: self.page._start_plan_job(
                    'preview-replacement',
                    dict(options=options, item_id=item_id, view_token=token, saved_role_view=True),
                )
            )
        )
        dialog.rejected.connect(lambda: self._replacement_cancelled(dialog, token))
        dialog.finished.connect(lambda _code: self._replacement_closed(dialog))
        dialog.open()

    def publish_preview(self, preview, token):
        if token != self.view_token or self._dialog is None:
            return
        self._drafts[self._draft_key()] = preview
        self._detail_signature = None
        self._dialog.accept()
        self.project()

    def _replacement_cancelled(self, dialog, token):
        if self._dialog is dialog and token == self.view_token and self._busy():
            self.page.cancel_job()

    def _replacement_closed(self, dialog):
        if self._dialog is dialog:
            self._dialog = None
            self._replacement_options = None
        dialog.deleteLater()
        self._sync_buttons()

    def _discard_preview(self):
        if not self._busy():
            self._drafts.pop(self._draft_key(), None)
            self._detail_signature = None
            self.project()

    def _save(self):
        context = self.detail_context()
        if not self._busy() and context is not None:
            self.page.action(
                lambda: self.page.save_saved_role(
                    context, draft=self._drafts.get(self._draft_key())
                )
            )

    def _backup(self):
        context = self.detail_context()
        if not self._busy() and context is not None and self.mode == 'history':
            self.page.action(
                lambda: self.page.backup_saved_role(
                    context, draft=self._drafts.get(self._draft_key())
                )
            )

    def _remove(self):
        context = self.detail_context()
        if not self._busy() and context is not None:
            self.page.action(
                lambda: (
                    self.page.change_current('remove-current')
                    if self.mode == 'current'
                    else self.page.saved_role_operation('remove-backup', context)
                )
            )

    def publish_backup(self, plan_id, character_id, modal_id, token):
        self.publish_saved(plan_id, character_id, modal_id, token, mode='history')

    def publish_saved(self, plan_id, character_id, modal_id, token, *, mode='current'):
        if token is not None:
            self._drafts.pop((token[1], token[2], token[3], token[4]), None)
        self._pending_saved_identity = (plan_id, character_id, modal_id)
        self.mode = mode
        self.tabs.setCurrentItem(self.mode)
        self._selection[mode] = self._pending_saved_identity
        self._generation += 1
        self._load_requested = None
        self._signature = self._detail_signature = None
        self._error = ''
        self.project(force=True)

    def show_error(self, message, token=None):
        if token is not None and token != self.view_token:
            return
        self._error = str(message)
        if self._dialog is not None:
            self._dialog.show_error(message)
        self._detail_signature = None
        self.project()

    def _lifecycle_active(self):
        p = self.page
        return not p._closing and not p.runtime._closed and not p.task._destroyed

    def stop_projection(self):
        try:
            for timer in (self._button_timer, self._verify_timer):
                if QThread.currentThread() == timer.thread():
                    timer.stop()
                else:
                    QMetaObject.invokeMethod(timer, 'stop', Qt.QueuedConnection)
        except RuntimeError:
            pass  # A destroyed QObject has already disposed its child timer.

    def _schedule_buttons(self):
        if self._lifecycle_active() and not self._button_timer.isActive():
            self._button_timer.start(0)

    def _sync_buttons(self):
        self._button_timer.stop()
        if not self._lifecycle_active():
            return
        busy, row = self._busy(), self.selection()
        dialog = self._dialog is not None
        for widget in (self.list, self.tabs, self.search, self.back_button):
            widget.setEnabled(not busy and not dialog)
        self.cancel_button.setVisible(busy and not dialog)
        self.cancel_button.setEnabled(busy)
        snapshot = self.page._current_loadouts
        revision = self.page.inventory_revision()
        verified = (
            snapshot is not None
            and revision is not None
            and snapshot.profile_id == self.page.profile.currentData()
            and snapshot.inventory_revision == revision
        )
        detail = self.current_detail()
        draft = self._drafts.get(self._draft_key())
        self.replace_button.setEnabled(
            verified
            and not busy
            and not dialog
            and detail
            and bool(self.selected_item_id())
            and draft is None
        )
        self.discard_button.setVisible(draft is not None)
        self.discard_button.setEnabled(not busy and not dialog)
        self.save_button.setEnabled(
            verified
            and not busy
            and not dialog
            and detail
            and self.detail_cards.model.rowCount() == 5
            and (self.mode == 'history' or draft is not None)
        )
        current = self.mode == 'current' and row is not None
        self.save_button.setText('更新当前配装' if self.mode == 'current' else '设为当前配装')
        self.backup_button.setVisible(self.mode == 'history' and draft is not None)
        self.backup_button.setEnabled(
            not busy and not dialog and bool(detail) and draft is not None
        )
        selected = self.protected_selection()
        self.release_button.setText(f'解除保护（{len(selected)}）' if selected else '解除保护')
        self.release_button.setVisible(current)
        self.remove_button.setVisible(row is not None)
        from echo_workshop.ui.allocation.action_labels import REMOVE_CURRENT_TIP, REMOVE_BACKUP_TIP

        self.remove_button.setToolTip(REMOVE_CURRENT_TIP if current else REMOVE_BACKUP_TIP)
        ready = verified and not busy and not dialog and draft is None
        self.remove_button.setEnabled(row is not None and bool(detail) and ready)
        self.release_button.setEnabled(current and ready and bool(selected))
        self.action_note.setText(self.selection_note())

    def _theme_changed(self, *_):
        if (
            not self.page._closing
            and not self.page.runtime._closed
            and not self.page.task._destroyed
        ):
            self._update_palette()
            self._detail_signature = None
            self.project()

    def _update_palette(self):
        palette = self.palette()
        surface = QColor('#202020' if isDarkTheme() else '#fafafa')
        foreground = QColor('#f4f4f4' if isDarkTheme() else '#202020')
        for role in (QPalette.Window, QPalette.Base):
            palette.setColor(role, surface)
        for role in (QPalette.WindowText, QPalette.Text):
            palette.setColor(role, foreground)
        accent = themeColor()
        palette.setColor(
            QPalette.Highlight, accent.darker(250) if isDarkTheme() else accent.lighter(180)
        )
        palette.setColor(QPalette.HighlightedText, foreground)
        for widget in (self, self.list, self.list.viewport()):
            widget.setPalette(palette)
            widget.setAutoFillBackground(True)

    def resizeEvent(self, event):
        narrow = self.width() < 800
        self.splitter.setOrientation(Qt.Vertical if narrow else Qt.Horizontal)
        self.sidebar.setMinimumWidth(0 if narrow else 220)
        self.sidebar.setMaximumWidth(16777215 if narrow else 300)
        self.sidebar.setMaximumHeight(270 if narrow else 16777215)
        if narrow and self.splitter.sizes()[0] > 270:
            self.splitter.setSizes([270, max(1, self.splitter.height() - 270)])
        super().resizeEvent(event)
