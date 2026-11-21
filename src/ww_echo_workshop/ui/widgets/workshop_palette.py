# 定义工坊明暗主题的语义颜色与原生输入控件色板。
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QWidget


BODY_PT = 12
SUBTITLE_PT = 13
TITLE_PT = 18


@dataclass(frozen=True)
class WorkshopColors:
    background: str
    panel: str
    control: str
    hover: str
    border: str
    text: str
    muted: str
    disabled: str
    accent: str
    action: str
    focus: str


# White text against the solid action colors remains above 4.5:1.
DARK = WorkshopColors(
    '#121218',
    '#1b1b23',
    '#22222b',
    '#292932',
    '#34343c',
    '#ededf2',
    '#b9b9c5',
    '#9292a0',
    '#7c5cff',
    '#7757fa',
    '#c8baff',
)
LIGHT = WorkshopColors(
    '#f7f7f8',
    '#fafafb',
    '#ffffff',
    '#eeeaf9',
    '#d5d3df',
    '#18181b',
    '#555462',
    '#646371',
    '#6844dd',
    '#5e38d1',
    '#44259d',
)


def colors_for(palette):
    return DARK if palette.color(QPalette.Window).lightness() < 128 else LIGHT


def colors_for_widget(widget=None):
    palette = None
    while widget is not None:
        if isinstance(widget, QWidget):
            dark = widget.property('workshopDark')
            if dark is not None:
                return DARK if dark else LIGHT
            if palette is None:
                palette = widget.palette()
        widget = widget.parent()
    if palette is not None:
        return colors_for(palette)
    return colors_for(QGuiApplication.palette()) if QGuiApplication.instance() else LIGHT


class _ThemeObserver(QObject):
    def __init__(self, widget, refresh):
        super().__init__(widget)
        self.refresh = refresh
        self.colors = colors_for_widget(widget)
        self.pending = QTimer(self, singleShot=True)
        self.pending.timeout.connect(self._update)
        widget.installEventFilter(self)

    def _update(self):
        colors = colors_for_widget(self.parent())
        if colors != self.colors:
            self.colors = colors
            self.refresh()

    def eventFilter(self, obj, event):
        if event.type() in (
            QEvent.PaletteChange,
            QEvent.StyleChange,
            QEvent.ParentChange,
            QEvent.Show,
        ):
            self.pending.start(0)
        return False


def watch_theme(widget, refresh):
    """Notify local, manually painted or styled elements without touching the host palette."""
    _ThemeObserver(widget, refresh)


def workshop_palette(host):
    palette = QPalette(host)
    colors = colors_for(host)
    for role, color in (
        (QPalette.Window, colors.background),
        (QPalette.AlternateBase, colors.panel),
        (QPalette.Base, colors.control),
        (QPalette.Button, colors.control),
        (QPalette.Mid, colors.border),
        (QPalette.Dark, colors.border),
        (QPalette.Highlight, colors.action),
        (QPalette.HighlightedText, '#ffffff'),
        (QPalette.PlaceholderText, colors.muted),
        (QPalette.ToolTipBase, colors.panel),
        (QPalette.ToolTipText, colors.text),
        (QPalette.Link, colors.focus),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(role, QColor(colors.text))
        palette.setColor(QPalette.Disabled, role, QColor(colors.disabled))
    return palette, colors


def workshop_styles(colors):
    # This sheet is installed on the workshop root, not QApplication.
    system_pt = QGuiApplication.font().pointSizeF()
    body_pt = max(BODY_PT, system_pt)
    return f"""
QWidget {{ font-size: {body_pt}pt; }}
SubtitleLabel {{ font-size: {body_pt + SUBTITLE_PT - BODY_PT}pt; font-weight: bold; }}
TitleLabel {{ font-size: {body_pt + TITLE_PT - BODY_PT}pt; font-weight: bold; }}
QWidget#echoAssistantPanel, QWidget#workspaceBody, QStackedWidget, QDialog {{
    background-color: {colors.background}; color: {colors.text};
}}
QScrollArea {{ background-color: {colors.background}; }}
QScrollArea > QWidget > QWidget {{ background-color: {colors.background}; }}
WorkshopCheckBox {{ padding: 0; spacing: 8px; }}
WorkshopCheckBox::indicator {{ width: 16px; height: 16px; border: 0; }}
QLabel, QAbstractButton, QComboBox, QAbstractSpinBox, QLineEdit, QGroupBox {{ color: {colors.text}; }}
CaptionLabel {{ color: {colors.muted}; }}
QLabel:disabled, QAbstractButton:disabled, QComboBox:disabled, QAbstractSpinBox:disabled, QLineEdit:disabled {{
    color: {colors.disabled};
}}
QPushButton, QComboBox, QAbstractSpinBox, QLineEdit, QAbstractSpinBox QLineEdit {{
    background-color: {colors.control}; border-color: {colors.border};
}}
QComboBox {{ padding-left: 8px; padding-right: 23px; }}
QAbstractSpinBox QLineEdit {{ background-color: transparent; }}
QPushButton:enabled:hover, QComboBox:enabled:hover, QAbstractSpinBox:enabled:hover, QLineEdit:enabled:hover {{
    background-color: {colors.hover};
}}
QPushButton:checked {{ background-color: {colors.hover}; border-color: {colors.accent}; }}
PrimaryPushButton:enabled {{ color: #ffffff; background-color: {colors.action}; }}
PrimaryPushButton:enabled:hover {{ background-color: {colors.action}; border-color: {colors.focus}; }}
QPushButton:focus, QComboBox:focus, QComboBox:on, QAbstractSpinBox:focus, QLineEdit:focus {{
    border-color: {colors.focus};
}}
QAbstractItemView, QTextEdit, QPlainTextEdit {{
    color: {colors.text}; background-color: {colors.panel}; alternate-background-color: {colors.control};
    selection-background-color: {colors.action}; selection-color: #ffffff; border-color: {colors.border};
}}
QTableView {{ gridline-color: {colors.border}; }}
QHeaderView, QHeaderView::section, QTableCornerButton::section {{
    color: {colors.text}; background-color: {colors.control}; border-color: {colors.border};
}}
QComboBox QAbstractItemView, QMenu, QToolTip {{
    color: {colors.text}; background-color: {colors.control}; border-color: {colors.border};
    selection-background-color: {colors.action}; selection-color: #ffffff;
}}
QMenu::item:selected {{ color: #ffffff; background-color: {colors.action}; }}
QScrollBar:vertical {{ width: 11px; background: {colors.background}; margin: 0; }}
QScrollBar:horizontal {{ height: 11px; background: {colors.background}; margin: 0; }}
QScrollBar::handle {{ background: {colors.disabled}; border: 2px solid {colors.background}; border-radius: 5px; }}
QScrollBar::handle:vertical {{ min-height: 30px; }}
QScrollBar::handle:horizontal {{ min-width: 30px; }}
QScrollBar::handle:hover {{ background: {colors.accent}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QFrame#PanelSourceBox {{ border: 1px solid {colors.border}; border-radius: 6px; }}
"""
