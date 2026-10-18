# 展示声骸对各角色的当前分和评级。
"""Inventory-style score cards projected from an already computed matrix.

One physical Echo shows one or all selected roles. Switching view, sorting, painting, and restoring
instance selection never reads inventory, runs scoring, or schedules downloads.
"""

import math
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, PushButton, isDarkTheme, qconfig

from echo_workshop.ui.inventory.card_data import DAMAGE_LABELS
from echo_workshop.ui.inventory.cards import (
    EchoCardDelegate,
    EchoCardGrid,
    RECORD_ROLE,
    SEARCH_ROLE,
    SORT_ROLE,
    FOOTER_ROLE,
    RESERVATION_ROLE,
    paint_card,
    stat_label_font,
)
from echo_workshop.ui.widgets.roles import IdentityCombo
from echo_workshop.ui.widgets.rating_style import score_text, rating_colour
from echo_workshop.ui.widgets.rating import lookup_avatar

SCORE_ROLE = int(Qt.UserRole) + 5
_CELL_AVAILABLE = (True, True, False, False, False)
_CELL_REASON = ('', '条件未知', '不可评分', '条件不符', '槽位要求不符')


@dataclass(frozen=True)
class RoleScoreRow:
    key: tuple
    label: str
    current: float | None
    potential: float | None
    available: bool
    reason: str = ''
    rating: object = None
    weight_version: str = ''

    def text(self):
        if not self.available:
            return self.label + '：不参与（' + self.reason + '）'
        return self.label + '：' + score_text(self.current, self.rating)


def _number(value):
    try:
        return float(value) if math.isfinite(float(value)) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _footer(score):
    values = getattr(score, 'score', None)
    current = _number(getattr(values, 'current_score', None))
    rating = getattr(values, 'rating', None)
    title = (
        score_text(current, rating)
        if current is not None
        else '待核对 / 不评分'
        if values is None
        else '评分无效，请重新计算'
    )
    return (title,)


class _LookupOnlyAvatar:
    def __init__(self, cache):
        self.cache = cache

    def request(self, _identity):
        return False  # Shared inventory painter may ask; result paint is IO-free.

    def lookup(self, identity):
        return self.cache.lookup(identity)

    @property
    def revision(self):
        return getattr(self.cache, 'revision', None)


class MatchCardSource(QAbstractListModel):
    """One lazy cell per actual instance, never per Cartesian score pair."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.records, self.scores = (), (), ()
        self._ids = {}
        self._comparisons = {}
        self._search_cache = {}
        self.claims = {}

    def reset_entries(self, entries=(), *, comparisons=None, claims=None):
        self.beginResetModel()
        entries = tuple(entries)
        self.rows = tuple((record, 'item', score) for record, score in entries)
        self.records = tuple(record for record, _ in entries)
        self.scores = tuple(score for _, score in entries)
        self._ids = {record.item_id: i for i, record in enumerate(self.records)}
        self._comparisons = dict(comparisons or {})
        self.claims = dict(claims or {})
        self._search_cache.clear()
        self.endResetModel()

    def row_for_id(self, identity):
        return self._ids.get(identity, -1)

    def comparison_rows(self, index):
        row = index.row() if isinstance(index, QModelIndex) else index
        return (
            self._comparisons.get(self.records[row].item_id, ())
            if 0 <= row < len(self.records)
            else ()
        )

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def filter_record(self, row):
        return self.records[row], 'item'

    def search_text(self, row):
        if row not in self._search_cache:
            record = self.records[row]
            self._search_cache[row] = ' '.join(
                (
                    record.name,
                    record.sonata,
                    record.owner,
                    *(
                        DAMAGE_LABELS.get(stat.stat_name, stat.stat_name)
                        for stat in record.main_stats + record.sub_stats
                    ),
                )
            )
        return self._search_cache[row]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        record, kind, score = self.rows[index.row()]
        if role == Qt.UserRole:
            return record.item_id
        if role == Qt.ToolTipRole:
            return ''
        if role == RECORD_ROLE:
            return record, kind, score
        if role == SCORE_ROLE:
            return score
        if role == RESERVATION_ROLE:
            return self.claims.get(record.item_id, '')
        if role == FOOTER_ROLE:
            rows = self.comparison_rows(index)
            if len(rows) > 1:
                return '\n'.join(row.text() for row in rows)
            return '\n'.join(_footer(score))
        if role == SORT_ROLE:
            return index.row()  # Proxy always preserves our descending score order.
        if role == SEARCH_ROLE:
            return self.search_text(index.row())
        if role == Qt.DisplayRole:
            return record.name


class MatchScoreDelegate(EchoCardDelegate):
    def paint(self, painter, option, index):
        data = self.projection(index)
        if data is None:
            return
        self.paint_count += 1
        area = option.rect.adjusted(5, 5, -5, -5)
        paint_card(
            painter,
            area,
            data,
            option.font,
            bool(option.state & QStyle.State_Selected),
            avatar=(
                lookup_avatar(getattr(self.parent(), 'avatar_cache', None), data.owner_character_id)
                if getattr(self.parent(), 'show_owner_avatar', True)
                else None
            ),
            show_owner_avatar=getattr(self.parent(), 'show_owner_avatar', True),
        )
        inside = area.adjusted(14, 10, -14, -10)
        line = QFontMetrics(option.font).height() + 8
        source = (
            index.model().mapToSource(index) if hasattr(index.model(), 'mapToSource') else index
        )
        rows = source.model().comparison_rows(source)
        count = len(rows) if rows else 1
        top = inside.bottom() - count * line + 1
        painter.save()
        painter.setFont(option.font)
        if rows:
            for ordinal, row in enumerate(rows):
                right = score_text(row.current, row.rating) if row.available else '不参与'
                width = QFontMetrics(option.font).horizontalAdvance(right) + 8
                area = QRect(inside.x(), top + ordinal * line, inside.width(), line)
                avatar_rect = QRect(area.x(), area.y() + (line - 22) // 2, 22, 22)
                avatar = lookup_avatar(getattr(self.parent(), 'avatar_cache', None), row.key[0])
                if avatar is not None:
                    painter.drawPixmap(avatar_rect, avatar)
                else:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor('#555555' if isDarkTheme() else '#dedede'))
                    painter.drawEllipse(avatar_rect)
                    painter.setPen(QColor('#ffffff' if isDarkTheme() else '#555555'))
                    painter.drawText(avatar_rect, Qt.AlignCenter, row.label[:1])
                label_area = area.adjusted(28, 0, -(width + 6), 0)
                label = row.label
                painter.setFont(stat_label_font(option.font, label, label_area.width()))
                painter.setPen(QColor('#b7b7b7' if isDarkTheme() else '#656565'))
                painter.drawText(
                    label_area,
                    Qt.AlignLeft | Qt.AlignVCenter,
                    QFontMetrics(painter.font()).elidedText(
                        label, Qt.ElideRight, label_area.width()
                    ),
                )
                painter.setFont(option.font)
                painter.setPen(
                    QColor(rating_colour(row.rating, isDarkTheme()))
                    if row.available
                    else QColor('#888888')
                )
                painter.drawText(area, Qt.AlignRight | Qt.AlignVCenter, right)
        else:
            for ordinal, text in enumerate((index.data(FOOTER_ROLE) or '').split('\n')):
                rating = getattr(getattr(source.data(SCORE_ROLE), 'score', None), 'rating', None)
                painter.setPen(QColor(rating_colour(rating, isDarkTheme())))
                painter.drawText(
                    QRect(inside.x(), top + ordinal * line, inside.width(), line),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    QFontMetrics(option.font).elidedText(text, Qt.ElideRight, inside.width()),
                )
        painter.restore()


class MatchScoreGrid(EchoCardGrid):
    def set_footer_rows(self, count):
        self._footer_rows = max(1, count)
        self._resize_grid()

    def _resize_grid(self):
        super()._resize_grid()
        size = self.gridSize()
        size.setHeight(
            size.height() + getattr(self, '_footer_rows', 1) * (self.fontMetrics().height() + 8) + 8
        )
        self.setGridSize(size)


class MatchResultCards(QWidget):
    selected = Signal(str, object)
    pending_review = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._matrix, self._key, self._focus_key = None, None, None
        self._loading, self._avatar_cache = True, None
        self._selected_id = None
        self._requirements = {}
        self._keys = ()
        self._pending_count = None
        self._projection_epoch = self._current_projection = self._selector_signature = None
        self._view_cache = OrderedDict()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.selector_row = QWidget(self)
        row = QHBoxLayout(self.selector_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(CaptionLabel('对比角色', self))
        self.role_selector = IdentityCombo(self)
        self.role_selector.setEditable(True)
        self.role_selector.setInsertPolicy(IdentityCombo.NoInsert)
        self.role_selector.lineEdit().setPlaceholderText('搜索已计算的角色 / 模版（支持拼音）')
        row.addWidget(self.role_selector, 1)
        self.order_hint = CaptionLabel('当前分高 → 低', self)
        row.addWidget(self.order_hint)
        self.pending_button = PushButton('待核对 —', self)
        self.pending_button.setToolTip('打开声骸库的待核对记录；不改变本次匹配结果')
        self.pending_button.clicked.connect(self.pending_review.emit)
        row.addWidget(self.pending_button)
        root.addWidget(self.selector_row)
        self.summary = CaptionLabel(self)
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.body = QStackedWidget(self)
        self.cards = self.grid = MatchScoreGrid(self.body, show_owner_avatar=False)
        self.cards.setSelectionMode(QAbstractItemView.SingleSelection)
        self.cards.lock_edit_enabled = False
        self.source = MatchCardSource(self.cards)
        self.cards.setModel(self.source)
        self.cards.delegate = MatchScoreDelegate(self.cards)
        self.cards.setItemDelegate(self.cards.delegate)
        # Entries already have the exact score/UUID order. Native proxy sorting
        # would call Python data N log N times to reproduce source row order.
        self.cards.proxy.sort(-1)
        self.body.addWidget(self.cards)
        self.message = QLabel(self.body)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignCenter)
        self.body.addWidget(self.message)
        root.addWidget(self.body, 1)
        self.role_selector.currentIndexChanged.connect(self._role_changed)
        self.role_selector.activated.connect(self._role_changed)
        self.cards.selectionModel().currentChanged.connect(self._card_changed)
        qconfig.themeChanged.connect(self.update_theme)
        self.setAutoFillBackground(True)
        self.update_theme()
        self._loading = False
        self.clear('选择角色并点击开始计算后，在这里查看完整声骸卡片。')

    def mount_selector(self, parent_layout):
        """Move the same selector/standards controls; preserve signals and draft."""
        self.layout().removeWidget(self.selector_row)
        parent_layout.addWidget(self.selector_row)
        self._selector_external = True
        self.summary.hide()
        self.order_hint.hide()

    def set_pending_count(self, count):
        """Passive counter owned by the inventory reader; never a compute trigger."""
        count = None if count is None else max(0, int(count))
        if count == self._pending_count:
            return
        self._pending_count = count
        self.pending_button.setText('待核对 —' if count is None else f'待核对 {count} 件')

    def update_theme(self, *_):
        dark = isDarkTheme()
        palette = self.palette()
        surface, foreground = (
            QColor('#202020' if dark else '#f9f9f9'),
            QColor('#f4f4f4' if dark else '#202020'),
        )
        for role in (QPalette.Window, QPalette.Base, QPalette.Button):
            palette.setColor(role, surface)
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            palette.setColor(role, foreground)
        self.setPalette(palette)
        self.role_selector.setPalette(palette)
        self.role_selector.view().setPalette(palette)
        self.update()

    @property
    def avatar_cache(self):
        return self._avatar_cache

    @avatar_cache.setter
    def avatar_cache(self, cache):
        self._avatar_cache = cache
        self.cards.avatar_cache = _LookupOnlyAvatar(cache) if cache is not None else None
        self.cards.viewport().update()

    @property
    def current_key(self):
        return self._key

    def selected_ids(self):
        return self.cards.selected_ids()

    def selected_id(self):
        return self._selected_id

    def clear(self, message=''):
        self._loading = True
        try:
            self._matrix, self._key, self._focus_key = None, None, None
            self._selected_id = None
            self._requirements = {}
            self._keys = ()
            self._projection_epoch = self._current_projection = self._selector_signature = None
            self._view_cache.clear()
            self.source.reset_entries()
            self.role_selector.clear()
            self.role_selector.setEnabled(False)
            self.selector_row.hide()
            self.summary.setText('')
            self.summary.setToolTip('')
            self.message.setText(message or '暂无匹配结果。')
            self.body.setCurrentWidget(self.message)
        finally:
            self._loading = False
        self.selected.emit('', None)

    def project(self, matrix_model, current_key=None, fresh=True, requirements=None):
        if not fresh:
            self.clear('选择或条件已变化；请点击开始计算更新结果。')
            return
        if any(
            not isinstance(key, (tuple, list))
            or len(key) != 2
            or any(not isinstance(value, str) or not value for value in key)
            for key in matrix_model.selections
        ):
            self.clear('角色 / 模版标识无效，请重新计算。')
            return
        keys = tuple(tuple(key) for key in matrix_model.selections)
        if (
            not keys
            or len(matrix_model.labels) != len(keys)
            or len(matrix_model.scores) != len(matrix_model.records) * len(keys)
        ):
            self.clear('暂无完整计算结果，请选择角色后重新计算。')
            return
        projection = matrix_model.projections
        if projection is None or not projection.matches(
            matrix_model.records, matrix_model.selections, matrix_model.scores
        ):
            self.clear('匹配投影未就绪，请重新计算。')
            return
        self._requirements = dict(requirements or {})
        epoch = (
            matrix_model,
            matrix_model.publication_revision,
            keys,
            tuple(matrix_model.labels),
            tuple((key, self._requirements.get(key)) for key in keys),
            tuple(sorted(getattr(matrix_model, 'claims', {}).items())),
        )
        if epoch != self._projection_epoch:
            self._projection_epoch = epoch
            self._current_projection = None
            self._view_cache.clear()
        focus = tuple(current_key) if current_key is not None else None
        previous = self._key
        if len(keys) == 1:
            desired = keys[0]
        elif self._matrix is None or self._keys != keys:
            desired = None  # A real multi-role view, not another single-role default.
        else:
            desired = previous if previous is None or previous in keys else None
            if desired is not None and focus in keys and focus != self._focus_key:
                desired = focus
        self._focus_key, self._matrix = focus, matrix_model
        self._keys = keys
        editor = self.role_selector.lineEdit()
        search = self.role_selector.currentText()
        searching = bool(search) and (
            self.role_selector.currentIndex() < 0
            or search != self.role_selector.itemText(self.role_selector.currentIndex())
        )
        cursor = editor.cursorPosition() if searching else 0
        self._loading = True
        try:
            blocked = self.role_selector.blockSignals(True)
            labels = tuple(label.split('\n', 1)[0] for label in matrix_model.labels)
            signature = (keys, labels)
            if signature != self._selector_signature:
                self.role_selector.clear()
                if len(keys) > 1:
                    self.role_selector.addItem('全部角色', userData=None)
                for key, label in zip(keys, labels):
                    self.role_selector.addItem(
                        label if labels.count(label) == 1 else label + '（' + '/'.join(key) + '）',
                        key,
                    )
                self._selector_signature = signature
            self.role_selector.setCurrentIndex(self.role_selector.findData(desired))
            if searching:
                self.role_selector.setEditText(search)
                editor.setCursorPosition(cursor)
            self.role_selector.blockSignals(blocked)
            self.role_selector.setEnabled(True)
            self.selector_row.setVisible(bool(keys))
        finally:
            self._loading = False
        self._project_role(desired)

    def _role_changed(self, *_):
        if self._loading or self._matrix is None:
            return
        box = self.role_selector
        if box.currentIndex() < 0 or box.currentText() != box.itemText(box.currentIndex()):
            return  # Half-typed pinyin is not an implicit role switch.
        data = box.currentData()
        key = tuple(data) if data is not None else None
        if key != self._key:
            self._project_role(key)

    def _project_role(self, key):
        if self._matrix is None or (key is not None and key not in self._matrix.selections):
            return
        if self._current_projection == (self._projection_epoch, key):
            # A passive refresh does not reset selection, scroll or typed search.
            self.selected.emit(self._selected_id or '', self._selection_key())
            return
        previous_id = self._selected_id
        cached = self._view_cache.get(key)
        if cached is None:
            cached = self._map_projection(key)
            self._view_cache[key] = cached
            if len(self._view_cache) > 3:
                self._view_cache.popitem(last=False)
        self._view_cache.move_to_end(key)
        entries, comparisons, excluded, unknown = cached
        self._publish_projection(key, previous_id, entries, comparisons, excluded, unknown)
        self._current_projection = self._projection_epoch, key

    def _map_projection(self, key):
        matrix = self._matrix
        width = len(matrix.selections)
        columns = {role: index for index, role in enumerate(matrix.selections)}
        keys = self._keys if key is None else (key,)
        labels = dict(zip(self._keys, (label.split('\n', 1)[0] for label in matrix.labels)))
        projection = matrix.projections
        view = projection.views[0 if key is None else columns[key] + 1]
        entries, comparisons = [], {}
        for source_row, representative in view.entries:
            record = matrix.records[source_row]
            entries.append((record, matrix.scores[source_row * width + representative]))
            rows = []
            for role in keys:
                offset = source_row * width + columns[role]
                score = matrix.scores[offset]
                flag = projection.flags[offset]
                available = _CELL_AVAILABLE[flag]
                values = score.score
                rows.append(
                    RoleScoreRow(
                        role,
                        labels[role],
                        _number(values.current_score) if available else None,
                        _number(values.potential_score) if available else None,
                        available,
                        _CELL_REASON[flag],
                        values.rating if available else None,
                        score.weight_version if available else '',
                    )
                )
            comparisons[record.item_id] = tuple(rows)
        return tuple(entries), comparisons, view.excluded, view.unknown

    def _publish_projection(self, key, previous_id, entries, comparisons, excluded, unknown):
        keys = self._keys if key is None else (key,)
        self._key = key
        self._loading = True
        try:
            self.source.reset_entries(
                entries, comparisons=comparisons, claims=getattr(self._matrix, 'claims', {})
            )
            self.cards.set_footer_rows(len(keys) if key is None else 1)
            self.cards.proxy.sort(-1)
            self.cards.restore_selection((previous_id,) if previous_id else (), previous_id)
            self._selected_id = self.cards.currentIndex().data(Qt.UserRole)
            self.selector_row.setVisible(bool(self._keys))
            self.role_selector.setVisible(bool(self._keys))
            self.selector_row.layout().itemAt(0).widget().setVisible(bool(self._keys))
            self.summary.setText(
                f'{len(entries)} 件声骸 · {len(keys)} 个角色'
                if key is None
                else f'{len(entries)} 件可用声骸'
            )
            self.order_hint.setText('最高当前分高 → 低' if key is None else '当前分高 → 低')
            self.summary.setToolTip(
                f'候选 {len(self._matrix.records)} 件，隐藏不符合要求或不可评分的 {excluded} 件。\n'
                '主词条按 +25 评分。多角色独立评分，不代表独占配装。'
                + (
                    '\n部分适配要求尚未设置；分数仍是词条评分。可在角色设置补全要求。'
                    if unknown
                    else ''
                )
            )
            self.message.setText('没有符合要求的声骸，请调整声骸选择或角色设置。')
            self.body.setCurrentWidget(self.cards if entries else self.message)
        finally:
            self._loading = False
        self.selected.emit(self._selected_id or '', self._selection_key())

    def _selection_key(self):
        if not self._selected_id:
            return None
        if self._key is not None:
            return self._key
        index = self.source.row_for_id(self._selected_id)
        available = tuple(row.key for row in self.source.comparison_rows(index) if row.available)
        return (
            self._focus_key
            if self._focus_key in available
            else (available[0] if available else None)
        )

    def _card_changed(self, *_):
        if self._loading:
            return
        self._selected_id = self.cards.currentIndex().data(Qt.UserRole)
        self.selected.emit(self._selected_id or '', self._selection_key())
