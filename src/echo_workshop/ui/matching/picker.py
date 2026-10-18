# 选择参与匹配的角色与模版。
"""Inline checkable inventory scope, UUID-based and virtualized for large pools."""

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QApplication,
    QGraphicsOpacityEffect,
)
from shiboken6 import isValid
from qfluentwidgets import PushButton, TogglePushButton, ToolButton, FluentIcon, isDarkTheme


class MatchRoleStrip(QScrollArea):
    """Stable role chips; only the avatar/name surface starts a reorder gesture."""

    focused = Signal(object)
    removed = Signal(object)
    managed = Signal(object)
    priority_changed = Signal(object, str)
    reordered = Signal(object)

    def __init__(self, parent=None, *, priority_controls=True, auxiliary_controls=True):
        super().__init__(parent)
        self._priority_controls = bool(priority_controls)
        self._auxiliary_controls = bool(auxiliary_controls)
        self._reordering_enabled = True
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body = QWidget()
        self.row = QHBoxLayout(self.body)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(6)
        self.setWidget(self.body)
        self.setFixedHeight(48)
        self.buttons = {}
        self.chips = {}
        self.remove_buttons = {}
        self.manage_buttons = {}
        self.priority_buttons = {}
        self._entries = None
        self._priorities = {}
        self._avatar_sources = {}
        self._avatar_revision = None
        self._press_key = None
        self._press_global = None
        self._dragging = False
        self._drag_target = None
        self._drag_order = None
        self._drag_ghost = None
        self._drag_offset = None
        self._drag_source = None
        self._drag_window = None
        self._drop_marker = QLabel(self.viewport())
        self._drop_marker.setObjectName('echoRoleDropMarker')
        self._drop_marker.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._drop_marker.setStyleSheet(
            'background: #63c8e2; border: 1px solid #276071; border-radius: 2px;'
        )
        self._drop_marker.hide()

    def set_roles(self, entries, current=None, priorities=None, avatar_cache=None):
        entries = tuple((tuple(key), label) for key, label in entries)
        priorities = dict(priorities or {})
        rebuilt = entries != self._entries
        if rebuilt:
            self._clear_drag()
            while self.row.count():
                item = self.row.takeAt(0)
                if item.widget():
                    item.widget().hide()
                    item.widget().deleteLater()
            self.buttons.clear()
            self.chips.clear()
            self.remove_buttons.clear()
            self.manage_buttons.clear()
            self.priority_buttons.clear()
            self._avatar_sources.clear()
            for ordinal, (key, label) in enumerate(entries):
                if ordinal and self._priority_controls:
                    relation = PushButton('=', self.body)
                    relation.setFixedSize(36, 32)
                    relation.setToolTip(
                        '点击切换：= 同级共同配装；> 前组优先配装。关系不改变单件评分。'
                    )
                    relation.clicked.connect(
                        lambda _checked=False, identity=key: self._toggle_priority(identity)
                    )
                    self.row.addWidget(relation)
                    self.priority_buttons[key] = relation
                chip = QWidget(self.body)
                chip.setObjectName('echoRoleStripChip')
                row = QHBoxLayout(chip)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(0)
                button = TogglePushButton(label, chip)
                button.setCheckable(True)
                button.setIconSize(QSize(24, 24))
                button.setToolTip('点击选择 ' + label + '；拖动头像或名称调整角色顺序')
                button.setCursor(Qt.OpenHandCursor)
                button._echo_role_key = key
                button.installEventFilter(self)
                button.clicked.connect(
                    lambda _checked=False, identity=key: self.focused.emit(identity)
                )
                remove = ToolButton(FluentIcon.CLOSE, chip)
                remove.setFixedSize(28, 32)
                remove.setToolTip('移除 ' + label + '；不删除声骸')
                remove.clicked.connect(
                    lambda _checked=False, identity=key: self.removed.emit(identity)
                )
                manage = ToolButton(FluentIcon.SETTING, chip)
                manage.setFixedSize(28, 32)
                manage.setToolTip('管理 ' + label + '：适配要求与计算选项')
                manage.clicked.connect(
                    lambda _checked=False, identity=key: self.managed.emit(identity)
                )
                row.addWidget(button)
                if self._auxiliary_controls:
                    row.addWidget(manage)
                    row.addWidget(remove)
                    self.manage_buttons[key] = manage
                    self.remove_buttons[key] = remove
                else:
                    manage.hide()
                    remove.hide()
                self.row.addWidget(chip)
                self.chips[key] = chip
                self.buttons[key] = button
            self.row.addStretch()
            self._entries = entries
        self._priorities = {key: priorities.get(key, '=') for key in self.priority_buttons}
        for key, button in self.priority_buttons.items():
            button.setText(self._priorities[key])
        revision = getattr(avatar_cache, 'revision', None)
        avatar_revision = avatar_cache, revision, isDarkTheme()
        theme_changed = (
            self._avatar_revision is None or self._avatar_revision[-1] != avatar_revision[-1]
        )
        refresh_avatars = rebuilt or revision is None or self._avatar_revision != avatar_revision
        for key, button in self.buttons.items():
            button.setChecked(key == current)
            if not refresh_avatars:
                continue
            # The owner schedules/cache-refreshes images. Reading chips never
            # queues IO/network, including repaint or a missing-image fallback.
            source = avatar_cache.lookup(key[0]) if avatar_cache is not None else None
            if (
                key not in self._avatar_sources
                or self._avatar_sources[key] != source
                or (source is None and theme_changed)
            ):
                pixmap = QPixmap()
                if isinstance(source, (bytes, bytearray)) and pixmap.loadFromData(source, 'PNG'):
                    button.setIcon(QIcon(pixmap))
                else:
                    button.setIcon(FluentIcon.PEOPLE.icon())
                self._avatar_sources[key] = source
        self._avatar_revision = avatar_revision
        self.setVisible(bool(entries))

    def set_reordering_enabled(self, enabled):
        self._reordering_enabled = bool(enabled)
        if not enabled:
            self._clear_drag()

    def _toggle_priority(self, key):
        if key not in self.priority_buttons:
            return
        value = '>' if self._priorities.get(key, '=') == '=' else '='
        self._priorities[key] = value
        self.priority_buttons[key].setText(value)
        self.priority_changed.emit(key, value)

    def _target_at(self, global_pos):
        local = self.viewport().mapFromGlobal(global_pos)
        if not self.viewport().rect().contains(local) or not self.buttons:
            return None
        return min(
            self.buttons,
            key=lambda key: abs(
                self.buttons[key].mapToGlobal(self.buttons[key].rect().center()).x()
                - global_pos.x()
            ),
        )

    def _drop_order(self, global_pos):
        target = self._target_at(global_pos)
        keys = [key for key, _ in self._entries or ()]
        if target is None or self._press_key not in keys:
            return tuple(keys)
        source, destination = keys.index(self._press_key), keys.index(target)
        keys.insert(destination, keys.pop(source))
        return tuple(keys)

    def _show_drag_target(self, global_pos):
        local = self.viewport().mapFromGlobal(global_pos)
        if self.viewport().rect().contains(local):
            bar = self.horizontalScrollBar()
            if local.x() < 24:
                bar.setValue(bar.value() - 12)
            elif local.x() > self.viewport().width() - 24:
                bar.setValue(bar.value() + 12)
        target = self._target_at(global_pos)
        self._drag_target = target
        self._drag_order = self._drop_order(global_pos)
        self._move_drag_ghost(global_pos)
        if target not in self.chips or target == self._press_key:
            self._drop_marker.hide()
            return
        keys = [key for key, _ in self._entries or ()]
        chip = self.chips[target]
        # Moving right inserts after the target; moving left inserts before it.
        # This is the same destination convention used by _drop_order, not a
        # nearest-widget highlight that implies a different eventual order.
        after = keys.index(self._press_key) < keys.index(target)
        boundary = chip.rect().topRight() if after else chip.rect().topLeft()
        point = self.viewport().mapFromGlobal(chip.mapToGlobal(boundary))
        x = point.x() + 3 if after else point.x() - 3
        y = max(0, point.y() - 3)
        self._drop_marker.setGeometry(
            max(0, min(x, self.viewport().width() - 4)),
            y,
            4,
            min(chip.height() + 6, self.viewport().height() - y),
        )
        self._drop_marker.show()
        self._drop_marker.raise_()

    def _begin_drag_feedback(self):
        chip = self.chips.get(self._press_key)
        if chip is None:
            return
        # grab() retains devicePixelRatio; QLabel uses the chip's logical size.
        # A child of the owning window can follow the pointer past the strip
        # without clipping or a QDrag.exec() nested event loop.
        pixmap = chip.grab()
        self._drag_window = self.window()
        ghost = self._drag_ghost = QLabel(self._drag_window)
        ghost.setObjectName('echoRoleDragGhost')
        ghost.setAttribute(Qt.WA_TransparentForMouseEvents)
        ghost.setAttribute(Qt.WA_NoSystemBackground)
        ghost.setPixmap(pixmap)
        ghost.resize(chip.size())
        opacity = QGraphicsOpacityEffect(ghost)
        opacity.setOpacity(0.68)
        ghost.setGraphicsEffect(opacity)
        self._drag_offset = chip.mapFromGlobal(self._press_global)
        self._drag_source = chip
        fade = QGraphicsOpacityEffect(chip)
        fade.setOpacity(0.28)
        chip.setGraphicsEffect(fade)  # Keep the original layout slot as a placeholder.
        self.destroyed.connect(ghost.deleteLater)
        if self._drag_window is not self:
            self._drag_window.installEventFilter(self)
        ghost.show()
        ghost.raise_()

    def _move_drag_ghost(self, global_pos):
        if self._drag_ghost is not None and isValid(self._drag_ghost):
            self._drag_ghost.move(self._drag_window.mapFromGlobal(global_pos) - self._drag_offset)
            self._drag_ghost.raise_()

    def _clear_drag(self):
        if self._dragging and self._press_key in self.buttons:
            pressed = self.buttons[self._press_key]
            pressed.setDown(False)
            pressed.isPressed = False
            pressed.update()
        if self._drag_source is not None and isValid(self._drag_source):
            self._drag_source.setGraphicsEffect(None)
        if self._drag_ghost is not None and isValid(self._drag_ghost):
            self._drag_ghost.hide()
            self._drag_ghost.deleteLater()
        if (
            self._drag_window is not None
            and self._drag_window is not self
            and isValid(self._drag_window)
        ):
            self._drag_window.removeEventFilter(self)
        self._drop_marker.hide()
        for button in self.buttons.values():
            button.setCursor(Qt.OpenHandCursor)
        self._press_key = self._press_global = self._drag_target = None
        self._drag_order = self._drag_ghost = self._drag_offset = self._drag_source = (
            self._drag_window
        ) = None
        self._dragging = False

    def hideEvent(self, event):
        if hasattr(self, '_drop_marker'):
            self._clear_drag()
        super().hideEvent(event)

    def resizeEvent(self, event):
        if getattr(self, '_dragging', False):
            self._clear_drag()
        super().resizeEvent(event)

    def eventFilter(self, watched, event):
        if watched is getattr(self, '_drag_window', None) and event.type() in (
            QEvent.Hide,
            QEvent.Close,
            QEvent.WindowDeactivate,
        ):
            self._clear_drag()
        key = getattr(watched, '_echo_role_key', None)
        if (
            key is None
            or key not in getattr(self, 'buttons', {})
            or self.buttons[key] is not watched
        ):
            return super().eventFilter(watched, event)
        kind = event.type()
        if not self._reordering_enabled:
            return super().eventFilter(watched, event)
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._clear_drag()
            self._press_key, self._press_global = key, event.globalPosition().toPoint()
        elif (
            kind == QEvent.MouseMove and self._press_key == key and event.buttons() & Qt.LeftButton
        ):
            pos = event.globalPosition().toPoint()
            if (
                self._dragging
                or (pos - self._press_global).manhattanLength() >= QApplication.startDragDistance()
            ):
                first_move = not self._dragging
                self._dragging = True
                watched.setDown(False)
                watched.isPressed = False
                watched.update()
                watched.setCursor(Qt.ClosedHandCursor)
                if first_move:
                    self._begin_drag_feedback()
                self._show_drag_target(pos)
                event.accept()
                return True
        elif (
            kind == QEvent.MouseButtonRelease
            and event.button() == Qt.LeftButton
            and self._press_key == key
        ):
            if self._dragging:
                order = self._drop_order(event.globalPosition().toPoint())
                original = tuple(identity for identity, _ in self._entries or ())
                self._clear_drag()
                event.accept()
                if order != original:
                    # The owner commits/rejects the complete stable-ID order;
                    # chips never mutate score selections or preferences here.
                    self.reordered.emit(order)
                return True
            self._clear_drag()
        elif kind in (QEvent.Hide, QEvent.WindowDeactivate) and self._press_key == key:
            self._clear_drag()
        return super().eventFilter(watched, event)
