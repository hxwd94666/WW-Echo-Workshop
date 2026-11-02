# 实时评分透明悬浮窗：默认可拖拽，锁定后鼠标穿透，详情展开设置。
import html
import json
import time

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
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
    QVBoxLayout,
    QWidget,
)

from ww_echo_workshop.application.policy import costs_from_config
from ww_echo_workshop.core.policy_presets import SYSTEMS
from ww_echo_workshop.ui.widgets.roles import RolePicker

DEFAULT_FONT_SIZE = 15
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


class HudWindow(QWidget):
    def __init__(self, task, runtime, parent=None):
        super().__init__(parent)
        self.task = task
        self.runtime = runtime
        self._locked = False
        self._details = False
        self._drag = None
        self._rows = ()
        self._line_labels = []
        self._wheel_guard = NoWheelGuard(self)
        self._last_layout = None
        self._last_interaction = 0.0
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
        self.lock_button = QPushButton('锁定', self.normal_buttons)
        self.lock_button.clicked.connect(self.lock)
        self.details_button = QPushButton('详情', self.normal_buttons)
        self.details_button.clicked.connect(self.toggle_details)
        normal_row.addWidget(self.lock_button)
        normal_row.addWidget(self.details_button)
        normal_row.addStretch()
        panel_layout.addWidget(self.normal_buttons)
        self.edit_panel = QWidget(self.panel)
        grid = QGridLayout(self.edit_panel)
        grid.setContentsMargins(0, 5, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        self.live_check = QCheckBox('实时评分', self.edit_panel)
        self.live_check.setChecked(bool(task.config.get('启用声骸评分', True)))
        self.live_check.toggled.connect(lambda checked: self._write('启用声骸评分', bool(checked)))
        self.auto_check = QCheckBox('跟随角色', self.edit_panel)
        self.auto_check.setChecked(bool(task.config.get('自动匹配评分模板', False)))
        self.auto_check.toggled.connect(
            lambda checked: self._write('自动匹配评分模板', bool(checked))
        )
        self.policy_check = QCheckBox('策略建议', self.edit_panel)
        self.policy_check.setChecked(bool(task.config.get('启用强化策略', False)))
        self.policy_check.toggled.connect(
            lambda checked: self._write('启用强化策略', bool(checked))
        )
        grid.addWidget(self.live_check, 0, 0)
        grid.addWidget(self.auto_check, 0, 1)
        grid.addWidget(self.policy_check, 0, 2)
        grid.addWidget(QLabel('字号', self.edit_panel), 1, 0)
        self.font_size = QDoubleSpinBox(self.edit_panel)
        self.font_size.setRange(12.0, 32.0)
        self.font_size.setDecimals(0)
        self.font_size.setSingleStep(1.0)
        self.font_size.setValue(float(task.config.get('策略悬浮窗字号') or DEFAULT_FONT_SIZE))
        self.font_size.valueChanged.connect(self._font_changed)
        grid.addWidget(self.font_size, 1, 1)
        grid.addWidget(QLabel('评分体系', self.edit_panel), 1, 2)
        self.scoring = QComboBox(self.edit_panel)
        self.scoring.addItem('XWUID（工坊默认）', userData='workshop_xwuid')
        for key, spec in SYSTEMS.items():
            if key != 'qq_bot':
                self.scoring.addItem(spec['label'], userData=key)
        index = self.scoring.findData(task.config.get('策略评分体系', 'workshop_xwuid'))
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
        index = self.mode.findData(task.config.get('强化策略模式', 'auto'))
        self.mode.setCurrentIndex(index if index >= 0 else 0)
        self.mode.currentIndexChanged.connect(
            lambda _: self._write('强化策略模式', self.mode.currentData())
        )
        grid.addWidget(self.mode, 2, 1)
        grid.addWidget(QLabel('目标分', self.edit_panel), 2, 2)
        self.target = QDoubleSpinBox(self.edit_panel)
        self.target.setRange(0.0, 655.35)
        self.target.setDecimals(1)
        self.target.setValue(float(task.config.get('强化策略目标分') or 0.0))
        self.target.valueChanged.connect(
            lambda value: self._write('强化策略目标分', round(float(value), 1))
        )
        grid.addWidget(self.target, 2, 3)
        self.role_picker = None
        weights = getattr(getattr(self.runtime, 'weights', None), 'active', None)
        if weights is not None:
            configured = (
                task.config.get('TemplateCharacterID'),
                task.config.get('TemplateModalID'),
            )
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
            zip(('胚子', '调谐器', '密音筒', '频整器'), costs_from_config(self.task.config))
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

    def _write(self, key, value):
        self.task.config[key] = value

    def _font_size(self):
        try:
            return max(
                12, min(32, int(self.task.config.get('策略悬浮窗字号') or DEFAULT_FONT_SIZE))
            )
        except (TypeError, ValueError):
            return DEFAULT_FONT_SIZE

    def _font_changed(self, value):
        self._write('策略悬浮窗字号', int(value))
        self._apply_font()

    def _apply_font(self):
        for label in self._line_labels:
            font = label.font()
            font.setPixelSize(self._font_size())
            label.setFont(font)

    def _scoring_changed(self, *_):
        system = self.scoring.currentData() or 'workshop_xwuid'
        self._write('策略评分体系', system)
        self.target.setRange(
            0.0,
            50.0 if system == 'workshop_xwuid' else (65535.0 if system == 'fixed' else 655.35),
        )

    def _resource_changed(self, *_):
        values = [round(float(box.value()), 3) for box in self.resource_boxes]
        self._write('强化策略资源权重', json.dumps(values, separators=(',', ':')))

    def _apply_role(self):
        if self.role_picker is None:
            return
        key = self.role_picker.key()
        if key is None:
            return
        try:
            template = self.runtime.weights.active.template(*key)
        except (AttributeError, KeyError, ValueError):
            return
        self.task.config['TemplateCharacterID'] = key[0]
        self.task.config['TemplateModalID'] = key[1]
        self.task.config['角色评分模板'] = template['name']

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
        raw = self.task.config.get('策略悬浮窗位置', '')
        try:
            data = json.loads(raw) if raw else {}
            x = int(data.get('x', 80))
            y = int(data.get('y', 120))
        except (TypeError, ValueError, json.JSONDecodeError):
            x, y = 80, 120
        self.move(QPoint(x, y))

    def _save_position(self):
        self.task.config['策略悬浮窗位置'] = json.dumps(
            {'x': self.x(), 'y': self.y()}, ensure_ascii=False, separators=(',', ':')
        )

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
        if not self.task.config.get('策略悬浮窗自动定位', False):
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
        if isinstance(colour, (tuple, list)) and colour and isinstance(colour[0], (tuple, list)):
            parts = [cls._rgb(item) for item in colour]
            parts = [item for item in parts if item is not None]
            if len(parts) >= 2 and ' / ' in str(text):
                left, right = str(text).split(' / ', 1)
                return (
                    f'<span style="color: rgb{parts[0]};">{html.escape(left)}</span>'
                    f' / <span style="color: rgb{parts[1]};">{html.escape(right)}</span>',
                    'background: transparent;',
                    True,
                )
            colour = parts[0] if parts else None
        rgb = cls._rgb(colour)
        if rgb is None:
            return str(text), 'color: #f4f4f4;', False
        return str(text), 'color: rgb(%d, %d, %d);' % rgb, False

    def set_rows(self, rows):
        rows = tuple(rows or ())
        if rows == self._rows:
            return
        count_changed = len(rows) != len(self._line_labels)
        self._rows = rows
        if count_changed:
            for label in self._line_labels:
                label.deleteLater()
            self._line_labels = []
        for index, (text, colour) in enumerate(rows):
            if count_changed:
                label = QLabel(self.lines_host)
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.NoTextInteraction)
                label.installEventFilter(self)
                self.lines_layout.addWidget(label)
                self._line_labels.append(label)
            label = self._line_labels[index]
            display, style, rich = self._format_row(text, colour)
            if label.text() != display:
                label.setTextFormat(Qt.RichText if rich else Qt.PlainText)
                label.setText(display)
            elif bool(label.textFormat() == Qt.RichText) != rich:
                label.setTextFormat(Qt.RichText if rich else Qt.PlainText)
            label.setStyleSheet(style)
            font = label.font()
            font.setPixelSize(self._font_size())
            label.setFont(font)
        if count_changed:
            self._needs_fit = True
        if self.isVisible() and self._drag is None:
            self._fit()
            self._needs_fit = False

    def showEvent(self, event):
        super().showEvent(event)
        if self._needs_fit:
            self._fit()
            self._needs_fit = False

    def normal(self):
        self._locked = False
        self._details = False
        self._last_interaction = time.monotonic()
        self.edit_panel.hide()
        self.drag_label.show()
        self.normal_buttons.show()
        self.details_button.setText('详情')
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
        self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._apply_style()
        self._fit()
        self.show()

    def edit(self):
        self._locked = False
        self._details = True
        self._last_interaction = time.monotonic()
        self.edit_panel.show()
        self.drag_label.show()
        self.normal_buttons.show()
        self.details_button.setText('收起')
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
    def __init__(self, task, runtime, parent=None):
        super().__init__(parent)
        self.task = task
        self.runtime = runtime
        self.window = HudWindow(task, runtime)
        self._hidden_since = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.refresh()

    def _mode(self):
        return self.task.config.get('策略浮层模式', 'qt')

    def reset_auto_position(self):
        self.window._last_layout = None

    def refresh(self):
        state = getattr(self.runtime, 'hud_state', None)
        if state is None:
            self.window.hide()
            return
        snapshot = state.snapshot()
        if self._mode() == 'qt' and snapshot['visible']:
            self.window.set_rows(snapshot['rows'])
            self.window.show()
        else:
            self.window.hide()

    def close(self):
        self.timer.stop()
        self.window.close()

    def toggle_edit(self):
        if self._mode() != 'qt':
            return
        if self.window._locked:
            self.window.normal()
        else:
            self.window.lock()

    def _tick(self):
        if getattr(self.runtime, '_closed', False):
            self.timer.stop()
            self.window.close()
            return
        state = getattr(self.runtime, 'hud_state', None)
        if state is None or self._mode() != 'qt' or not self.task.config.get('启用声骸评分', True):
            self.window.hide()
            return
        snapshot = state.snapshot()
        if not snapshot['visible']:
            if self.window._details or self.window._drag is not None:
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
