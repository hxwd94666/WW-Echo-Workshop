# 展示评级颜色及明确打开的标准窗口。
"""Small score/standard surfaces. All values are already frozen; no IO/scoring."""

from collections import OrderedDict
import weakref

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPixmap, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QScrollArea,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QApplication,
)
from qfluentwidgets import CaptionLabel, PushButton, TitleLabel, isDarkTheme, qconfig, themeColor
from echo_workshop.ui.inventory.cards import (
    EchoCardDelegate,
    EchoCardWidget,
    FOOTER_ROLE,
    card_height,
    paint_card,
)
from echo_workshop.ui.widgets.rating_style import (
    COLOUR_NOTE,
    rating_colour,
    rating_tooltip,
    score_text,
)

FOOTER_RATING_ROLE = int(Qt.UserRole) + 20
HISTORY_SCORE_ROLE = int(Qt.UserRole) + 21


def apply_rating_colour(widget, rating):
    widget.setStyleSheet('color: ' + rating_colour(rating, isDarkTheme()) + ';')


class RatingValueLabel(TitleLabel):
    def __init__(self, text='', parent=None):
        super().__init__(parent=parent)
        self.setText(text)
        self.rating = None
        qconfig.themeChangedFinished.connect(self.update_rating_colour)

    def update_rating_colour(self, *_):
        apply_rating_colour(self, self.rating)

    def set_value(self, points, rating=None, prefix='', suffix=''):
        self.rating = rating
        self.setText(score_text(points, rating, prefix) + suffix)
        self.setToolTip('')
        self.update_rating_colour()

    def clear(self):
        self.rating = None
        super().clear()
        self.setToolTip('')
        self.update_rating_colour()


class RatingStandardsButton(PushButton):
    """Explicit, compact viewer for the actual current/historical thresholds."""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setText('评级标准')
        self.entries = ()
        self.dialog = None
        self.clicked.connect(self.open_standards)
        self.hide()

    def set_entries(self, entries):
        unique = {}
        rows = []
        for title, rating, version in entries:
            if rating is None:
                continue
            identity = (title, getattr(rating, 'standard_id', rating.tooltip), version)
            if identity in unique:
                continue
            unique[identity] = True
            rows.append((title, rating, version))
        self.entries = tuple(rows)
        self.setVisible(bool(self.entries))
        self.setToolTip('')

    def open_standards(self):
        if not self.entries:
            return
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
        dialog = self.dialog = QDialog(self.window())
        dialog.setObjectName('EchoRatingStandards')
        dialog.setWindowTitle('评级标准')
        dialog.grade_rows = []
        outer = QVBoxLayout(dialog)
        note = CaptionLabel(COLOUR_NOTE, dialog)
        note.setWordWrap(True)
        outer.addWidget(note)
        area = QScrollArea(dialog)
        area.setWidgetResizable(True)
        body = QWidget(area)
        body.setObjectName('ratingStandardsBody')
        area.viewport().setObjectName('ratingStandardsViewport')
        dialog.area, dialog.body = area, body
        rows = QVBoxLayout(body)
        for title, rating, version in self.entries:
            cost = getattr(rating, 'cost', None)
            scope = (
                f'COST {cost} · {getattr(rating, "scale", 50)} 分制'
                if cost is not None
                else f'整套 · {getattr(rating, "scale", 250)} 分制'
            )
            heading = CaptionLabel(title + ' · ' + scope, body)
            heading.setWordWrap(True)
            rows.addWidget(heading)
            source = CaptionLabel('来源：' + getattr(rating, 'source', '保存时模版'), body)
            rows.addWidget(source)
            thresholds = getattr(rating, 'thresholds', ())
            if len(thresholds) == 6:
                for level, lower in zip(('C', 'B', 'A', 'S', 'SS', 'SSS'), thresholds):
                    line = QHBoxLayout()
                    label = CaptionLabel(level, body)
                    label.setMinimumWidth(45)
                    threshold = CaptionLabel(f'≥ {lower:g} 分', body)
                    line.addWidget(label)
                    line.addWidget(threshold, 1)
                    rows.addLayout(line)
                    dialog.grade_rows.append((level, label, threshold))
            else:
                # Legacy synthetic cells may not carry the structured table.
                details = CaptionLabel(rating_tooltip(rating, version), body)
                details.setWordWrap(True)
                rows.addWidget(details)
        rows.setSpacing(5)
        rows.setContentsMargins(6, 4, 6, 4)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(body)
        outer.addWidget(area, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, dialog)
        buttons.button(QDialogButtonBox.Close).setText('关闭')
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)

        def update_theme(*_):
            dark = isDarkTheme()
            surface = QColor('#202020' if dark else '#fafafa')
            foreground = QColor('#f4f4f4' if dark else '#202020')
            palette = dialog.palette()
            for role in (QPalette.Window, QPalette.Base):
                palette.setColor(role, surface)
            for role in (QPalette.WindowText, QPalette.Text):
                palette.setColor(role, foreground)
            for widget in (dialog, area, area.viewport(), body):
                widget.setPalette(palette)
                widget.setAutoFillBackground(True)
            dialog.setStyleSheet(
                f'#EchoRatingStandards, #ratingStandardsBody, #ratingStandardsViewport {{background:{surface.name()};}} #EchoRatingStandards QScrollArea {{background:{surface.name()};border:none;}} #EchoRatingStandards QLabel {{color:{foreground.name()};background:transparent;}}'
            )
            for level, label, threshold in dialog.grade_rows:
                apply_rating_colour(label, level)
                apply_rating_colour(threshold, level)

        bound = [True]

        def disconnect_theme(*_):
            if bound[0]:
                qconfig.themeChangedFinished.disconnect(update_theme)
                bound[0] = False

        qconfig.themeChangedFinished.connect(update_theme)
        dialog.finished.connect(disconnect_theme)
        dialog.destroyed.connect(disconnect_theme)
        update_theme()
        screen = dialog.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else QRect(0, 0, 800, 600)
        width = min(420, max(260, available.width() - 40))
        body.layout().activate()
        body_height = body.layout().sizeHint().height()
        note_height = max(note.sizeHint().height(), note.heightForWidth(width - 24))
        height = body_height + note_height + buttons.sizeHint().height() + outer.spacing() * 2 + 24
        from echo_workshop.ui.widgets.layout import fit_dialog

        fit_dialog(dialog, width, min(height, 520))
        dialog.show()


_AVATAR_PIXMAPS = OrderedDict()
_AVATAR_PIXMAP_BYTES = 0
_AVATAR_PIXMAP_LIMIT = 24 * 1024 * 1024
_AVATAR_PIXMAP_STATS = {'hits': 0, 'decodes': 0}


def _drop_avatar(key):
    global _AVATAR_PIXMAP_BYTES
    row = _AVATAR_PIXMAPS.pop(key, None)
    if row is not None:
        _AVATAR_PIXMAP_BYTES -= row[4]


def lookup_avatar(cache, identity):
    """GUI-only bounded decoding shared by all card/header consumers.

    Revisions allow an O(1) warm lookup; caches without revisions compare the
    immutable source bytes. Missing images are not retained as negative hits.
    """
    global _AVATAR_PIXMAP_BYTES
    if cache is None or not identity:
        return None
    key = id(cache), identity
    revision = getattr(cache, 'revision', None)
    old = _AVATAR_PIXMAPS.get(key)
    if old is not None and old[0]() is cache:
        if revision is not None and old[1] == revision:
            _AVATAR_PIXMAPS.move_to_end(key)
            _AVATAR_PIXMAP_STATS['hits'] += 1
            return old[3]
    body = cache.lookup(identity)
    if body is None:
        _drop_avatar(key)
        return None
    if old is not None and old[0]() is cache and old[2] == body:
        _AVATAR_PIXMAPS[key] = old[0], revision, body, old[3], old[4]
        _AVATAR_PIXMAPS.move_to_end(key)
        _AVATAR_PIXMAP_STATS['hits'] += 1
        return old[3]
    _drop_avatar(key)
    pixmap = QPixmap()
    _AVATAR_PIXMAP_STATS['decodes'] += 1
    pixmap.loadFromData(body, 'PNG')
    if pixmap.isNull():
        return None
    cost = len(body) + pixmap.width() * pixmap.height() * 4
    if cost <= _AVATAR_PIXMAP_LIMIT:
        try:
            owner = weakref.ref(
                cache,
                lambda _ref, cache_id=id(cache): [
                    _drop_avatar(k) for k in tuple(_AVATAR_PIXMAPS) if k[0] == cache_id
                ],
            )
        except TypeError:

            def owner():
                return cache  # Legacy stubs without weak references remain bounded.

        _AVATAR_PIXMAPS[key] = owner, revision, body, pixmap, cost
        _AVATAR_PIXMAP_BYTES += cost
        while len(_AVATAR_PIXMAPS) > 256 or _AVATAR_PIXMAP_BYTES > _AVATAR_PIXMAP_LIMIT:
            _drop_avatar(next(iter(_AVATAR_PIXMAPS)))
    return pixmap


class RatedEchoCardWidget(EchoCardWidget):
    def __init__(self, parent=None, *, show_owner_avatar=True):
        super().__init__(parent, show_owner_avatar=show_owner_avatar)
        self.rating = None

    def set_record(self, *args, **kwargs):
        self.rating = None
        super().set_record(*args, **kwargs)

    def set_rating(self, rating):
        self.rating = rating
        self.setToolTip('')
        self.update()

    def paintEvent(self, event):
        if self.data is None or not self.score_text:
            return super().paintEvent(event)
        painter = QPainter(self)
        paint_card(
            painter,
            self.rect(),
            self.data,
            self.font(),
            avatar=(
                lookup_avatar(getattr(self, 'avatar_cache', None), self.data.owner_character_id)
                if self.show_owner_avatar
                else None
            ),
            show_owner_avatar=self.show_owner_avatar,
        )
        line = QFontMetrics(self.font()).height() + 8
        rect = self.rect().adjusted(14, 10, -14, -10)
        painter.setPen(QColor(rating_colour(self.rating, isDarkTheme())))
        painter.drawText(
            QRect(rect.x(), rect.bottom() - line, rect.width(), line),
            Qt.AlignLeft | Qt.AlignVCenter,
            QFontMetrics(self.font()).elidedText(self.score_text, Qt.ElideRight, rect.width()),
        )


class RatedCardDelegate(EchoCardDelegate):
    def paint(self, painter, option, index):
        data = self.projection(index)
        if data is None:
            return
        footer = index.data(FOOTER_ROLE) or ''
        area = option.rect.adjusted(5, 5, -5, -5)
        area.setHeight(min(area.height(), card_height(data, option.font, footer)))
        from PySide6.QtWidgets import QStyle

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
        painter.save()
        painter.setFont(option.font)
        painter.setPen(QColor(rating_colour(index.data(FOOTER_RATING_ROLE), isDarkTheme())))
        painter.drawText(
            QRect(inside.x(), inside.bottom() - line, inside.width(), line),
            Qt.AlignLeft | Qt.AlignVCenter,
            QFontMetrics(option.font).elidedText(footer, Qt.ElideRight, inside.width()),
        )
        painter.restore()


class HistoryScoreDelegate(QStyledItemDelegate):
    """Compact list row: original headings plus individually coloured totals."""

    def paint(self, painter, option, index):
        scores = index.data(HISTORY_SCORE_ROLE)
        if not scores:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        lines = opt.text.splitlines()
        opt.text = ''
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        selected = bool(option.state & QStyle.State_Selected)
        painter.setPen(
            themeColor() if selected else QColor('#464646' if isDarkTheme() else '#dedede')
        )
        painter.setBrush(
            QColor(
                ('#29313a' if isDarkTheme() else '#e8eff5')
                if selected
                else ('#232323' if isDarkTheme() else '#ffffff')
            )
        )
        painter.drawRoundedRect(option.rect.adjusted(2, 2, -2, -2), 6, 6)
        if not opt.icon.isNull():
            opt.icon.paint(
                painter, QRect(option.rect.x() + 9, option.rect.center().y() - 15, 30, 30)
            )
        painter.restore()
        rect = option.rect.adjusted(46 if not opt.icon.isNull() else 9, 8, -8, -6)
        painter.save()
        painter.setFont(option.font)
        line = QFontMetrics(option.font).height() + 5
        for i, text in enumerate(lines[:2]):
            painter.setPen(QColor('#eeeeee' if isDarkTheme() else '#202020'))
            painter.drawText(
                QRect(rect.x(), rect.y() + i * line, rect.width(), line),
                Qt.AlignLeft | Qt.AlignVCenter,
                QFontMetrics(option.font).elidedText(text, Qt.ElideRight, rect.width()),
            )
        x = rect.x()
        visible = scores[:3]
        width = max(1, rect.width() // len(visible))
        for points, label in visible:
            text = f'{points:.2f}' + (f' · {label}' if label else '')
            painter.setPen(QColor(rating_colour(label, isDarkTheme())))
            painter.drawText(
                QRect(x, rect.y() + 2 * line, width, line),
                Qt.AlignLeft | Qt.AlignVCenter,
                QFontMetrics(option.font).elidedText(text, Qt.ElideRight, width),
            )
            x += width
        painter.restore()
