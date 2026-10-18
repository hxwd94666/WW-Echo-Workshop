# 汇总扫描过程的保存和失败计数。
"""Click-through foreground scan HUD. Qt stays on its owning GUI thread."""

import time
from PySide6.QtCore import Qt, QTimer, QRect
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QWindow
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout


def banner_bounds(game_rect):
    """Logical-pixel header strip, entirely above the first bag row (.12H)."""
    x, y, w, h = game_rect
    width = min(580, round(w * 0.62))
    height = min(56, round(h * 0.09))
    return QRect(round(x + (w - width) / 2), round(y + h * 0.01), width, height)


def logical_game_rect(physical, monitor_origin, screen_rect, ratio):
    x, y, w, h = physical
    mx, my = monitor_origin
    sx, sy = screen_rect
    if ratio <= 0:
        raise ValueError('Invalid display scale')
    return (sx + (x - mx) / ratio, sy + (y - my) / ratio, w / ratio, h / ratio)


class ScanProgressOverlay(QWidget):
    def __init__(self, task):
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput,
        )
        self.task = task
        self.setObjectName('echoScanProgress')
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.progress = QLabel(self)
        self.notice = QLabel(self)
        for label in (self.progress, self.notice):
            label.setTextFormat(Qt.PlainText)
            label.setAlignment(Qt.AlignCenter)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(0)
        layout.addWidget(self.progress)
        layout.addWidget(self.notice)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self._terminal_at = None
        self._terminal_state = None
        self._screen_signature = None
        self._screen_devices = {}

    def _screen_for_device(self, device):
        # QScreen.name is often an EDID product name (e.g. G24H2), not
        # MONITORINFOEX.Device (\\.\DISPLAY1). Map through an invisible Qt
        # native window so Qt performs the mixed-DPI global-coordinate mapping.
        screens = QGuiApplication.screens()
        signature = tuple((s.name(), s.geometry().getRect(), s.devicePixelRatio()) for s in screens)
        if signature != self._screen_signature:
            import win32api
            import win32con

            mapping = {}
            for screen in screens:
                probe = QWindow()
                probe.setFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
                probe.setScreen(screen)
                probe.setGeometry(QRect(screen.geometry().center(), screen.geometry().size() / 100))
                try:
                    monitor = win32api.MonitorFromWindow(
                        int(probe.winId()), win32con.MONITOR_DEFAULTTONEAREST
                    )
                    mapping[win32api.GetMonitorInfo(monitor)['Device']] = screen
                finally:
                    probe.destroy()  # Never show/activate the probe window.
            self._screen_signature, self._screen_devices = signature, mapping
        return self._screen_devices.get(device)

    def start(self):
        self._terminal_at = self._terminal_state = None
        self.timer.start()
        self.refresh()

    def _game_rect(self):
        from ok import og

        manager = getattr(og, 'device_manager', None)
        capture = getattr(manager, 'capture_method', None)
        window = getattr(manager, 'hwnd_window', None)
        if capture is None or window is None:
            return None
        try:
            viewport = getattr(self.task, '_scan_hud_viewport', None)
            if viewport is None:
                w, h = getattr(capture, '_size', (0, 0))
                if not w or not h:
                    w, h = getattr(window, 'width', 0), getattr(window, 'height', 0)
                viewport = (0, 0, w, h)
            x, y, w, h = viewport
            if min(w, h) <= 0:
                return None
            ax, ay = capture.get_abs_cords(x, y)
            bx, by = capture.get_abs_cords(x + w, y + h)
            import win32api
            import win32con

            info = win32api.GetMonitorInfo(
                win32api.MonitorFromPoint(
                    (round((ax + bx) / 2), round((ay + by) / 2)), win32con.MONITOR_DEFAULTTONEAREST
                )
            )
            screen = self._screen_for_device(info['Device'])
            if screen is None:
                return None  # Guessing between mixed-DPI displays misplaces a HUD.
            geometry = screen.geometry()
            return logical_game_rect(
                (ax, ay, bx - ax, by - ay),
                info['Monitor'][:2],
                (geometry.x(), geometry.y()),
                screen.devicePixelRatio(),
            )
        except Exception:
            return None

    def refresh(self):
        runtime = self.task.runtime
        if self.task._destroyed or runtime is None or runtime._closed:
            self.close()
            return
        scan = runtime.scan.status
        state = scan.state
        if state not in ('preparing', 'running', 'paused', 'draining', 'finished', 'interrupted'):
            self.hide()
            self.timer.stop()
            return
        top_warning = state == 'interrupted' and '请将声骸背包拉至顶部' in scan.message
        try:
            foreground = self.task._game_foreground()
        except Exception:
            foreground = False
        game = self._game_rect() if foreground else None
        if game is None:
            if top_warning:
                self._terminal_at = None  # Start warning lifetime only when visible over the game.
            self.hide()
            return
        geometry = banner_bounds(game)
        if geometry.width() < 120 or geometry.height() < 20:
            self.hide()
            return
        if state in ('finished', 'interrupted'):
            if self._terminal_state != state or self._terminal_at is None:
                self._terminal_state, self._terminal_at = state, time.monotonic()
            if time.monotonic() - self._terminal_at >= (10 if top_warning else 5):
                self.hide()
                self.timer.stop()
                return
        label = {
            'preparing': '准备扫描',
            'running': '前台扫描',
            'paused': '扫描已暂停',
            'draining': '正在解析',
            'finished': '扫描完成',
            'interrupted': '扫描已停止',
        }[state]
        self.progress.setText(
            f'{label} · 已处理 {scan.visited} · 已入库 {scan.added} · 待解析 {scan.backlog}'
            + (f' · 失败 {scan.failed}' if scan.failed else '')
        )
        notice = (
            '扫描期间请勿移动、点击或滚动鼠标；切出游戏会暂停'
            if state == 'running'
            else '请保持声骸背包在前台；扫描开始后请勿操作鼠标'
            if state == 'preparing'
            else '可操作鼠标；在助手点继续后切回游戏'
            if state == 'paused'
            else '正在处理已截图的声骸，可以操作鼠标'
            if state == 'draining'
            else scan.message
        )
        self.notice.setText(notice)
        if top_warning:
            self.progress.setText(scan.message)
            self.notice.setText('未开始扫描；回到顶部后重新点击“开始扫描”')
        size = max(8, min(13, (geometry.height() - 6) // 3))
        self.progress.setStyleSheet(
            f'color: #f4f7fb; font-size: {size}px; background: transparent;'
        )
        self.notice.setStyleSheet(f'color: #ffda83; font-size: {size}px; background: transparent;')
        self.setGeometry(geometry)
        if not self.isVisible():
            self.show()  # Never activateWindow/raise_, never steal game focus.
        if top_warning:
            self._keep_on_top()

    def _keep_on_top(self):
        """Put the warning above other topmost overlays without activating it."""
        if QGuiApplication.platformName() == 'offscreen':
            return
        try:
            import win32con
            import win32gui

            win32gui.SetWindowPos(
                int(self.winId()),
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
        except Exception:
            pass  # Qt's topmost, nonactivating flags still apply.

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#587187'), 1))
        painter.setBrush(QColor(15, 24, 34, 235))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


class ManualProgressOverlay(ScanProgressOverlay):
    """In-game top banner for shortcut intake and its parse queue."""

    def _overlay_task(self):
        try:
            for task in self.task.get_tasks():
                if task.__class__.__name__ == 'EchoWorkshopOverlayTask':
                    return task
        except Exception:
            pass
        return None

    def refresh(self):
        runtime = self.task.runtime
        if self.task._destroyed or runtime is None or runtime._closed:
            self.close()
            return
        capture = runtime.capture.status
        overlay = self._overlay_task()
        queue = getattr(overlay, '_manual_queue', None)
        backlog = queue.qsize() if queue is not None else 0
        pending = len(getattr(overlay, '_manual_pending', ()))
        if capture.mode != 'manual':
            self.hide()
            self.timer.stop()
            return
        if capture.state == 'stopped' and not backlog and not pending:
            self.hide()
            self.timer.stop()
            return
        try:
            foreground = self.task._game_foreground()
        except Exception:
            foreground = False
        game = self._game_rect() if foreground else None
        if game is None:
            self.hide()
            return
        geometry = banner_bounds(game)
        if geometry.width() < 120 or geometry.height() < 20:
            self.hide()
            return
        self.setGeometry(geometry)
        label = '等待收录快捷键' if capture.state == 'armed' else '手动收录'
        in_flight = backlog + (1 if getattr(overlay, '_manual_parsing', False) else 0)
        self.progress.setText(
            f'{label} · 截图 {getattr(overlay, "_manual_total_captures", 0)}'
            f' · 新增 {getattr(overlay, "_manual_total_added", 0)}'
            f' · 解析队列 {in_flight}'
        )
        self.notice.setText(
            '回到游戏按收录快捷键；已冻结的画面会在后台解析'
            if capture.state == 'armed'
            else '可继续按快捷键，解析在后台进行'
        )
        size = max(8, min(13, (geometry.height() - 6) // 3))
        self.progress.setStyleSheet(
            f'color: #f4f7fb; font-size: {size}px; background: transparent;'
        )
        self.notice.setStyleSheet(f'color: #ffda83; font-size: {size}px; background: transparent;')
        self.setGeometry(geometry)
        if not self.isVisible():
            self.show()
