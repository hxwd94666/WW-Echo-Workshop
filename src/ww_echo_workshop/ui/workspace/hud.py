# 实时评分透明悬浮窗：默认可拖拽，锁定后鼠标穿透，详情展开设置。
import html
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ww_echo_workshop.application.live_preferences import LiveOption as Option
from ww_echo_workshop.application.hud_state import (
    HUD_COMPARISON_SEPARATOR_RGB,
    HUD_POLICY_BUSY,
    HUD_POLICY_HEADER,
)
from ww_echo_workshop.core.policy_presets import SYSTEMS
from ww_echo_workshop.ui.widgets.roles import RolePicker

DEFAULT_FONT_SIZE = 15
_WAITING_SCORE_ROWS = (('等待新的评分画面…', None),)
_CHECK_STYLE = (
    'QCheckBox { color: #f4f4f4; background: rgba(42, 48, 60, 165);'
    ' border-radius: 5px; padding: 3px 6px; }'
    'QCheckBox::indicator { width: 16px; height: 16px;'
    ' border: 2px solid #ffffff; border-radius: 2px; background: transparent; }'
    'QCheckBox::indicator:checked { background: #ffffff;'
    ' border: 2px solid #ffffff; }'
)
LAYOUT_ANCHORS = {
    'enhancement': (0.375, 0.32),
    'bag': (0.53, 0.50),
    'display': (0.59, 0.27),
    'comparison': (0.43, 0.22),
    'tuning': (0.375, 0.32),
    'single': (0.375, 0.32),
}
# The right-hand echo detail panel starts at roughly 77% of the viewport.
RIGHT_DETAIL_LEFT = 0.77
# The comparison left stat panel must not be covered by the HUD.
COMPARISON_LEFT = 0.56


def _hud_icon(kind, color='#7dd3fc', size=24):
    """Draw tiny flat HUD icons without adding external assets."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(color), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    if kind == 'lock':
        painter.drawRoundedRect(5, 11, 14, 9, 2, 2)
        painter.drawArc(8, 4, 8, 10, 0, 180 * 16)
    elif kind == 'unlock':
        painter.drawRoundedRect(5, 11, 14, 9, 2, 2)
        painter.drawArc(11, 4, 8, 10, 20 * 16, 150 * 16)
    else:
        for y in (7, 12, 17):
            painter.drawLine(5, y, 19, y)
    painter.end()
    return QIcon(pixmap)


def _icon_button(parent, kind, tooltip, color='#7dd3fc'):
    button = QPushButton(parent)
    button.setIcon(_hud_icon(kind, color))
    button.setIconSize(QSize(20, 20))
    button.setFixedSize(30, 28)
    button.setFlat(True)
    button.setAutoFillBackground(False)
    button.setText('')
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setStyleSheet(
        'QPushButton, QPushButton:hover, QPushButton:pressed, QPushButton:focus {'
        ' background-color: transparent; border: none; padding: 0; outline: none; }'
    )
    return button


def layout_position(layout, viewport, width, ratio=1.0):
    """Return logical HUD coordinates for a physical-pixel viewport."""
    try:
        vx, vy, vw, vh = (int(value) for value in viewport)
        width = int(width)
        ratio = float(ratio)
    except (TypeError, ValueError):
        raise ValueError('Invalid HUD layout geometry') from None
    if width <= 0 or vw <= 0 or vh <= 0 or ratio <= 0:
        raise ValueError('Invalid HUD layout geometry')
    anchor = LAYOUT_ANCHORS.get(layout, (0.02, 0.20))
    x = round((vx + vw * anchor[0]) / ratio)
    y = round((vy + vh * anchor[1]) / ratio)
    if layout in ('display', 'bag'):
        panel_left = round((vx + vw * RIGHT_DETAIL_LEFT) / ratio)
        x = min(x, panel_left - width - 12)
    elif layout == 'comparison':
        panel_left = round((vx + vw * COMPARISON_LEFT) / ratio)
        x = min(x, panel_left - width - 12)
    return x, y


class NoWheelGuard(QObject):
    def eventFilter(self, source, event):
        return event.type() == QEvent.Wheel or super().eventFilter(source, event)


class _PolicyLine(QLabel):
    """Reserve one line vertically without affecting the original HUD width."""

    def __init__(self, parent):
        super().__init__(parent)
        self._full_text = ''
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_value(self, text):
        self._full_text = text
        self.setAccessibleName(text)
        self.setFixedHeight(self.fontMetrics().height() + 1)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        width = self.contentsRect().width()
        text = self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, width)
        self.setText(text)
        self.setToolTip(self._full_text if text != self._full_text else '')


class HudWindow(QWidget):
    unlock_requested = Signal()

    def __init__(self, preferences, weight_snapshot, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self._weight_snapshot = weight_snapshot
        self._locked = False
        self._details = False
        self._drag = None
        self._rows = ()
        self._line_labels = []
        self._wheel_guard = NoWheelGuard(self)
        self._last_layout = None
        self._last_interaction = 0.0
        self._window_hidden_since = 0.0
        self._needs_fit = True
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle('实时评分悬浮窗')
        self.setMinimumWidth(300)
        self.setMaximumWidth(620)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame(self)
        self.panel.setObjectName('hudPanel')
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(14, 9, 14, 11)
        panel_layout.setSpacing(4)
        self.drag_label = QLabel('按住拖动', self.panel)
        self.drag_label.setStyleSheet('color: #7dd3fc; font-weight: 600; font-size: 11px;')
        panel_layout.addWidget(self.drag_label)
        self.lines_host = QWidget(self.panel)
        self.lines_layout = QVBoxLayout(self.lines_host)
        self.lines_layout.setContentsMargins(0, 0, 0, 0)
        self.lines_layout.setSpacing(2)
        panel_layout.addWidget(self.lines_host)
        self.normal_buttons = QWidget(self.panel)
        normal_row = QHBoxLayout(self.normal_buttons)
        normal_row.setContentsMargins(0, 4, 0, 0)
        self.lock_button = _icon_button(self.normal_buttons, 'lock', '锁定悬浮窗')
        self.lock_button.clicked.connect(self.lock)
        self.details_button = _icon_button(self.normal_buttons, 'menu', '详情')
        self.details_button.clicked.connect(self.toggle_details)
        normal_row.addStretch()
        normal_row.addWidget(self.lock_button)
        normal_row.addWidget(self.details_button)
        panel_layout.addWidget(self.normal_buttons)
        self.unlock_button = _icon_button(None, 'unlock', '解除悬浮窗锁定（Ctrl+Alt+L）', '#e8f6ff')
        self.unlock_button.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        )
        self.unlock_button.setAttribute(Qt.WA_ShowWithoutActivating)
        self.unlock_button.setAttribute(Qt.WA_TranslucentBackground)
        self.unlock_button.setFocusPolicy(Qt.NoFocus)
        self.unlock_button.setFixedSize(30, 28)
        self.unlock_button.clicked.connect(self._request_normal)
        self.unlock_button.hide()
        self.edit_panel = QWidget(self.panel)
        grid = QGridLayout(self.edit_panel)
        grid.setContentsMargins(0, 5, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        self.live_check = QCheckBox('实时评分', self.edit_panel)
        self.live_check.setChecked(bool(preferences.get(Option.DISPLAY_SCORE, True)))
        self.live_check.toggled.connect(
            lambda checked: self.preferences.set(Option.DISPLAY_SCORE, bool(checked))
        )
        self.auto_check = QCheckBox('跟随角色', self.edit_panel)
        self.auto_check.setChecked(bool(preferences.get(Option.FOLLOW_ROLE, False)))
        self.auto_check.toggled.connect(
            lambda checked: self.preferences.set(Option.FOLLOW_ROLE, bool(checked))
        )
        self.policy_check = QCheckBox('策略建议', self.edit_panel)
        self.policy_check.setChecked(bool(preferences.get(Option.SHOW_POLICY, False)))
        self.policy_check.toggled.connect(
            lambda checked: self.preferences.set(Option.SHOW_POLICY, bool(checked))
        )
        self.hud_follow_check = QCheckBox('悬浮窗位置变换（仅适配2K）', self.edit_panel)
        self.hud_follow_check.setChecked(bool(preferences.get(Option.HUD_AUTO_POSITION, False)))
        self.hud_follow_check.toggled.connect(self._position_changed)
        checks_row = QWidget(self.edit_panel)
        checks_row.setObjectName('hudChecks')
        checks_layout = QHBoxLayout(checks_row)
        checks_layout.setContentsMargins(0, 0, 0, 0)
        checks_layout.setSpacing(12)
        for check in (
            self.live_check,
            self.hud_follow_check,
            self.auto_check,
            self.policy_check,
        ):
            check.setStyleSheet(_CHECK_STYLE)
            checks_layout.addWidget(check)
        checks_layout.addStretch()
        grid.addWidget(checks_row, 0, 0, 1, 4)
        grid.addWidget(QLabel('字号', self.edit_panel), 1, 0)
        self.font_size = QDoubleSpinBox(self.edit_panel)
        self.font_size.setRange(12.0, 32.0)
        self.font_size.setDecimals(0)
        self.font_size.setSingleStep(1.0)
        self.font_size.setValue(float(preferences.get(Option.HUD_FONT_SIZE) or DEFAULT_FONT_SIZE))
        self.font_size.valueChanged.connect(self._font_changed)
        grid.addWidget(self.font_size, 1, 1)
        grid.addWidget(QLabel('评分体系', self.edit_panel), 1, 2)
        self.scoring = QComboBox(self.edit_panel)
        self.scoring.addItem('XWUID（工坊默认）', userData='workshop_xwuid')
        for key, spec in SYSTEMS.items():
            if key != 'qq_bot':
                self.scoring.addItem(spec['label'], userData=key)
        index = self.scoring.findData(preferences.get(Option.SCORING_SYSTEM, 'workshop_xwuid'))
        self.scoring.setCurrentIndex(index if index >= 0 else 0)
        self.scoring.currentIndexChanged.connect(self._scoring_changed)
        grid.addWidget(self.scoring, 1, 3)
        grid.addWidget(QLabel('模式', self.edit_panel), 2, 0)
        self.mode = QComboBox(self.edit_panel)
        for label, value in (
            ('自动', 'auto'),
            ('仅强化', 'upgrade'),
            ('仅重抽', 'reroll'),
            ('联合（实验）', 'joint'),
        ):
            self.mode.addItem(label, userData=value)
        index = self.mode.findData(preferences.get(Option.POLICY_MODE, 'auto'))
        self.mode.setCurrentIndex(index if index >= 0 else 0)
        self.mode.currentIndexChanged.connect(
            lambda _: self.preferences.set(Option.POLICY_MODE, self.mode.currentData())
        )
        grid.addWidget(self.mode, 2, 1)
        grid.addWidget(QLabel('目标分', self.edit_panel), 2, 2)
        self.target = QDoubleSpinBox(self.edit_panel)
        self.target.setRange(0.0, 655.35)
        self.target.setDecimals(1)
        self.target.setValue(float(preferences.get(Option.TARGET) or 0.0))
        self.target.valueChanged.connect(
            lambda value: self.preferences.set(Option.TARGET, round(float(value), 1))
        )
        grid.addWidget(self.target, 2, 3)
        self.role_picker = None
        weights = self._weight_snapshot()
        if weights is not None:
            configured = preferences.role_key()
            self.role_picker = RolePicker(
                weights, self.edit_panel, configured if all(configured) else None
            )
            self.role_picker.hint.hide()
            self.role_picker.changed.connect(self._apply_role)
            grid.addWidget(self.role_picker, 3, 0, 1, 4)
        resources = QWidget(self.edit_panel)
        res_grid = QGridLayout(resources)
        res_grid.setContentsMargins(0, 0, 0, 0)
        self.resource_boxes = []
        for idx, (label, value) in enumerate(
            zip(('胚子', '调谐器', '密音筒', '频整器'), preferences.costs())
        ):
            box = QDoubleSpinBox(resources)
            box.setRange(0.0, 1_000_000_000.0)
            box.setDecimals(3)
            box.setValue(float(value))
            box.valueChanged.connect(self._resource_changed)
            row, column = divmod(idx, 4)
            res_grid.addWidget(QLabel(label, resources), row, column)
            res_grid.addWidget(box, row + 1, column)
            self.resource_boxes.append(box)
        grid.addWidget(QLabel('资源价值', self.edit_panel), 4, 0)
        grid.addWidget(resources, 4, 1, 1, 3)
        self.edit_panel.setStyleSheet(
            'background: transparent; color: #f4f4f4;'
            'QLabel { color: #f4f4f4; }'
            'QCheckBox { color: #f4f4f4; background: rgba(42, 48, 60, 165);'
            ' border-radius: 5px; padding: 3px 6px; }'
            'QCheckBox::indicator { width: 14px; height: 14px;'
            ' border: 1px solid #ffffff; border-radius: 2px; background: transparent; }'
            'QCheckBox::indicator:checked { background: #ffffff;'
            ' border: 1px solid #ffffff; }'
            'QComboBox, QDoubleSpinBox { color: #f4f4f4;'
            ' background: rgba(42, 48, 60, 240);'
            ' border: 1px solid rgba(125, 211, 252, 35); border-radius: 5px;'
            ' padding: 2px 4px; }'
            'QComboBox QAbstractItemView { color: #f4f4f4; background: #2a303c; }'
            'QPushButton { color: #f4f4f4; background: rgba(55, 64, 78, 230);'
            ' border-radius: 5px; padding: 3px 8px; }'
        )
        wheel_controls = [
            self.font_size,
            self.scoring,
            self.mode,
            self.target,
            *self.resource_boxes,
        ]
        if self.role_picker is not None:
            wheel_controls.extend(self.role_picker.findChildren(QComboBox))
        for control in wheel_controls:
            control.installEventFilter(self._wheel_guard)
        panel_layout.addWidget(self.edit_panel)
        root.addWidget(self.panel)
        for target in (self.drag_label, self.lines_host, self.panel):
            target.installEventFilter(self)
        self.edit_panel.hide()
        self.drag_label.hide()
        self._apply_style()
        self._restore_position()
        self.normal()

    def _font_size(self):
        try:
            return max(
                12,
                min(32, int(self.preferences.get(Option.HUD_FONT_SIZE) or DEFAULT_FONT_SIZE)),
            )
        except (TypeError, ValueError):
            return DEFAULT_FONT_SIZE

    def _font_changed(self, value):
        self.preferences.set(Option.HUD_FONT_SIZE, int(value))
        self._apply_font()

    def _apply_font(self):
        self.set_rows(self._rows, force=True)

    def _scoring_changed(self, *_):
        system = self.scoring.currentData() or 'workshop_xwuid'
        self.preferences.set(Option.SCORING_SYSTEM, system)
        self.target.setRange(
            0.0,
            50.0 if system == 'workshop_xwuid' else (65535.0 if system == 'fixed' else 655.35),
        )

    def _resource_changed(self, *_):
        self.preferences.save_costs(box.value() for box in self.resource_boxes)

    def _apply_role(self):
        if self.role_picker is None:
            return
        key = self.role_picker.key()
        if key is None:
            return
        try:
            template = self._weight_snapshot().template(*key)
        except (AttributeError, KeyError, ValueError):
            return
        self.preferences.save_role(key, template['name'])

    def _apply_style(self):
        if self._locked:
            border = '1px solid rgba(125, 211, 252, 20)'
            background = 'rgba(20, 24, 32, 165)'
        elif self._details:
            border = '1px solid rgba(125, 211, 252, 220)'
            background = 'rgba(20, 24, 32, 230)'
        else:
            border = '1px solid rgba(125, 211, 252, 90)'
            background = 'rgba(20, 24, 32, 195)'
        self.panel.setStyleSheet(
            '#hudPanel { background: %s; border: %s; '
            'border-left: 3px solid rgba(125, 211, 252, 190); border-radius: 12px; }'
            % (background, border)
        )

    def _restore_position(self):
        self.move(QPoint(*self.preferences.position()))

    def _save_position(self):
        self.preferences.save_position(self.x(), self.y())

    def _fit(self):
        self.layout().activate()
        hint = self.sizeHint()
        if hint.isValid():
            self.resize(hint)
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            if self.height() > area.height():
                self.resize(self.width(), area.height())
            if self.width() > area.width():
                self.resize(area.width(), self.height())
            x = min(max(self.x(), area.left()), area.right() - self.width())
            y = min(max(self.y(), area.top()), area.bottom() - self.height())
            self.move(x, y)

    def move_for_layout(self, layout, viewport):
        if not self.preferences.get(Option.HUD_AUTO_POSITION, False):
            return
        if self._locked or self._details or self._drag is not None:
            return
        if not viewport:
            return
        screen = self.screen() or QApplication.primaryScreen()
        # Capture/viewport bounds are physical pixels (for example 2560x1440),
        # while Qt window coordinates are device-independent logical pixels.
        # At 125% Windows scaling these differ by 1.25 and every HUD position
        # otherwise lands too far right/down.
        ratio = float(screen.devicePixelRatio()) if screen is not None else 1.0
        ratio = ratio if ratio > 0 else 1.0
        try:
            x, y = layout_position(layout, viewport, self.width(), ratio)
        except ValueError:
            return
        if screen is not None:
            area = screen.availableGeometry()
            x = min(max(x, area.left()), area.right() - self.width())
            y = min(max(y, area.top()), area.bottom() - self.height())
        self.move(x, y)

    @staticmethod
    def _rgb(value):
        if not isinstance(value, (tuple, list)) or len(value) != 3:
            return None
        try:
            return tuple(max(0, min(255, int(v))) for v in value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _format_row(cls, text, colour):
        if not colour:
            return str(text), 'color: #f4f4f4;', False
        if isinstance(colour, (tuple, list)) and len(colour) == 2 and ' / ' in str(text):
            left, right = str(text).split(' / ', 1)
            left_color, right_color = (cls._rgb(item) or (244, 244, 244) for item in colour)
            return (
                f'<span style="color: rgb{left_color};">{html.escape(left)}</span>'
                f'<span style="color: rgb{HUD_COMPARISON_SEPARATOR_RGB};"> / </span>'
                f'<span style="color: rgb{right_color};">{html.escape(right)}</span>',
                'background: transparent;',
                True,
            )
        if isinstance(colour, (tuple, list)) and colour and isinstance(colour[0], (tuple, list)):
            parts = [cls._rgb(item) for item in colour]
            parts = [item for item in parts if item is not None]
            colour = parts[0] if parts else None
        rgb = cls._rgb(colour)
        if rgb is None:
            return str(text), 'color: #f4f4f4;', False
        return str(text), 'color: rgb(%d, %d, %d);' % rgb, False

    def set_rows(self, rows, *, force=False):
        rows = tuple(rows or ())
        if not force and rows == self._rows:
            return
        policy_start = next(
            (index for index, (text, _) in enumerate(rows) if text == HUD_POLICY_HEADER), len(rows)
        )
        rebuild = len(rows) != len(self._line_labels) or any(
            isinstance(label, _PolicyLine) != (index >= policy_start)
            for index, label in enumerate(self._line_labels)
        )
        self._rows = rows
        px = self._font_size()
        if rebuild:
            for label in self._line_labels:
                self.lines_layout.removeWidget(label)
                label.hide()
                label.deleteLater()
            self._line_labels = []
        for index, (text, colour) in enumerate(rows):
            if rebuild:
                policy_line = index >= policy_start
                label = _PolicyLine(self.lines_host) if policy_line else QLabel(self.lines_host)
                label.setWordWrap(not policy_line)
                label.setTextInteractionFlags(Qt.NoTextInteraction)
                label.installEventFilter(self)
                self.lines_layout.addWidget(label)
                self._line_labels.append(label)
            label = self._line_labels[index]
            display, style, rich = self._format_row(text, colour)
            font = label.font()
            font.setPixelSize(px)
            label.setFont(font)
            label.setStyleSheet(style)
            if isinstance(label, _PolicyLine):
                label.set_value(text)
                continue
            if label.text() != display:
                label.setTextFormat(Qt.RichText if rich else Qt.PlainText)
                label.setText(display)
            elif bool(label.textFormat() == Qt.RichText) != rich:
                label.setTextFormat(Qt.RichText if rich else Qt.PlainText)
        if rebuild or force:
            self._needs_fit = True
        if self.isVisible() and self._drag is None:
            self._fit()
            self._needs_fit = False

    def showEvent(self, event):
        super().showEvent(event)
        if self._needs_fit:
            self._fit()
            self._needs_fit = False
        if self._locked:
            self._position_unlock()
            self.unlock_button.show()

    def hideEvent(self, event):
        self.unlock_button.hide()
        super().hideEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        if self._locked:
            self._position_unlock()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._locked:
            self._position_unlock()

    def closeEvent(self, event):
        self.unlock_button.close()
        super().closeEvent(event)

    def _request_normal(self):
        self.normal()

    def _position_changed(self, checked):
        self.preferences.set(Option.HUD_AUTO_POSITION, bool(checked))
        if checked:
            self._last_layout = None

    def _position_unlock(self):
        self.unlock_button.adjustSize()
        screen = self.screen() or QApplication.primaryScreen()
        x = self.x() + self.width() - self.unlock_button.width() - 8
        y = self.y() + 8
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - self.unlock_button.width()))
            y = max(area.top(), min(y, area.bottom() - self.unlock_button.height()))
        self.unlock_button.move(x, y)

    def normal(self):
        if self._locked:
            self.unlock_requested.emit()
            return
        self.hide()
        self._details = False
        self._last_interaction = time.monotonic()
        self.edit_panel.hide()
        self.drag_label.show()
        self.normal_buttons.show()
        self.unlock_button.hide()
        self.details_button.setToolTip('详情')
        self.setWindowFlag(Qt.WindowTransparentForInput, False)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._apply_style()
        self._fit()
        self.show()

    def lock(self):
        self._locked = True
        self._details = False
        self.edit_panel.hide()
        self.drag_label.hide()
        self.normal_buttons.hide()
        self._position_unlock()
        self.unlock_button.show()
        self.unlock_button.raise_()
        self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._apply_style()
        self._fit()
        self.show()

    def edit(self):
        self.hide()
        self._locked = False
        self._details = True
        self._last_interaction = time.monotonic()
        self.edit_panel.show()
        self.drag_label.show()
        self.normal_buttons.show()
        self.unlock_button.hide()
        self.details_button.setToolTip('收起')
        self.setWindowFlag(Qt.WindowTransparentForInput, False)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, False)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._apply_style()
        self._fit()
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle_details(self):
        if self._details:
            self.normal()
        else:
            self.edit()

    def eventFilter(self, source, event):
        can_drag = not self._locked
        if can_drag and (
            source in (self.drag_label, self.lines_host, self.panel) or source in self._line_labels
        ):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag = event.globalPosition().toPoint() - self.pos()
                return True
            if event.type() == QEvent.MouseMove and self._drag is not None:
                self.move(event.globalPosition().toPoint() - self._drag)
                return True
            if event.type() == QEvent.MouseButtonRelease and self._drag is not None:
                self._drag = None
                self._last_interaction = time.monotonic()
                self._save_position()
                self._fit()
                return True
        return super().eventFilter(source, event)

    def mousePressEvent(self, event):
        if not self._locked and event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag is not None and not self._locked:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self._last_interaction = time.monotonic()
            self._save_position()
            self._fit()


class HudController(QObject):
    def __init__(self, preferences, parent=None, *, weight_snapshot, state, closed):
        super().__init__(parent)
        self.preferences = preferences
        self._weight_snapshot = weight_snapshot
        self._state = state
        self._closed = closed
        self.window = HudWindow(preferences, weight_snapshot)
        self.window.unlock_requested.connect(self._rebuild_window)
        self._hidden_since = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.refresh()

    def _mode(self):
        return self.preferences.get(Option.HUD_MODE, 'qt')

    def reset_auto_position(self):
        self.window._last_layout = None

    def refresh(self):
        if self._state is None:
            self.window.hide()
            return
        snapshot = self._state.snapshot()
        if self._mode() == 'qt' and snapshot['visible']:
            self.window.set_rows(snapshot['rows'])
            self.window.show()
        else:
            self.window.hide()

    def close(self):
        self.timer.stop()
        self.window.close()

    def _rebuild_window(self):
        old = self.window
        rows = self._state.snapshot()['rows'] if self._state is not None else ()
        position = old.pos()
        layout = old._last_layout
        old.close()
        window = HudWindow(self.preferences, self._weight_snapshot)
        window._last_layout = layout
        window.set_rows(rows or self._waiting_rows())
        window.move(position)
        window.unlock_requested.connect(self._rebuild_window)
        self.window = window
        window.normal()
        window.show()

    def toggle_edit(self):
        if self._mode() != 'qt':
            return
        if self.window._locked:
            self._rebuild_window()
        else:
            self.window.lock()

    def _waiting_rows(self):
        if not self.preferences.get(Option.SHOW_POLICY, False):
            return _WAITING_SCORE_ROWS
        return (
            *_WAITING_SCORE_ROWS,
            (HUD_POLICY_HEADER, None),
            ('策略：' + HUD_POLICY_BUSY, None),
            ('期望：' + HUD_POLICY_BUSY, None),
        )

    def _tick(self):
        if self._closed():
            self.timer.stop()
            self.window.close()
            return
        if (
            self._state is None
            or self._mode() != 'qt'
            or not self.preferences.get(Option.DISPLAY_SCORE, True)
        ):
            self.window.hide()
            return
        snapshot = self._state.snapshot()
        if not snapshot['visible']:
            if self.window._details or self.window._drag is not None:
                self.window.set_rows(self._waiting_rows())
                if not self.window.isVisible():
                    self.window.show()
                return
            now = time.monotonic()
            if now - self.window._last_interaction < 0.8:
                return
            if self._hidden_since == 0.0:
                self._hidden_since = now
            if now - self._hidden_since >= 0.35:
                self.window.hide()
            return
        self._hidden_since = 0.0
        self.window.set_rows(snapshot['rows'])
        viewport = tuple(snapshot.get('viewport') or ())
        position_key = (snapshot.get('layout'), viewport)
        if snapshot.get('layout') and position_key != self.window._last_layout:
            self.window._last_layout = position_key
            self.window.move_for_layout(snapshot['layout'], viewport)
        if not self.window.isVisible():
            self.window.show()
        self.window._fit()
