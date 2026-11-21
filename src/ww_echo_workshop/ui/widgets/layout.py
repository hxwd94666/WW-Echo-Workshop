# 提供页面布局和明确导航的小型组件。
"""Small Qt presentation helpers; capture pixels and UI pixels stay separate."""

from PySide6.QtCore import QRect, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from ww_echo_workshop.ui.inventory.card_data import stat_value_text


def stat_text(stat):
    return f'{stat.stat_name} {stat_value_text(stat)}'


def _fit_dialog_geometry(dialog, width, height, area, owner=None):
    """Keep client geometry inside one screen, including oversized minimums."""
    margin_x = min(20, max(0, (area.width() - 1) // 4))
    margin_y = min(30, max(0, (area.height() - 1) // 4))
    bounds = area.adjusted(margin_x, margin_y, -margin_x, -margin_y)
    width = min(max(1, int(width)), bounds.width())
    height = min(max(1, int(height)), bounds.height())
    # A large dialog minimum must not override the available screen size.
    # Callers keep long content in scroll areas and actions outside those areas.
    dialog.setMinimumSize(min(dialog.minimumWidth(), width), min(dialog.minimumHeight(), height))
    if dialog.layout() is not None:
        dialog.layout().setSizeConstraint(QLayout.SetNoConstraint)
    center = owner.frameGeometry().center() if owner is not None else bounds.center()
    x = min(max(center.x() - width // 2, bounds.left()), bounds.right() - width + 1)
    y = min(max(center.y() - height // 2, bounds.top()), bounds.bottom() - height + 1)
    dialog.setGeometry(QRect(x, y, width, height))


def fit_dialog(dialog, width, height):
    """Size and center on the owner's screen, then settle after Qt lays out."""
    parent = dialog.parentWidget()
    # 内嵌页面的 frameGeometry 是父级局部坐标，须使用顶层窗口的屏幕坐标。
    owner = parent.window() if parent is not None else None

    def fit():
        screen = (owner.screen() if owner is not None else None) or dialog.screen()
        screen = screen or QApplication.primaryScreen()
        if screen is None:
            dialog.resize(width, height)
            return
        _fit_dialog_geometry(dialog, width, height, screen.availableGeometry(), owner)

    fit()
    # Context-bound callback is dropped if the dialog is destroyed before show.
    QTimer.singleShot(0, dialog, fit)


class Disclosure(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = QPushButton('展开：' + title)
        self.toggle.setCheckable(True)
        self.content = QScrollArea()
        self.content.setWidgetResizable(True)
        self.content.setMaximumHeight(220)
        widget = QWidget()
        self.content_layout = QVBoxLayout(widget)
        self.content.setWidget(widget)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.content.hide()

        def show(checked):
            self.content.setVisible(checked)
            self.toggle.setText(('收起：' if checked else '展开：') + title)

        self.toggle.toggled.connect(show)


def add_home_button(window, return_home):
    button = QPushButton('返回声骸工坊', window)
    button.clicked.connect(return_home)
    window.layout().insertWidget(0, button)
    window._echo_home_button = button
    fit_dialog(window, window.width(), window.height())
