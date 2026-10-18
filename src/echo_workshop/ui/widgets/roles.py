# 提供角色模版检索与选择。
"""Two-level explicit character/modal picker; no score-based selection."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QWidget
from echo_workshop.ui.widgets.inputs import QComboBox


class IdentityCombo(QComboBox):
    """Qt may compare opaque Python tuples by identity; IDs need value equality."""

    def findData(self, data, role=Qt.UserRole, flags=Qt.MatchExactly | Qt.MatchCaseSensitive):
        if isinstance(data, (tuple, list)):
            return next(
                (
                    index
                    for index in range(self.count())
                    if isinstance(self.itemData(index, role), (tuple, list))
                    and tuple(self.itemData(index, role)) == tuple(data)
                ),
                -1,
            )
        return super().findData(data, role, flags)


class RolePicker(QWidget):
    changed = Signal()

    def __init__(self, snapshot, parent=None, key=None):
        super().__init__(parent)
        self.characters, self.modals = QComboBox(), IdentityCombo()
        self.characters.setEditable(True)
        self.characters.setInsertPolicy(QComboBox.NoInsert)
        self.characters.completer().setFilterMode(Qt.MatchContains)
        self.characters.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addRow('角色（可搜索）', self.characters)
        layout.addRow('模版', self.modals)
        layout.addRow(self.hint)
        self._loading = False
        self.characters.currentTextChanged.connect(self._character_changed)
        self.modals.currentIndexChanged.connect(self._modal_changed)
        self.reload(snapshot, key)

    def reload(self, snapshot, key=None):
        self._loading = True
        self.snapshot = snapshot
        self.characters.clear()
        names = {}
        for label, (character, _, _) in snapshot.options.items():
            names.setdefault(character, label.split('-', 1)[0])
        # Names are labels only. Duplicate display names still carry explicit IDs.
        for character, name in names.items():
            display = name if list(names.values()).count(name) == 1 else f'{name}（{character}）'
            self.characters.addItem(display, character)
        index = self.characters.findData(key[0]) if key else -1
        if key and index < 0:
            self.characters.addItem('失效角色：' + key[0], key[0])
            index = self.characters.count() - 1
        self.characters.setCurrentIndex(index)
        self._load_modals(key[1] if key else None, restoring=True)
        self._loading = False

    def key(self):
        # Editable search text is not an implicit selection of the previous role.
        if (
            self.characters.currentIndex() < 0
            or self.characters.currentText()
            != self.characters.itemText(self.characters.currentIndex())
        ):
            return None
        return self.modals.currentData()

    def _load_modals(self, preserve=None, *, restoring=False):
        self.modals.blockSignals(True)
        self.modals.clear()
        char = self.characters.currentData()
        exact = (
            self.characters.currentIndex() >= 0
            and self.characters.currentText()
            == self.characters.itemText(self.characters.currentIndex())
        )
        if exact:
            for label, (character, modal, _) in self.snapshot.options.items():
                if character == char:
                    self.modals.addItem(label.split('-', 1)[-1], (character, modal))
        index = self.modals.findData((char, preserve)) if preserve else -1
        if preserve and index < 0:
            self.modals.addItem('失效模版：' + preserve, (char, preserve))
            index = self.modals.count() - 1
        elif preserve is None and not restoring:
            index = self.modals.findData((char, 'default'))
        self.modals.setCurrentIndex(index)
        self.modals.blockSignals(False)
        self._update_hint()

    def _update_hint(self):
        key = self.key()
        valid = (
            key is not None
            and key[0] in self.snapshot.templates
            and key[1] in self.snapshot.templates[key[0]]
        )
        self.hint.setText('已选择此模版，可按实际装备更换。' if valid else '请选择角色和模版。')
        self.hint.setToolTip('按你选择的模版评分，不会自动切到分数最高的模版。')

    def _character_changed(self, *_):
        if not self._loading:
            self._load_modals()
            self.changed.emit()

    def _modal_changed(self, *_):
        if not self._loading:
            self._update_hint()
            self.changed.emit()
