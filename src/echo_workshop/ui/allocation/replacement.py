# 展示已保存配装单槽候选及应用前的评分差异。
"""The application prepares frozen candidates; this dialog never saves them."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QVBoxLayout,
    QBoxLayout,
    QScrollArea,
    QFrame,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    TitleLabel,
    isDarkTheme,
    qconfig,
)

from echo_workshop.ui.widgets.layout import fit_dialog
from echo_workshop.ui.inventory.card_data import project_card
from echo_workshop.ui.inventory.cards import (
    EchoCardGrid,
    FOOTER_ROLE,
    RECORD_ROLE,
    SEARCH_ROLE,
    SORT_ROLE,
)
from echo_workshop.ui.widgets.rating import (
    FOOTER_RATING_ROLE,
    RatedCardDelegate,
    RatedEchoCardWidget,
    RatingValueLabel,
    apply_rating_colour,
)
from echo_workshop.ui.widgets.rating_style import score_text


def _score_text(cents, rating):
    return score_text(cents / 100, rating) if cents is not None else '评分不可用'


class _CandidateModel(QStandardItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.identities = {}

    def row_for_id(self, identity):
        return self.identities.get(identity, -1)


class ReplacementDialog(QDialog):
    """All same-COST candidates, followed by an explicit application preview."""

    preview_requested = Signal(str)

    def __init__(self, options, parent=None, *, avatar_cache=None, queries=None):
        super().__init__(parent)
        self.options = options
        self._queries = queries
        self._busy = False
        self._choices = {row.item_id: row for row in options.candidates}
        self._records = {record.item_id: record for record in options.records}
        self.setWindowTitle('替换优化')
        self.setObjectName('EchoReplacementDialog')
        self.setWindowModality(Qt.WindowModal)
        self.resize(1100, 820)
        self.setMinimumSize(760, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        heading = BodyLabel(f'{options.label} · 槽位 {options.slot_index + 1}', self)
        layout.addWidget(heading)
        note = CaptionLabel(f'同 COST {options.old_record.cost} 候选；其余四件保持不变。', self)
        layout.addWidget(note)
        comparison = self.comparison = QBoxLayout(QBoxLayout.LeftToRight)
        old = QVBoxLayout()
        old.addWidget(CaptionLabel('原声骸', self))
        self.old_card = RatedEchoCardWidget(self, show_owner_avatar=False)
        self.old_card.avatar_cache = avatar_cache
        self.old_card.set_record(
            options.old_record, score_text=_score_text(options.old_cents, options.old_rating)
        )
        self.old_card.set_rating(options.old_rating)
        self.old_card.setMinimumWidth(220)
        self.old_card.setMaximumWidth(290)
        old.addWidget(self.old_card)
        comparison.addLayout(old, 1)
        chosen = QVBoxLayout()
        chosen.addWidget(CaptionLabel('候选声骸', self))
        self.chosen_card = RatedEchoCardWidget(self, show_owner_avatar=False)
        self.chosen_card.avatar_cache = avatar_cache
        self.chosen_card.hint = '点击下方候选声骸'
        self.chosen_card.setMinimumWidth(220)
        self.chosen_card.setMaximumWidth(290)
        chosen.addWidget(self.chosen_card)
        comparison.addLayout(chosen, 1)
        detail = QVBoxLayout()
        detail.setContentsMargins(24, 24, 12, 0)
        detail.setSpacing(12)
        self.delta = TitleLabel(self)
        self.delta.setWordWrap(True)
        detail.addWidget(self.delta)
        self.original_total = BodyLabel(
            '原总分 ' + _score_text(options.source_total_cents, options.source_total_rating), self
        )
        self.original_total.setWordWrap(True)
        detail.addWidget(self.original_total)
        detail.addWidget(CaptionLabel('替换后总分', self))
        self.total_score = RatingValueLabel(parent=self)
        detail.addWidget(self.total_score)
        self.reason = CaptionLabel(self)
        self.reason.setWordWrap(True)
        detail.addWidget(self.reason)
        detail.addStretch(1)
        comparison.addLayout(detail, 1)
        self.comparison_scroll = QScrollArea(self)
        self.comparison_scroll.setObjectName('EchoReplacementScroll')
        self.comparison_scroll.viewport().setObjectName('EchoReplacementViewport')
        self.comparison_scroll.setWidgetResizable(True)
        self.comparison_scroll.setFrameShape(QFrame.NoFrame)
        self.comparison_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        comparison_body = QWidget(self.comparison_scroll)
        comparison_body.setObjectName('EchoReplacementComparison')
        comparison_body.setLayout(comparison)
        self.comparison_scroll.setWidget(comparison_body)
        layout.addWidget(self.comparison_scroll)
        search = QHBoxLayout()
        self.search = LineEdit(self)
        self.search.setPlaceholderText('搜索声骸、套装或词条')
        self.search.textChanged.connect(self._search)
        search.addWidget(self.search, 1)
        self.count_label = CaptionLabel(self)
        search.addWidget(self.count_label)
        self.all_button = PushButton('清空搜索', self)
        self.all_button.clicked.connect(self.search.clear)
        search.addWidget(self.all_button)
        excluded = sum(not row.available for row in options.candidates)
        self.unavailable_button = PushButton(f'显示不可用（{excluded}）', self)
        self.unavailable_button.setCheckable(True)
        self.unavailable_button.setVisible(bool(excluded))
        self.unavailable_button.toggled.connect(lambda _: self._populate())
        search.addWidget(self.unavailable_button)
        layout.addLayout(search)
        self.grid = EchoCardGrid(
            self, show_score_footer=True, show_owner_avatar=False, queries=self._queries
        )
        self.grid.avatar_cache = avatar_cache
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.delegate = RatedCardDelegate(self.grid)
        self.grid.setItemDelegate(self.grid.delegate)
        self.model = _CandidateModel(self.grid)
        self.grid.setModel(self.model)
        self.grid.selectionModel().currentChanged.connect(self._selected)
        layout.addWidget(self.grid, 1)
        self.error_note = CaptionLabel(self)
        self.error_note.setWordWrap(True)
        self.error_note.hide()
        layout.addWidget(self.error_note)
        footer = QHBoxLayout()
        footer.addWidget(
            CaptionLabel('应用仅预览；可另存备份，或明确设为当前配装后生效。', self), 1
        )
        self.cancel_button = PushButton('取消', self)
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        self.apply_button = PrimaryPushButton('应用预览', self)
        self.apply_button.clicked.connect(self._apply)
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        self._populate()
        qconfig.themeChanged.connect(self._update_theme)
        qconfig.themeChangedFinished.connect(self._update_theme)
        self._update_theme()
        fit_dialog(self, 1100, 820)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'comparison_scroll'):
            self.comparison.setDirection(
                QBoxLayout.TopToBottom if self.width() < 760 else QBoxLayout.LeftToRight
            )
            self.comparison_scroll.setMaximumHeight(max(120, min(370, (self.height() - 200) // 2)))

    def _update_theme(self, *_):
        surface = QColor('#202020' if isDarkTheme() else '#fafafa')
        foreground = QColor('#f4f4f4' if isDarkTheme() else '#202020')
        palette = self.palette()
        for role in (QPalette.Window, QPalette.Base):
            palette.setColor(role, surface)
        for role in (QPalette.WindowText, QPalette.Text):
            palette.setColor(role, foreground)
        for widget in (
            self,
            self.grid,
            self.grid.viewport(),
            self.comparison_scroll,
            self.comparison_scroll.viewport(),
            self.comparison_scroll.widget(),
        ):
            widget.setPalette(palette)
            widget.setAutoFillBackground(True)
        self.setStyleSheet(
            '#EchoReplacementDialog {background:' + surface.name() + ';}'
            '#EchoReplacementDialog QListView {background:' + surface.name() + ';border:none;}'
            '#EchoReplacementScroll, #EchoReplacementViewport, #EchoReplacementComparison '
            '{background:' + surface.name() + ';color:' + foreground.name() + ';}'
        )
        apply_rating_colour(self.original_total, self.options.source_total_rating)
        self._colour_delta()
        self.grid.viewport().update()

    def _colour_delta(self):
        choice = self.selected_candidate()
        value = choice.delta_cents if choice is not None else None
        colour = (
            ('#62d6a4' if isDarkTheme() else '#087b4b')
            if value is not None and value > 0
            else (
                ('#ff9292' if isDarkTheme() else '#c62828')
                if value is not None and value < 0
                else ('#d4d4d4' if isDarkTheme() else '#555555')
            )
        )
        self.delta.setStyleSheet('color: ' + colour + '; font-weight: 600;')

    def _populate(self):
        selected = self.grid.currentIndex().data(Qt.UserRole)
        self.model.removeRows(0, self.model.rowCount())
        self.model.identities.clear()
        for ordinal, choice in enumerate(self.options.candidates):
            record = self._records.get(choice.item_id)
            if record is None or (not choice.available and not self.unavailable_button.isChecked()):
                continue
            item = QStandardItem(record.name)
            item.setData(record.item_id, Qt.UserRole)
            item.setData((record, 'item', ()), RECORD_ROLE)
            item.setData(project_card(record).text, SEARCH_ROLE)
            item.setData(ordinal, SORT_ROLE)
            footer = _score_text(choice.current_cents, choice.item_rating)
            footer += (
                f' · {choice.delta_cents / 100:+.2f}'
                if choice.available and choice.delta_cents is not None
                else ' · 不可替换'
            )
            item.setData(footer, FOOTER_ROLE)
            item.setData(choice.item_rating, FOOTER_RATING_ROLE)
            self.model.identities[choice.item_id] = self.model.rowCount()
            self.model.appendRow(item)
        self._search(self.search.text())
        if selected in self.model.identities:
            self.grid.setCurrentIndex(
                self.grid.proxy.mapFromSource(self.model.index(self.model.identities[selected], 0))
            )
        self._selected(None, None)
        if not self.model.rowCount():
            self.reason.setText(
                '暂无可替换声骸，可查看不可用原因。'
                if self.options.candidates
                else '没有同 COST 的候选声骸。'
            )
        elif self.selected_candidate() is None:
            self.reason.setText('选择候选，查看评分变化。')

    def selected_candidate(self):
        return self._choices.get(self.grid.currentIndex().data(Qt.UserRole))

    def _selected(self, _index, _previous):
        choice = self.selected_candidate()
        record = self._records.get(choice.item_id) if choice is not None else None
        self.chosen_card.set_record(record)
        self.total_score.clear()
        self.delta.clear()
        self._colour_delta()
        if choice is None or record is None:
            self.apply_button.setEnabled(False)
            self.reason.setText('选择候选，查看评分变化。')
            return
        self.chosen_card.set_record(
            record, score_text=_score_text(choice.current_cents, choice.item_rating)
        )
        self.chosen_card.set_rating(choice.item_rating)
        if choice.total_cents is not None:
            self.total_score.set_value(choice.total_cents / 100, choice.total_rating)
        else:
            self.total_score.setText('评分不可用')
        self.delta.setText(
            f'变化 {choice.delta_cents / 100:+.2f} 分'
            if choice.delta_cents is not None
            else '变化 —'
        )
        self.reason.setText(choice.reason or '符合配装要求。')
        self.apply_button.setEnabled(choice.available and not self._busy)

    def _search(self, text):
        self.grid.search(text)
        shown = self.grid.proxy.rowCount()
        self.count_label.setText(f'{shown}/{self.model.rowCount()} 件候选')
        self.all_button.setEnabled(bool(text) and not self._busy)
        self._selected(None, None)

    def _apply(self):
        choice = self.selected_candidate()
        if not self._busy and choice is not None and choice.available:
            self.set_busy(True)
            self.preview_requested.emit(choice.item_id)

    def set_busy(self, busy):
        self._busy = bool(busy)
        for widget in (self.grid, self.search, self.all_button, self.unavailable_button):
            widget.setEnabled(not busy)
        self.all_button.setEnabled(bool(self.search.text()) and not busy)
        self.apply_button.setText('正在检查…' if busy else '应用预览')
        self.error_note.hide()
        self._selected(None, None)

    def show_error(self, message):
        self.set_busy(False)
        self.error_note.setText(str(message))
        self.error_note.show()
