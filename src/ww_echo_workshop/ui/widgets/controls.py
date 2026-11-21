# 提供工坊共用的 Qt 控件、主题投影与操作反馈。
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyleOptionButton,
)
from ww_echo_workshop.ui.widgets.workshop_palette import DARK, colors_for_widget


def is_dark_theme(widget=None):
    return colors_for_widget(widget) is DARK


def accent_color(widget=None):
    return QColor(colors_for_widget(widget).accent)


class CaptionLabel(QLabel):
    """Secondary copy uses color, not a reduced point size."""


class SubtitleLabel(QLabel):
    def __init__(self, *args):
        super().__init__(*args)
        font = self.font()
        font.setBold(True)
        self.setFont(font)


class TitleLabel(SubtitleLabel):
    """Marks the primary title for the workshop style sheet."""


class CardWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)

    def paintEvent(self, event):
        # Keep StyledPanel's layout metrics, but never hand the surface back to
        # the host frame painter after drawing the workshop colors.
        colors = colors_for_widget(self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor(colors.border))
        painter.setBrush(QColor(colors.panel))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        painter.end()


class WorkshopCheckBox(QCheckBox):
    """Keep Qt hit boxes, state and accessibility; own the whole visual indicator."""

    def _style_rects(self):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        style = self.style()
        return (
            style.subElementRect(QStyle.SE_CheckBoxIndicator, option, self),
            style.subElementRect(QStyle.SE_CheckBoxContents, option, self),
        )

    def paintEvent(self, event):
        indicator, content = self._style_rects()
        colors = colors_for_widget(self)
        enabled = self.isEnabled()
        marked = self.checkState() != Qt.Unchecked
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        fill = colors.action if enabled and marked else colors.control
        if enabled and not marked and self.isDown():
            fill = colors.hover
        outline = colors.focus if enabled and self.underMouse() else colors.disabled
        if enabled and marked:
            outline = colors.accent
        box = QRectF(indicator).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setBrush(QColor(fill))
        painter.setPen(QColor(outline))
        painter.drawRoundedRect(box, 4, 4)
        if marked:
            ink = QPen(QColor('#ffffff' if enabled else colors.disabled), 1.65)
            ink.setCapStyle(Qt.RoundCap)
            painter.setPen(ink)

            def point(x, y):
                return QPointF(box.x() + box.width() * x, box.y() + box.height() * y)

            if self.checkState() == Qt.PartiallyChecked:
                painter.drawLine(point(0.28, 0.5), point(0.72, 0.5))
            else:
                painter.drawLine(point(0.25, 0.52), point(0.43, 0.69))
                painter.drawLine(point(0.43, 0.69), point(0.76, 0.32))
        painter.setPen(QColor(colors.text if enabled else colors.disabled))
        alignment = Qt.AlignRight if self.layoutDirection() == Qt.RightToLeft else Qt.AlignLeft
        painter.drawText(content, alignment | Qt.AlignVCenter | Qt.TextShowMnemonic, self.text())
        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            focus = QPen(QColor(colors.focus), 1, Qt.DashLine)
            painter.setPen(focus)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        painter.end()


class PrimaryPushButton(QPushButton):
    """Marks explicit primary actions for the workshop style sheet."""


class TogglePushButton(QPushButton):
    def __init__(self, *args):
        super().__init__(*args)
        self.setCheckable(True)


def standard_icon(kind):
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app.style().standardIcon(QStyle.StandardPixmap[kind])
    return QIcon()


def confirm_action(parent, title, message):
    return (
        QMessageBox.question(
            parent, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        == QMessageBox.Yes
    )


def show_notice(parent, content, *, title='', duration, warning=False, closable=False):
    banner = QFrame(parent)
    banner.setAttribute(Qt.WA_DeleteOnClose)
    banner.setFrameShape(QFrame.StyledPanel)
    banner.setAutoFillBackground(True)
    layout = QHBoxLayout(banner)
    if title or warning:
        layout.addWidget(SubtitleLabel(title or '提示', banner))
    label = QLabel(content, banner)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setMaximumWidth(max(120, min(560, parent.width() - 84)))
    layout.addWidget(label)
    if closable:
        button = QPushButton('关闭', banner)
        button.clicked.connect(banner.close)
        layout.addWidget(button)
    banner.adjustSize()
    banner.move(max(0, (parent.width() - banner.width()) // 2), 16)
    banner.show()
    banner.raise_()
    timer = QTimer(banner)
    timer.setSingleShot(True)
    timer.timeout.connect(banner.close)
    timer.start(duration)
    return banner
