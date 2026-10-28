# 安排角色设置的等高布局。
"""Responsive role-settings surface; presentation only, no workers or rules."""

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QLayout,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CardWidget, PushButton, isDarkTheme, qconfig


class _CenteredContent(QWidget):
    """Prefer a full-width settings surface without imposing a hard minimum."""

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(1200, hint.height())

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())


class RoleSettingsSurface(QWidget):
    """Sticky header/footer around one scrollable, centered two-card surface.

    The owning page inserts its existing controls into role_layout and
    calculation_layout. The hidden toggle is a compatibility state signal;
    the owner, rather than this widget, chooses the QStackedWidget page.
    """

    TWO_COLUMN_WIDTH = 820
    CONTENT_MAX_WIDTH = 1200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.toggle = PushButton('角色设置', self)
        self.toggle.setCheckable(True)
        self.toggle.hide()

        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        self.header, self.header_layout = self._sticky_row()
        body.addWidget(self.header)

        self.scroll = QScrollArea(self)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumSize(0, 0)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.viewport().installEventFilter(self)
        self.scroll_body = QWidget()
        self.scroll_body.setMinimumWidth(0)
        center = QHBoxLayout(self.scroll_body)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(0)
        center.setSizeConstraint(QLayout.SetNoConstraint)

        self.content = _CenteredContent(self.scroll_body)
        self.content.setMinimumWidth(0)
        self.content.setMaximumWidth(self.CONTENT_MAX_WIDTH)
        self.content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.columns = QBoxLayout(QBoxLayout.LeftToRight, self.content)
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(16)
        self.columns.setSizeConstraint(QLayout.SetNoConstraint)
        self.role_card, self.role_layout = self._card()
        self.calculation_card, self.calculation_layout = self._card()
        self.columns.addWidget(self.role_card, 1, Qt.AlignTop)
        self.columns.addWidget(self.calculation_card, 1, Qt.AlignTop)
        # Existing role builders can remain intact while the owner routes
        # calculation controls to the separate calculation_layout.
        self.content_layout = self.role_layout
        center.addWidget(self.content, 1, Qt.AlignHCenter | Qt.AlignTop)
        self.scroll.setWidget(self.scroll_body)
        body.addWidget(self.scroll, 1)
        self.footer, self.footer_layout = self._sticky_row()
        body.addWidget(self.footer)
        qconfig.themeChanged.connect(self.update_theme)
        qconfig.themeChangedFinished.connect(self.update_theme)
        self.update_theme()
        self._reflow()

    def _sticky_row(self):
        row = QWidget(self)
        row.setMinimumWidth(0)
        row.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        return row, layout

    def _card(self):
        card = CardWidget(self.content)
        card.setMinimumWidth(0)
        # Child controls' size hints must not force horizontal overflow at
        # narrow widths. Their vertical hints still determine scrolling.
        card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        layout.setAlignment(Qt.AlignTop)
        return card, layout

    def _reflow(self):
        if not hasattr(self, 'columns'):
            return
        wide = self.scroll.viewport().width() >= self.TWO_COLUMN_WIDTH
        direction = QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom
        if self.columns.direction() != direction:
            self.columns.setDirection(direction)
        self.columns.setStretch(0, 1 if wide else 0)
        self.columns.setStretch(1, 1 if wide else 0)
        # Equal outer height in the horizontal layout; each card's own body
        # remains top-aligned. In a vertical stack, use natural card heights.
        alignment = Qt.AlignmentFlag(0) if wide else Qt.AlignTop
        for index, card in enumerate((self.role_card, self.calculation_card)):
            if self.columns.itemAt(index).alignment() != alignment:
                self.columns.setAlignment(card, alignment)

    def update_theme(self, *_):
        # QScrollArea owns an auto-filled widget, which otherwise keeps the
        # light palette even after Fluent labels switch to white text. Sync
        # every structural layer explicitly, without opaque CardWidget QSS.
        dark = isDarkTheme()
        background = QColor('#202020' if dark else '#fafafa')
        text = QColor('#f3f3f3' if dark else '#202020')
        surfaces = (
            self,
            self.scroll,
            self.scroll.viewport(),
            self.scroll_body,
            self.content,
            self.role_card,
            self.calculation_card,
            self.header,
            self.footer,
        ) + tuple(self.findChildren(QWidget))
        for surface in surfaces:
            palette = surface.palette()
            for role in (QPalette.Window, QPalette.Base, QPalette.Button, QPalette.AlternateBase):
                palette.setColor(role, background)
            for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
                palette.setColor(role, text)
                palette.setColor(QPalette.Disabled, role, QColor('#858585' if dark else '#8a8a8a'))
            surface.setPalette(palette)
            surface.update()
        self.setAutoFillBackground(True)
        self.scroll.viewport().setAutoFillBackground(True)
        self.scroll_body.setAutoFillBackground(True)
        # Resolve the short old-theme background transition immediately so a
        # freshly opened page never flashes light cards behind dark-theme text.
        for card in (self.role_card, self.calculation_card):
            card.backgroundColorAni.stop()
            card.setBackgroundColor(card._normalBackgroundColor())

    def eventFilter(self, watched, event):
        if (
            hasattr(self, 'scroll')
            and watched is self.scroll.viewport()
            and event.type() == QEvent.Resize
        ):
            self._reflow()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def ensureWidgetVisible(self, widget, xmargin=50, ymargin=50):
        self.scroll.ensureWidgetVisible(widget, xmargin, ymargin)

    def verticalScrollBar(self):
        return self.scroll.verticalScrollBar()

    def viewport(self):
        return self.scroll.viewport()
