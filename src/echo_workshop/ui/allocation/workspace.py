# 组织配装角色和装备要求工作区。
"""Small, read-only loadout overview; controllers own jobs and persistence."""

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon


def role_label(snapshot, key):
    try:
        return snapshot.template(*key)['name']
    except (KeyError, ValueError):
        return '失效角色 / 模版'


def avatar_icon(cache, character):
    """Use already available bytes only: selection/layout never requests a URL."""
    if cache is not None:
        data = cache.lookup(character)
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                return QIcon(pixmap)
    return FluentIcon.PEOPLE.icon()


class LoadoutPriorityEditor(QWidget):
    """Explicit solver priority navigation; never mutates requests itself."""

    selected = Signal(object)
    priority_changed = Signal(object, str)
    reordered = Signal(object)
    removed = Signal(object)
    managed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        from echo_workshop.ui.matching.picker import MatchRoleStrip

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.strip = MatchRoleStrip(self, priority_controls=True, auxiliary_controls=True)
        layout.addWidget(self.strip)
        self.buttons = self.strip.buttons
        self.current_key = None
        self._requested_characters = frozenset()
        self.strip.focused.connect(self.select)
        self.strip.priority_changed.connect(self.priority_changed)
        self.strip.reordered.connect(self.reordered)
        self.strip.removed.connect(self.removed)
        self.strip.managed.connect(self.managed)

    def set_entries(self, entries, snapshot, *, current=None, avatars=None, groups=()):
        entries = tuple((tuple(key), bool(pending)) for key, pending in entries)
        keys = tuple(key for key, _ in entries)
        characters = frozenset(key[0] for key in keys)
        if avatars is not None:
            for character in characters - self._requested_characters:
                avatars.request(character)
            self._requested_characters = characters
        levels = {tuple(key): ordinal for ordinal, group in enumerate(groups) for key in group}
        links = {
            key: ('>' if levels.get(key, 0) != levels.get(previous, 0) else '=')
            for previous, key in zip(keys, keys[1:])
        }
        labels = tuple(
            (key, role_label(snapshot, key) + ('（待确认）' if pending else ''))
            for key, pending in entries
        )
        if current not in keys:
            current = self.current_key if self.current_key in keys else keys[0] if keys else None
        self.current_key = current
        self.strip.set_roles(labels, current, links, avatars)
        self.buttons = self.strip.buttons
        self.setVisible(bool(keys))

    def select(self, key, *, emit=True):
        key = tuple(key) if key is not None else None
        if key is not None and key not in self.strip.buttons:
            return
        self.current_key = key
        for identity, button in self.strip.buttons.items():
            button.setChecked(identity == key)
        if emit and key is not None:
            self.selected.emit(key)

    def set_editable(self, editable):
        self.strip.set_reordering_enabled(editable)
        for button in (
            *self.strip.priority_buttons.values(),
            *self.strip.manage_buttons.values(),
            *self.strip.remove_buttons.values(),
        ):
            button.setEnabled(editable)


class EditorHost(QWidget):
    """The inline editor owns its scrolling and sticky save/cancel controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widget = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

    def setWidget(self, widget):
        self._widget = widget
        self.layout().addWidget(widget)
        widget.show()

    def takeWidget(self):
        widget, self._widget = self._widget, None
        if widget is not None:
            self.layout().removeWidget(widget)
            widget.hide()
            widget.setParent(None)
        return widget
