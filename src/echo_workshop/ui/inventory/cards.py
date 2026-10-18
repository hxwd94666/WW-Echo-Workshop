# 绘制声骸卡片并支持多选操作。
"""Virtual card grid and selected card share one painter; never decode evidence."""

from collections import OrderedDict
from dataclasses import replace
from echo_workshop.core.filters import is_protected
from echo_workshop.ui.inventory.filter_proxy import InventoryFilterProxy
from PySide6.QtCore import (
    QAbstractAnimation,
    QEvent,
    QItemSelectionModel,
    QItemSelection,
    QModelIndex,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QStyle,
    QToolTip,
    QStyledItemDelegate,
    QWidget,
    QSizePolicy,
)
from qfluentwidgets import isDarkTheme, themeColor, qconfig, SmoothScrollDelegate
from echo_workshop.ui.inventory.card_data import project_card, project_checked_card

RECORD_ROLE = int(Qt.UserRole) + 1
SEARCH_ROLE = int(Qt.UserRole) + 2
SORT_ROLE = int(Qt.UserRole) + 3
FOOTER_ROLE = int(Qt.UserRole) + 4
RESERVATION_ROLE = int(Qt.UserRole) + 6
QUALITY_ROLE = int(Qt.UserRole) + 7


def card_height(data, font, score_text=''):
    line = QFontMetrics(font).height() + 8
    # All valid echoes (two main + zero..five sub rows) share one height.
    rows = max(7, len(data.rows) + bool(data.empty_sub))
    # 始终预留一行状态区，保护或占用信息变化时不改变卡片高度。
    return 30 + (2 + rows) * line + line + 6


def stat_label_font(font, value, width):
    """Fit full damage labels into the same five-column, equal-height cards."""
    result = QFont(font)
    while QFontMetrics(result).horizontalAdvance(value) > width:
        if result.pointSizeF() > 8:
            result.setPointSizeF(max(8, result.pointSizeF() - 0.5))
        elif result.pointSizeF() < 0 and result.pixelSize() > 10:
            result.setPixelSize(result.pixelSize() - 1)
        else:
            break
    return result


def card_header_rects(rect, data, font, *, show_owner_avatar=True):
    """Reserve an independent upper-right badge even before its avatar is cached."""
    inside = rect.adjusted(14, 10, -14, -10)
    metrics = QFontMetrics(font)
    line = metrics.height() + 8
    title = QRect(inside.x(), inside.y(), inside.width(), line)
    if not data.owner:
        return title, QRect(), QRect()
    side = min(24, line) if show_owner_avatar else 0
    gap = 5 if show_owner_avatar else 0
    width = min(side + gap + metrics.horizontalAdvance(data.owner), inside.width() // 2)
    x = inside.right() - width + 1
    avatar = QRect(x, inside.y() + (line - side) // 2, side, side)
    owner = QRect(x + side + gap, inside.y(), max(1, width - side - gap), line)
    title.setWidth(max(1, x - inside.x() - 8))
    return title, avatar, owner


def card_lock_rect(rect, font):
    inside = rect.adjusted(14, 10, -14, -10)
    line = QFontMetrics(font).height() + 8
    return QRect(inside.right() - 17, inside.y() + line + (line - 18) // 2, 18, 18)


def card_lock_color(locked):
    return themeColor() if locked else QColor('#888888')


def paint_plan_lock(painter, area, locked):
    """Read-only local-plan status, never the discarded game lock observation."""
    painter.save()
    painter.setPen(QPen(card_lock_color(locked), 1.6))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRectF(area.x() + 5, area.y() + 1, 8, 10), 4, 4)
    painter.setBrush(card_lock_color(locked))
    painter.drawRoundedRect(QRectF(area.x() + 2, area.y() + 8, 14, 9), 2, 2)
    painter.setPen(QPen(QColor('#232323' if isDarkTheme() else '#ffffff'), 1.5))
    painter.drawLine(area.x() + 9, area.y() + 11, area.x() + 9, area.y() + 14)
    painter.restore()


def paint_card(
    painter, rect, data, font, selected=False, score_text='', avatar=None, *, show_owner_avatar=True
):
    from echo_workshop.ui.inventory.render_cache import draw_cached_card

    draw_cached_card(
        painter,
        rect,
        data,
        font,
        selected,
        score_text,
        avatar,
        show_owner_avatar,
        lambda target, area: _paint_card(
            target,
            area,
            data,
            font,
            selected,
            score_text,
            avatar,
            show_owner_avatar=show_owner_avatar,
        ),
    )


def _paint_card(
    painter, rect, data, font, selected=False, score_text='', avatar=None, *, show_owner_avatar=True
):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    dark = isDarkTheme()
    accent = themeColor()
    surface = QColor('#232323' if dark else '#ffffff')
    foreground = QColor('#f4f4f4' if dark else '#202020')
    secondary = QColor('#b7b7b7' if dark else '#656565')
    border = accent if selected else QColor('#464646' if dark else '#dedede')
    painter.setPen(QPen(border, 2 if selected else 1))
    painter.setBrush(surface)
    painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)
    inside = rect.adjusted(14, 10, -14, -10)
    regular = QFont(font)
    bold = QFont(font)
    bold.setBold(True)
    metrics = QFontMetrics(regular)
    bold_metrics = QFontMetrics(bold)
    line = metrics.height() + 8

    def text(area, value, *, color=foreground, heavy=False, align=Qt.AlignLeft):
        painter.setFont(bold if heavy else regular)
        painter.setPen(color)
        painter.drawText(
            area,
            align | Qt.AlignVCenter,
            (bold_metrics if heavy else metrics).elidedText(value, Qt.ElideRight, area.width()),
        )

    title_rect, avatar_rect, owner_rect = card_header_rects(
        rect, data, font, show_owner_avatar=show_owner_avatar
    )
    text(title_rect, data.title, heavy=True)
    paint_plan_lock(painter, card_lock_rect(rect, font), data.locked)
    if data.owner:
        if show_owner_avatar and avatar is not None and not avatar.isNull():
            painter.drawPixmap(avatar_rect, avatar)
        text(owner_rect, data.owner, color=secondary)
    text(
        QRect(inside.x(), inside.y() + line, inside.width() - 24, line),
        data.metadata,
        color=secondary,
    )
    y = inside.y() + 2 * line + 10
    for row in data.rows:
        area = QRect(inside.x(), y, inside.width(), line)
        if row.main:
            tint = QColor(accent)
            tint.setAlpha(24 if dark else 15)
            painter.fillRect(area.adjusted(-3, 0, 3, 0), tint)
            painter.fillRect(QRect(area.x() - 3, y + 3, 2, line - 6), accent)
        # Right value is never elided. Delegate minimum width reserves its span.
        row_metrics = bold_metrics if row.main else metrics
        value_width = row_metrics.horizontalAdvance(row.value) + 6
        name_rect = area.adjusted(5, 0, -(value_width + 12), 0)
        row_font = bold if row.main else regular
        fitted = row_metrics.horizontalAdvance(row.name) <= name_rect.width()
        painter.setFont(
            row_font if fitted else stat_label_font(row_font, row.name, name_rect.width())
        )
        painter.setPen(secondary if row.uncertain else foreground)
        painter.drawText(
            name_rect,
            Qt.AlignLeft | Qt.AlignVCenter,
            (row_metrics if fitted else QFontMetrics(painter.font())).elidedText(
                row.name, Qt.ElideRight, name_rect.width()
            ),
        )
        painter.setFont(bold if row.main else regular)
        painter.setPen(secondary if row.uncertain else foreground)
        painter.drawText(
            QRect(area.right() - value_width, y, value_width + 1, line),
            Qt.AlignRight | Qt.AlignVCenter,
            row.value,
        )
        if row.uncertain:
            painter.setPen(QPen(secondary, 1, Qt.DotLine))
            painter.drawLine(area.x() + 5, y + line - 2, area.right(), y + line - 2)
        y += line
    if data.empty_sub:
        text(QRect(inside.x() + 5, y, inside.width() - 5, line), data.empty_sub, color=secondary)
        y += line
    if score_text:
        text(
            QRect(inside.x(), inside.bottom() - line, inside.width(), line),
            score_text,
            color=secondary,
        )
    painter.restore()


def cached_avatar(cache, ident):
    if cache is None or not ident:
        return None
    cache.request(ident)
    from echo_workshop.ui.widgets.rating import lookup_avatar

    return lookup_avatar(cache, ident)


class EchoCardWidget(QWidget):
    """Single selected/detail card. Constant widget count, not per-record widgets."""

    def __init__(self, parent=None, *, show_owner_avatar=True):
        super().__init__(parent)
        self.show_owner_avatar = bool(show_owner_avatar)
        self.data = None
        self.score_text = ''
        self.hint = '选择声骸查看词条'
        self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        qconfig.themeChanged.connect(self.update_theme)
        qconfig.themeColorChanged.connect(self.update_theme)

    def update_theme(self, *_):
        self.update()

    def set_record(self, record, kind='item', score_text='', *, reservation='', quality=None):
        self.data = (
            project_checked_card(record, quality, kind)
            if record is not None and quality is not None
            else project_card(record, kind)
            if record is not None
            else None
        )
        if self.data is not None and reservation:
            self.data = replace(self.data, locked=is_protected(record, reservation))
        self.score_text = score_text
        self.setToolTip('')
        height = card_height(self.data, self.font(), self.score_text) if self.data else 50
        self.setFixedHeight(height)
        self.updateGeometry()
        self.update()

    def clear(self):
        self.set_record(None)

    def setText(self, text):
        self.hint = text
        self.set_record(None)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and getattr(self, 'data', None) is not None:
            self.setFixedHeight(card_height(self.data, self.font(), self.score_text))
            self.updateGeometry()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.data is not None:
            paint_card(
                painter,
                self.rect(),
                self.data,
                self.font(),
                score_text=self.score_text,
                avatar=(
                    cached_avatar(getattr(self, 'avatar_cache', None), self.data.owner_character_id)
                    if self.show_owner_avatar
                    else None
                ),
                show_owner_avatar=self.show_owner_avatar,
            )
        else:
            painter.setPen(QColor('#b7b7b7' if isDarkTheme() else '#656565'))
            painter.drawText(self.rect().adjusted(8, 8, -8, -8), Qt.TextWordWrap, self.hint)


class EchoCardDelegate(QStyledItemDelegate):
    CACHE_LIMIT = 256

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cache = OrderedDict()
        self.paint_count = 0

    def projection(self, index):
        value = index.data(RECORD_ROLE)
        if value is None:
            return None
        record, kind, _ = value
        reservation = index.data(RESERVATION_ROLE) or ''
        quality = index.data(QUALITY_ROLE)
        key = (
            record.item_id,
            kind,
            reservation,
            None if quality is None else (quality.issues, quality.usable),
        )
        old = self.cache.get(key)
        if old is not None and old[0] is record:
            self.cache.move_to_end(key)
            return old[1]
        data = (
            project_checked_card(record, quality, kind)
            if quality is not None
            else project_card(record, kind)
        )
        if reservation:
            data = replace(data, locked=is_protected(record, reservation))
        self.cache[key] = record, data
        self.cache.move_to_end(key)
        if len(self.cache) > self.CACHE_LIMIT:
            self.cache.popitem(last=False)
        return data

    def paint(self, painter, option, index):
        data = self.projection(index)
        if data is None:
            return
        self.paint_count += 1
        footer = index.data(FOOTER_ROLE) if self.parent().show_score_footer else ''
        area = option.rect.adjusted(5, 5, -5, -5)
        area.setHeight(min(area.height(), card_height(data, option.font, footer)))
        paint_card(
            painter,
            area,
            data,
            option.font,
            bool(option.state & QStyle.State_Selected),
            score_text=footer or '',
            avatar=(
                cached_avatar(getattr(self.parent(), 'avatar_cache', None), data.owner_character_id)
                if getattr(self.parent(), 'show_owner_avatar', True)
                else None
            ),
            show_owner_avatar=getattr(self.parent(), 'show_owner_avatar', True),
        )

    def sizeHint(self, option, index):
        # Uniform cell permits QListView's fixed-size virtualized layout.
        return self.parent().gridSize()

    def helpEvent(self, event, view, option, index):
        QToolTip.hideText()
        return True  # Cards deliberately have no hover information popup.


class CardScrollDelegate(SmoothScrollDelegate):
    """One wheel notch moves half a card, without Qt line-count acceleration."""

    def __init__(self, parent):
        super().__init__(parent, useAni=True)
        self.vScrollBar.setScrollAnimation(200)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Wheel:
            return super().eventFilter(obj, event)
        pixels = event.pixelDelta().y()
        angle = event.angleDelta().y()
        if not pixels and not angle:
            return super().eventFilter(obj, event)
        # Keep the target in sync after selection, filtering or dragging the bar.
        if self.vScrollBar.ani.state() == QAbstractAnimation.Stopped:
            self.vScrollBar.resetValue(self.parent().verticalScrollBar().value())
        if pixels:
            self.vScrollBar.scrollValue(-pixels, useAni=False)
        else:
            half_card = max(1, round((self.parent().gridSize().height() - 10) / 2))
            self.vScrollBar.scrollValue(round(-angle / 120 * half_card))
        event.accept()
        return True


class EchoCardGrid(QListView):
    """Source-row API used by the existing controller; proxy used only for UI."""

    lockRequested = Signal(str)
    projectionChanged = Signal()

    def __init__(
        self, parent=None, *, show_score_footer=False, show_owner_avatar=True, queries=None
    ):
        super().__init__(parent)
        self.show_owner_avatar = bool(show_owner_avatar)
        self.show_score_footer = show_score_footer
        self.lock_edit_enabled = False
        self._lock_press = None
        self.proxy = InventoryFilterProxy(self, queries=queries)
        self._projection_state = None
        self._selection_signals_blocked = None
        self.proxy.stateChanged.connect(self._projection_state_changed)
        self.proxy.projectionAboutToChange.connect(self._projection_changing)
        self.proxy.projectionChanged.connect(self._projection_changed)
        self.proxy.setFilterRole(SEARCH_ROLE)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setFilterKeyColumn(0)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.delegate = EchoCardDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionRectVisible(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSpacing(0)
        self.scroll_delegate = CardScrollDelegate(self)
        self.setMinimumWidth(190)
        self.columns = 1
        self._resize_grid()
        self.setFrameShape(QListView.NoFrame)
        qconfig.themeChanged.connect(self.update_theme)
        qconfig.themeColorChanged.connect(self.update_theme)
        self.update_theme()

    def update_theme(self, *_):
        palette = self.palette()
        palette.setColor(QPalette.Base, QColor('#202020' if isDarkTheme() else '#f9f9f9'))
        palette.setColor(QPalette.Text, QColor('#f4f4f4' if isDarkTheme() else '#202020'))
        self.setPalette(palette)
        self.viewport().update()

    def setModel(self, model):
        self.proxy.setSourceModel(model)
        super().setModel(self.proxy)
        self.setModelColumn(0)
        model.modelReset.connect(self.delegate.cache.clear)
        self._selection_cache = None
        self.selectionModel().selectionChanged.connect(self._selection_changed)
        model.modelReset.connect(self._selection_changed)

    def _selection_changed(self, *_):
        self._selection_cache = None

    def _capture_projection_state(self):
        if self._projection_state is None and self.selectionModel() is not None:
            self._projection_state = (
                self.selected_ids(),
                self.currentIndex().data(Qt.UserRole),
                self.verticalScrollBar().value(),
            )

    def _projection_state_changed(self):
        if not self.proxy.projection_ready:
            self._capture_projection_state()
        self.viewport().setEnabled(self.proxy.projection_ready)

    def _projection_changing(self):
        self._capture_projection_state()
        if self.selectionModel() is not None:
            self._selection_signals_blocked = self.selectionModel().blockSignals(True)

    def _projection_changed(self):
        selection = self.selectionModel()
        if selection is None:
            return
        try:
            if self.proxy.projection_ready and self._projection_state is not None:
                identities, current, scroll = self._projection_state
                self._projection_state = None
                self.restore_selection(identities, current)
                self.verticalScrollBar().setValue(scroll)
        finally:
            if self._selection_signals_blocked is not None:
                selection.blockSignals(self._selection_signals_blocked)
                self._selection_signals_blocked = None
            self._selection_changed()
        # The page restores its authoritative UUID draft before observers sync it.
        self.projectionChanged.emit()
        if self.proxy.projection_ready:
            selection.currentChanged.emit(self.currentIndex(), QModelIndex())
            selection.selectionChanged.emit(QItemSelection(), QItemSelection())

    def selectRow(self, source_row):
        source = self.proxy.sourceModel()
        if source is None:
            return
        if not self.proxy.projection_ready:
            identity = source.index(source_row, 0).data(Qt.UserRole)
            self.restore_selection((identity,) if identity else (), identity)
            return
        index = self.proxy.mapFromSource(source.index(source_row, 0))
        if index.isValid():
            self.selectionModel().setCurrentIndex(index, QItemSelectionModel.ClearAndSelect)
            self.scrollTo(index, QAbstractItemView.EnsureVisible)

    def search(self, text, filters=None):
        filters = self.proxy.filters if filters is None else filters
        self.proxy.set_filter_spec(text, filters)

    def _resize_grid(self):
        # Fixed logical dimensions: window resizing changes columns, not cards.
        cell_width = 240
        self.columns = max(1, (self.viewport().width() - 2) // cell_width)
        line = self.fontMetrics().height() + 8
        height = 30 + 10 * line + 6 + 10
        self.setGridSize(QSize(cell_width, height))

    def selected_ids(self):
        if self._selection_cache is None:
            self._selection_cache = tuple(
                dict.fromkeys(
                    index.data(Qt.UserRole)
                    for index in self.selectionModel().selectedIndexes()
                    if index.isValid() and index.column() == 0
                )
            )
        return self._selection_cache

    def restore_selection(self, identities, current_id=None):
        if not self.proxy.projection_ready:
            self._projection_state = (
                tuple(identities),
                current_id,
                self.verticalScrollBar().value(),
            )
            return
        source = self.proxy.sourceModel()
        if source is None:
            return
        selection = self.selectionModel()
        indexes = []
        for identity in identities:
            row = source.row_for_id(identity)
            index = self.proxy.mapFromSource(source.index(row, 0)) if row >= 0 else QModelIndex()
            if index.isValid():
                indexes.append(index)
        indexes.sort(key=lambda index: index.row())
        ranges = QItemSelection()
        start = end = None
        for index in indexes:
            if end is not None and index.row() != end.row() + 1:
                ranges.select(start, end)
                start = None
            start = index if start is None else start
            end = index
        if start is not None:
            ranges.select(start, end)
        selection.select(ranges, QItemSelectionModel.ClearAndSelect)
        current = (
            self.proxy.mapFromSource(source.index(source.row_for_id(current_id), 0))
            if current_id
            else QModelIndex()
        )
        if not current.isValid():
            current = next(iter(selection.selectedIndexes()), QModelIndex())
        selection.setCurrentIndex(current, QItemSelectionModel.NoUpdate)

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        self._lock_press = None
        if event.button() == Qt.LeftButton and index.isValid() and self.lock_edit_enabled:
            area = self.visualRect(index).adjusted(5, 5, -5, -5)
            hit = card_lock_rect(area, self.font()).adjusted(-4, -4, 4, 4)
            if hit.contains(event.position().toPoint()):
                self._lock_press = (index.data(Qt.UserRole), hit)
                event.accept()
                return
        if event.button() == Qt.RightButton:
            if self.selectionMode() == QAbstractItemView.MultiSelection:
                event.accept()
                return
            if index.isValid():
                flags = (
                    QItemSelectionModel.NoUpdate
                    if self.selectionModel().isSelected(index)
                    else QItemSelectionModel.ClearAndSelect
                )
                self.selectionModel().setCurrentIndex(index, flags)
            event.accept()
            return
        if event.button() == Qt.LeftButton and not index.isValid() and not event.modifiers():
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._lock_press is not None:
            identity, hit = self._lock_press
            self._lock_press = None
            if event.button() == Qt.LeftButton and hit.contains(event.position().toPoint()):
                self.lockRequested.emit(identity)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self._lock_press is not None:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.ToolTip:
            QToolTip.hideText()
            event.accept()
            return True
        return super().viewportEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_grid()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._resize_grid()
