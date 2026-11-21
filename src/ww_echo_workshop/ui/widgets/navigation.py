# 绘制工坊顶部模式导航，并把选中状态交还给页面路由。
from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QSizePolicy, QWidget

from ww_echo_workshop.ui.widgets.workshop_palette import colors_for_widget


class _ModeButton(QAbstractButton):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setAccessibleName(text)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def nextCheckState(self):
        # A blocked route must leave the previous item checked.
        pass

    def sizeHint(self):
        metrics = self.fontMetrics()
        return QSize(
            max(64, metrics.horizontalAdvance(self.text()) + 28), max(32, metrics.height() + 12)
        )

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        colors = colors_for_widget(self)
        enabled = self.isEnabled()
        selected = self.isChecked()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        selected_or_hovered = enabled and (selected or self.underMouse())
        if enabled and (self.isDown() or selected_or_hovered):
            fill = colors.hover if self.isDown() or self.underMouse() else colors.panel
            painter.setBrush(QColor(fill))
            painter.setPen(QColor(colors.border))
            painter.drawRoundedRect(body, 6, 6)
        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors.accent if enabled else colors.disabled))
            painter.drawRoundedRect(QRectF(10, self.height() - 3, self.width() - 20, 2), 1, 1)
        painter.setPen(QColor(colors.text if enabled else colors.disabled))
        painter.drawText(self.rect(), Qt.AlignCenter, self.text())
        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(colors.focus), 1, Qt.DashLine))
            painter.drawRoundedRect(body.adjusted(1, 1, -1, -1), 5, 5)
        painter.end()


class ModeNavigation(QWidget):
    """One strip on both hosts; Qt buttons expose checked/focus semantics."""

    def __init__(self, parent=None, *, accessible_name='声骸工坊模式'):
        super().__init__(parent)
        self.setAccessibleName(accessible_name)
        self._items = {}
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(8)
        self._row.addStretch()

    def addItem(self, key, text, onClick):
        button = _ModeButton(text, self)
        button.setObjectName(f'workshopMode-{key}')
        button.installEventFilter(self)
        button.clicked.connect(onClick)
        self._items[key] = button
        self._row.insertWidget(self._row.count() - 1, button)

    def setCurrentItem(self, key):
        if key in self._items:
            for identity, button in self._items.items():
                button.setChecked(identity == key)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and isinstance(watched, _ModeButton):
            key = event.key()
            if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End):
                items = [button for button in self._items.values() if button.isEnabled()]
                if watched in items:
                    index = items.index(watched)
                    step = -1 if key == Qt.Key_Left else 1
                    if self.layoutDirection() == Qt.RightToLeft:
                        step = -step
                    target = {Qt.Key_Home: 0, Qt.Key_End: -1}.get(key, (index + step) % len(items))
                    items[target].setFocus(Qt.OtherFocusReason)
                    items[target].click()
                    return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if not event.isAutoRepeat():
                    watched.click()
                return True
        return super().eventFilter(watched, event)
