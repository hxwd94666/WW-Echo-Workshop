# 连续展示所有角色的冻结配装分组并统一滚动和单选。
"""Presentation only: original role rows, scores, records and weight snapshot."""

from math import ceil

from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, PushButton, isDarkTheme, qconfig

from echo_workshop.ui.inventory.cards import EchoCardGrid
from echo_workshop.ui.widgets.rating import lookup_avatar
from echo_workshop.ui.widgets.workspace import LoadoutCards


def _key(row):
    return str(row['character_id']), str(row['modal_id'])


def _snapshot_identity(snapshot):
    version = getattr(snapshot, 'version', None)
    if version is None and isinstance(snapshot, dict):
        version = snapshot.get('version')
    return version if version is not None else id(snapshot)


def _row_identity(row):
    return (
        _key(row),
        row.get('label'),
        row.get('total_cents'),
        row.get('target_cents'),
        tuple(row.get('item_ids', ())),
        tuple(row.get('scores', ())),
    )


class _FullHeightGrid(EchoCardGrid):
    """The outer results area scrolls; card columns adapt within readable bounds."""

    def _resize_grid(self):
        super()._resize_grid()
        width = max(1, self.viewport().width() - 2)
        count = self.model().rowCount() if self.model() is not None else 5
        self.columns = max(1, min(max(1, count), width // 220))
        size = self.gridSize()
        size.setWidth(max(220, min(240, width // self.columns)))
        self.setGridSize(size)


class RoleResultGroup(QWidget):
    """One frozen role heading and the shared five-card presentation."""

    def __init__(self, key, parent=None, *, panel_service=None):
        super().__init__(parent)
        self.key = key
        self.panel_service = panel_service
        self.signature = None
        self._avatar_label = ''
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 12)
        layout.setSpacing(6)
        self.header = QWidget(self)
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.avatar = QLabel(self.header)
        self.avatar.setFixedSize(30, 30)
        self.avatar.setAlignment(Qt.AlignCenter)
        header.addWidget(self.avatar)
        self.heading = CaptionLabel(self.header)
        self.heading.setWordWrap(True)
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        header.addWidget(self.heading)
        self.loadout = LoadoutCards(
            self, grid_type=_FullHeightGrid, summary_layout=header, panel_service=panel_service
        )
        self.changes_button = PushButton('变动', self.header)
        self.changes_button.setEnabled(False)
        header.addWidget(self.changes_button)
        self.replace_button = PushButton('替换优化', self.header)
        self.replace_button.setEnabled(False)
        header.addStretch(1)
        header.addWidget(self.replace_button)
        from echo_workshop.ui.allocation.action_labels import explain, APPLY_TIP, BACKUP_TIP

        self.backup_button = PushButton('备份方案', self.header)
        self.save_button = PushButton('设为当前配装', self.header)
        explain(self.backup_button, BACKUP_TIP)
        explain(self.save_button, APPLY_TIP)
        header.addWidget(self.backup_button)
        header.addWidget(self.save_button)
        layout.addWidget(self.header)
        self.loadout.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.loadout)
        self.grid, self.model = self.loadout.cards, self.loadout.model
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setAutoScroll(False)
        self.grid.setMinimumWidth(222)
        self.grid.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.grid.viewport().removeEventFilter(self.grid.scroll_delegate)

    @property
    def rating(self):
        return self.loadout.rating

    def refresh_avatar(self, cache):
        pixmap = lookup_avatar(cache, self.key[0])
        if pixmap is None and cache is not None:
            request = getattr(cache, 'request', None)
            if request is not None:
                request(self.key[0])
        if pixmap is not None:
            self.avatar.setStyleSheet('')
            self.avatar.setPixmap(
                pixmap.scaled(self.avatar.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self.avatar.setPixmap(QPixmap())
            self.avatar.setText(self._avatar_label[:1] or '角')
            self.avatar.setStyleSheet(
                'border-radius: 15px; background: '
                + ('#454545; color: #eeeeee;' if isDarkTheme() else '#e8e8e8; color: #555555;')
            )

    def set_role(self, row, records, completed, snapshot, request_role=None):
        signature = (
            _row_identity(row),
            tuple(records),
            frozenset(completed),
            _snapshot_identity(snapshot),
        )
        if signature == self.signature:
            return
        self.signature = signature
        self._avatar_label = str(row.get('label', ''))
        self.heading.setText(self._avatar_label)
        self.loadout.set_role(row, records, completed, snapshot=snapshot, request_role=request_role)

    def expand_grid(self):
        self.grid._resize_grid()
        rows = ceil(self.model.rowCount() / self.grid.columns)
        height = rows * self.grid.gridSize().height() + 2 * self.grid.frameWidth() + 4
        self.grid.setFixedHeight(height if rows else 0)
        self.grid.setVisible(bool(rows))
        self.grid.doItemsLayout()
        self.grid.verticalScrollBar().setValue(0)
        self.grid.horizontalScrollBar().setValue(0)
        self.layout().activate()


class GroupedLoadoutResults(QScrollArea):
    """All roles remain visible; focus never filters or recomputes a result."""

    selected = Signal(str)
    role_selected = Signal(tuple)
    backup_requested = Signal(tuple)
    save_requested = Signal(tuple)
    changes_requested = Signal(tuple)
    replace_requested = Signal(tuple, str)

    def __init__(self, parent=None, *, avatar_cache=None, panel_service=None):
        super().__init__(parent)
        self._panel_service = panel_service
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.groups = {}
        self._avatar_cache = avatar_cache
        self._avatar_revision = None
        self._signature = None
        self._protection_snapshot = None
        self._selected_id = ''
        self._selected_group = None
        self._updating = False
        self._laying_out = False
        self._wheel_targets = {}
        self._header_targets = {}
        self._pending_anchor = None
        self._pending_focus = None
        self._scroll_serial = 0
        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 0, 4, 0)
        self.body_layout.setSpacing(8)
        self.empty_label = CaptionLabel('尚未生成方案', self.body)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.body_layout.addWidget(self.empty_label)
        self.body_layout.addStretch(1)
        self.setWidget(self.body)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._relayout)
        self.viewport().installEventFilter(self)
        self.verticalScrollBar().sliderPressed.connect(self._user_scrolled)
        qconfig.themeChangedFinished.connect(self.update_theme)
        qconfig.themeChanged.connect(self.update_theme)
        self.update_theme()

    @property
    def avatar_cache(self):
        return self._avatar_cache

    @avatar_cache.setter
    def avatar_cache(self, cache):
        self._avatar_cache = cache
        self.refresh_avatars(force=True)

    @property
    def selected_id(self):
        return self._selected_id

    def refresh_avatars(self, *, force=False):
        revision = (id(self._avatar_cache), getattr(self._avatar_cache, 'revision', None))
        if not force and revision == self._avatar_revision:
            return
        self._avatar_revision = revision
        for group in self.groups.values():
            group.grid.avatar_cache = self._avatar_cache
            group.refresh_avatar(self._avatar_cache)
            group.grid.viewport().update()

    def update_theme(self, *_):
        palette = self.palette()
        surface = QColor('#202020' if isDarkTheme() else '#f9f9f9')
        for role in (QPalette.Window, QPalette.Base):
            palette.setColor(role, surface)
        palette.setColor(QPalette.WindowText, QColor('#f4f4f4' if isDarkTheme() else '#202020'))
        self.setPalette(palette)
        self.viewport().setPalette(palette)
        self.body.setPalette(palette)
        self.body.setAutoFillBackground(True)
        self.refresh_avatars(force=True)
        self._queue_layout()

    def set_actions_enabled(self, *, backup, save):
        for group in self.groups.values():
            group.backup_button.setEnabled(backup)
            group.save_button.setEnabled(save and group.model.rowCount() == 5)

    def set_protection(self, snapshot):
        self._protection_snapshot = snapshot
        for group in self.groups.values():
            group.loadout.set_protection(snapshot)

    def set_changes_enabled(self, keys, *, enabled):
        keys = frozenset(tuple(key) for key in keys)
        for key, group in self.groups.items():
            group.changes_button.setEnabled(enabled and key in keys)

    def _register_group(self, group):
        group.changes_button.clicked.connect(
            lambda checked=False, key=group.key: self.changes_requested.emit(key)
        )
        group.replace_button.clicked.connect(
            lambda checked=False, key=group.key: self.replace_requested.emit(
                key, self._selected_id if self._selected_group == key else ''
            )
        )
        group.backup_button.clicked.connect(lambda: self.backup_requested.emit(group.key))
        group.save_button.clicked.connect(lambda: self.save_requested.emit(group.key))
        group.loadout.selected.connect(
            lambda identity, key=group.key: self._selection_changed(key, identity)
        )
        group.grid.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected, key=group.key: self._selection_cleared(key)
        )
        for target in (group.grid, group.grid.viewport()):
            target.installEventFilter(self)
            self._wheel_targets[target] = group.grid
        for target in (group.header, group.avatar, group.heading):
            target.installEventFilter(self)
            target.setCursor(Qt.PointingHandCursor)
            self._header_targets[target] = group.key

    def _remove_group(self, group):
        for targets in (self._wheel_targets, self._header_targets):
            for target in tuple(targets):
                if (
                    target is group.grid
                    or target is group.grid.viewport()
                    or targets[target] == group.key
                ):
                    target.removeEventFilter(self)
                    del targets[target]
        self.body_layout.removeWidget(group)
        group.deleteLater()

    def set_result(self, result, records=(), completed=(), *, snapshot=None):
        rows = tuple(result.get('roles', ())) if result is not None else ()
        records = tuple(records)
        completed = frozenset(completed)
        needed = {identity for row in rows for identity in row.get('item_ids', ())}
        record_map = {record.item_id: record for record in records if record.item_id in needed}
        request_roles = (
            {
                (role.get('character_id'), role.get('modal_id')): role
                for role in result.get('request', {}).get('roles', [])
                if isinstance(role, dict)
            }
            if result is not None
            else {}
        )
        signature = (
            tuple(_row_identity(row) for row in rows),
            tuple(sorted(record_map.items())),
            completed,
            _snapshot_identity(snapshot),
        )
        self.refresh_avatars()
        if signature == self._signature:
            self.selected.emit(self._selected_id)
            return
        self._signature = signature
        anchor = self._capture_anchor()
        previous = self._selected_id
        self._updating = True
        try:
            wanted = {_key(row) for row in rows}
            for key in tuple(self.groups):
                if key not in wanted:
                    self._remove_group(self.groups.pop(key))
            ordered = {}
            for index, row in enumerate(rows, 1):
                key = _key(row)
                group = self.groups.get(key)
                if group is None:
                    group = RoleResultGroup(key, self.body, panel_service=self._panel_service)
                    self._register_group(group)
                self.body_layout.removeWidget(group)
                self.body_layout.insertWidget(index, group)
                role_records = tuple(
                    record_map[identity]
                    for identity in row.get('item_ids', ())
                    if identity in record_map
                )
                group.set_role(
                    row,
                    role_records,
                    completed,
                    snapshot,
                    request_role=request_roles.get(key),
                )
                group.refresh_avatar(self._avatar_cache)
                group.grid.avatar_cache = self._avatar_cache
                ordered[key] = group
            self.groups = ordered
            self.empty_label.setVisible(not rows)
            self._restore_selection(previous)
            self._sync_replace_buttons()
        finally:
            self._updating = False
        # The controller may have rebuilt its checklist in a different order.
        # Re-align its current row by UUID even when this view kept the highlight.
        self.selected.emit(self._selected_id)
        self.set_protection(self._protection_snapshot)
        self._queue_layout(anchor)

    def _selection_changed(self, key, identity):
        if self._updating:
            return
        if not identity:
            if self._selected_group == key:
                self.clear_selection()
            return
        self._updating = True
        try:
            for other_key, group in self.groups.items():
                if other_key != key:
                    self._clear_grid(group.grid)
            self._selected_id, self._selected_group = identity, key
        finally:
            self._updating = False
        self._sync_replace_buttons()
        self.selected.emit(identity)
        self.role_selected.emit(key)

    def _selection_cleared(self, key):
        group = self.groups.get(key)
        if (
            not self._updating
            and self._selected_group == key
            and group is not None
            and not group.grid.selectionModel().hasSelection()
        ):
            self.clear_selection()

    @staticmethod
    def _clear_grid(grid):
        selection = grid.selectionModel()
        blocked = selection.blockSignals(True)
        try:
            selection.clearSelection()
            grid.setCurrentIndex(QModelIndex())
        finally:
            selection.blockSignals(blocked)

    def clear_selection(self):
        previous = self._selected_id
        for group in self.groups.values():
            self._clear_grid(group.grid)
        self._selected_id, self._selected_group = '', None
        self._sync_replace_buttons()
        if previous and not self._updating:
            self.selected.emit('')

    def _sync_replace_buttons(self):
        for key, group in self.groups.items():
            group.replace_button.setEnabled(bool(self._selected_id) and self._selected_group == key)

    def _restore_selection(self, identity):
        self.clear_selection()
        if not identity:
            return
        for key, group in self.groups.items():
            for row in range(group.model.rowCount()):
                if group.model.index(row, 0).data(Qt.UserRole) != identity:
                    continue
                index = group.grid.proxy.mapFromSource(group.model.index(row, 0))
                selection = group.grid.selectionModel()
                blocked = selection.blockSignals(True)
                try:
                    selection.setCurrentIndex(index, QItemSelectionModel.ClearAndSelect)
                finally:
                    selection.blockSignals(blocked)
                self._selected_id, self._selected_group = identity, key
                return

    def _capture_anchor(self):
        scroll = self.verticalScrollBar().value()
        for key, group in self.groups.items():
            if group.y() + group.height() > scroll:
                return key, scroll - group.y(), scroll, self._scroll_serial
        return None, 0, scroll, self._scroll_serial

    def _queue_layout(self, anchor=None):
        if self._laying_out:
            return
        if self._pending_anchor is None:
            self._pending_anchor = anchor or self._capture_anchor()
        self._layout_timer.start(0)

    def _relayout(self):
        anchor, self._pending_anchor = self._pending_anchor, None
        self._laying_out = True
        try:
            self.body_layout.activate()
            for group in self.groups.values():
                group.expand_grid()
            self.body_layout.activate()
            self.body.setMinimumHeight(self.body_layout.minimumSize().height())
            self.body_layout.activate()
        finally:
            self._laying_out = False
        QTimer.singleShot(0, self, lambda: self._restore_anchor(anchor))

    def _restore_anchor(self, anchor):
        if self._pending_focus is not None:
            key, self._pending_focus = self._pending_focus, None
            group = self.groups.get(key)
            if group is not None:
                self.verticalScrollBar().setValue(group.y())
            return
        if anchor is None or anchor[3] != self._scroll_serial:
            return
        key, offset, previous, _serial = anchor
        group = self.groups.get(key)
        self.verticalScrollBar().setValue(group.y() + offset if group is not None else previous)

    def scroll_to_role(self, key):
        if isinstance(key, str):
            key = next((value for value in self.groups if value[0] == key), None)
        elif key is not None:
            key = tuple(key)
        if key not in self.groups:
            return False
        self._user_scrolled()
        self._pending_focus = key
        self._queue_layout()
        return True

    def focus_role(self, key):
        return self.scroll_to_role(key)

    def _user_scrolled(self):
        self._scroll_serial += 1

    def eventFilter(self, obj, event):
        if obj in self._wheel_targets and event.type() == QEvent.Wheel:
            grid = self._wheel_targets[obj]
            pixels = event.pixelDelta().y()
            angle = event.angleDelta().y()
            if pixels or angle:
                self._user_scrolled()
                amount = pixels if pixels else angle / 120 * max(40, grid.gridSize().height() // 2)
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(amount))
                event.accept()
                return True
        if (
            obj in self._header_targets
            and event.type() == QEvent.MouseButtonRelease
            and event.button() == Qt.LeftButton
        ):
            self.role_selected.emit(self._header_targets[obj])
        if event.type() == QEvent.Resize and (obj is self.viewport() or obj in self._wheel_targets):
            self._queue_layout()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        anchor = self._capture_anchor()
        super().resizeEvent(event)
        self._queue_layout(anchor)
