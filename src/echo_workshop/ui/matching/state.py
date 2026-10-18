# 保存匹配参与角色候选范围和独立筛选草稿。
from dataclasses import dataclass, field
from PySide6.QtCore import QObject, Signal
from echo_workshop.application.pins import EMPTY_PIN_CATALOG


@dataclass
class MatchingState:
    roles: tuple = ()
    current_role: tuple | None = None
    selected_identity: tuple | None = None
    filters: dict = field(
        default_factory=lambda: dict(
            text='', cost=None, mode='confirmed', fully_tuned=False, max_age_days=0
        )
    )


class CandidateScope(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ids = None
        self.catalog = ()
        self.fixed_catalog = EMPTY_PIN_CATALOG

    def selected_ids(self):
        return self.ids

    def set_selection(self, ids):
        self.ids = None if ids is None else frozenset(ids)
        self.changed.emit()

    def reload(self, records, *, fixed_catalog):
        self.catalog = tuple(records)
        self.fixed_catalog = fixed_catalog

    def reset_profile(self):
        self.ids, self.catalog = None, ()
        self.fixed_catalog = EMPTY_PIN_CATALOG


class RuleText(QObject):
    textChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = ''

    def text(self):
        return self.value

    def setText(self, text):
        text = str(text)
        if text != self.value:
            self.value = text
            self.textChanged.emit(text)

    def clear(self):
        self.setText('')
