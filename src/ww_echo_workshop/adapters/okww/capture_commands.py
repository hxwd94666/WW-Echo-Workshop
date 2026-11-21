# 将助手收录与快捷键命令接入宿主设备。
from dataclasses import dataclass
import time
from ok import TriggerTask
from ww_echo_workshop.application.inventory_intake import IntakeStart
from .capture_device import (
    capture_connected,
    capture_source_identity,
    device_manager,
    foreground_scan_supported,
    frame_token,
    game_foreground,
)


@dataclass(frozen=True)
class _ShortcutContext:
    runtime: object
    owner: object
    status: object
    profile_id: str
    source: object


class CaptureCommands:
    def inventory_intake(self):
        return InventoryIntakeControls(self)

    def _game_foreground(self):
        return game_foreground(device_manager())

    def _capture_ready(self):
        return capture_connected(device_manager(), self.executor)

    def _restore_shortcuts(self):
        # Saved shortcuts are optional: a broken value or unavailable key reader
        # must not affect the workbench or rewrite the persisted preferences.
        try:
            self._configure_shortcuts(self.config.get('CaptureHotkeysJSON', ''))
        except (ValueError, OSError) as error:
            self.shortcut_message = '快捷键配置待修正：' + str(error)

    def _configure_shortcuts(self, raw):
        from ww_echo_workshop.adapters.okww.shortcuts import ShortcutService
        from ww_echo_workshop.application.hotkeys import parse_bindings

        if not parse_bindings(raw) and self.runtime.shortcuts is None:
            return
        # Start from the canonical GUI entry/arming path, not a disposable legacy card.
        self._ensure_signals(None)
        runtime = self.runtime
        if runtime.shortcuts is None:

            def dispatch(generation, action, context):
                if (
                    not self._destroyed
                    and self.runtime is runtime
                    and self._window_signals is not None
                ):
                    self._window_signals.shortcut.emit(generation, action, context)

            runtime.shortcuts = ShortcutService(
                self._game_foreground,
                dispatch,
                context=lambda: self._shortcut_context() if self.runtime is runtime else None,
                stop_event=runtime.stop_event,
            )
        runtime.shortcuts.configure(raw)

    def apply_shortcuts(self, raw):
        from ww_echo_workshop.application.hotkeys import parse_bindings

        parse_bindings(raw)
        self.config['CaptureHotkeysJSON'] = raw
        self._configure_shortcuts(raw)
        self.shortcut_message = ''

    def arm_capture(self, profile_id):
        from ww_echo_workshop.application.hotkeys import parse_bindings

        if 'capture' not in parse_bindings(self.config.get('CaptureHotkeysJSON', '')):
            raise ValueError('请先在设置中配置“收录当前声骸”快捷键')
        if self.runtime.backups.restoring:
            raise ValueError('库存恢复作业进行中，请稍后开始收录')
        if not self._capture_ready():
            raise ValueError('先在 OKWW 开启并连接游戏捕获')
        self._configure_shortcuts(self.config.get('CaptureHotkeysJSON', ''))
        self.runtime.capture.arm_manual(profile_id)
        for task in self.get_tasks():
            if task.__class__.__name__ == 'WWEchoWorkshopOverlayTask':
                task._manual_total_captures = 0
                task._manual_total_added = 0
        if self.manual_progress_overlay is None:
            from ww_echo_workshop.adapters.okww.scan_progress import ManualProgressOverlay

            self.manual_progress_overlay = ManualProgressOverlay(self)
        self.manual_progress_overlay.start()
        self.log_info('手动收录已就绪：快捷键监听及事件分发已连接，等待按键')

    def capture_once(self, profile_id, *, from_ui=False, context=None):
        if from_ui:
            raise ValueError('点击开始仅等待快捷键，请回到游戏按收录快捷键')
        if self.runtime.backups.restoring:
            raise ValueError('库存恢复作业进行中，请稍后收录')
        capture = self.runtime.capture
        if capture.status.state not in ('armed', 'single'):
            raise ValueError('请先点击“开始收录”；已有请求时请等待完成')
        if capture.status.state == 'single':
            raise ValueError('当前声骸仍在采样或解析，请完成后再按')
        if not self._capture_ready() or not self._game_foreground():
            raise ValueError('请连接游戏捕获，并在游戏前台按收录快捷键')
        if context is not None and not self._shortcut_current('capture', context):
            return False
        accepted = self.runtime.capture.request_once(
            profile_id,
            after_token=frame_token(self.executor),
            weights=self.runtime.weights.active,
            sonata=self.runtime.sonata.active,
            source_identity=(
                context.source if context is not None else capture_source_identity(device_manager())
            ),
            expected_status=context.status if context is not None else None,
        )
        if accepted:
            self.log_info('手动收录新请求已接受')
            wake = getattr(self.executor, '_wake_executor', None)
            if callable(wake):
                wake()
            self.log_info('手动收录请求已接收：等待新画面，实时评分开关不影响收录')
            for task in self.get_tasks():
                if task.__class__.__name__ == 'WWEchoWorkshopOverlayTask' and not task.enabled:
                    task.enable()
        return accepted

    def start_scan(self, profile_id):
        if self.runtime.backups.restoring:
            raise ValueError('库存恢复进行中，请稍后扫描')
        if not self._capture_ready():
            raise ValueError('先在 OKWW 开启并连接游戏捕获')
        if not foreground_scan_supported(device_manager()):
            raise ValueError('当前捕获方式未开放自动扫描；请使用前台原生 SDR 窗口捕获')
        if profile_id not in self.runtime.repository.profiles():
            raise ValueError('请先选择有效档案')
        if self.runtime.capture.status.state != 'stopped':
            raise ValueError('请先结束当前收录')
        tasks = self.get_tasks()
        task = next((t for t in tasks if t.__class__.__name__ == 'WWEchoWorkshopScanTask'), None)
        if task is None or not any(
            t.__class__.__name__ == 'WWEchoWorkshopOverlayTask' for t in tasks
        ):
            raise ValueError('扫描任务未加载，请重新导入完整插件包')
        current = getattr(self.executor, 'current_task', None)
        # Trigger checks share the serial executor: the queued scan starts only
        # after the current run() returns. Do not disable them or treat their
        # transient current_task reference as a conflicting one-time job.
        if (
            current is not None
            and getattr(current, 'running', False)
            and not isinstance(current, TriggerTask)
        ):
            raise ValueError('请先结束 OKWW 当前任务')
        queued = getattr(self.executor, 'onetime_task_queue', ())
        if queued or task.running:
            raise ValueError('有任务等待或运行中，请稍后扫描')
        from ww_echo_workshop.capture.scan import SCAN_LEVELS

        raw = self.config.get('ScanMinimumLevel', '25')
        if str(raw) not in tuple(str(level) for level in SCAN_LEVELS):
            raise ValueError('请选择有效最低等级')
        generation = self.runtime.scan.queue(
            profile_id,
            minimum_level=int(raw),
            weights=self.runtime.weights.active,
            sonata=self.runtime.sonata.active,
        )
        try:
            self._ensure_signals(None)
            if self.scan_progress_overlay is None:
                from ww_echo_workshop.adapters.okww.scan_progress import ScanProgressOverlay

                self.scan_progress_overlay = ScanProgressOverlay(self)
            self.scan_progress_overlay.start()
            self.runtime.capture.start_scan(profile_id, generation)
            task.bind_queued_scan(self.runtime, generation)
            wake = getattr(self.executor, '_wake_executor', None)
            if callable(wake):
                wake()
        except Exception:
            task.cancel_scan(self.runtime, generation)
            raise
        self._show_scan_start_notice()
        return True

    def _show_scan_start_notice(self):
        # Non-modal and owned by the already visible assistant: no extra
        # top-level popup, confirmation click, game-focus steal or wait loop.
        from ww_echo_workshop.ui.widgets.controls import show_notice

        parent = next(
            (
                w
                for w in (self.embedded_panel, self.inventory_window, self.assistant_window)
                if w is not None and w.isVisible()
            ),
            None,
        )
        if parent is None:
            return
        if self.scan_start_notice is not None:
            from shiboken6 import isValid

            if isValid(self.scan_start_notice):
                self.scan_start_notice.close()
        self.scan_start_notice = show_notice(
            parent.window(),
            '已启动自动扫描，请将游戏切至前台，并打开背包-声骸界面。',
            title='自动扫描已启动',
            duration=7000,
            closable=True,
        )

    def pause_scan(self):
        if self.runtime.scan.status.state == 'running':
            self.runtime.scan.pause()
            self.runtime.capture.pause()
        elif self.runtime.scan.status.state == 'paused':
            self.runtime.scan.resume_requested = True
            self.executor._wake_executor()

    def stop_scan(self):
        self.runtime.stop_intake()
        for task in self.get_tasks():
            if task.__class__.__name__ == 'WWEchoWorkshopScanTask':
                task.cancel_bound_scan()

    def expire_queued_scan(self):
        for task in self.get_tasks():
            if task.__class__.__name__ == 'WWEchoWorkshopScanTask':
                task.expire_queued_scan()
                return

    def _shortcut_context(self):
        runtime = self.runtime
        if self._destroyed or runtime is None or runtime._closed:
            return None
        owner, status = runtime.capture.shortcut_snapshot()
        return _ShortcutContext(
            runtime,
            owner,
            status,
            self.config.get('InventoryProfileID', ''),
            capture_source_identity(device_manager()),
        )

    def _shortcut_current(self, action, context):
        current = self._shortcut_context()
        return (
            context is not None
            and current is not None
            and current.runtime is context.runtime
            and current.owner is context.owner
            and current.profile_id == context.profile_id
            and current.source == context.source
            and (
                current.status is context.status
                if action == 'capture'
                else context.owner is not None
            )
        )

    def handle_shortcut(self, generation, action, context):
        runtime = self.runtime
        service = getattr(runtime, 'shortcuts', None)
        if (
            self._destroyed
            or service is None
            or runtime._closed
            or generation != service.generation
        ):
            return
        try:
            if not self._game_foreground() or not self._shortcut_current(action, context):
                return
            capture = self.runtime.capture
            status = capture.status
            profile = self.config.get('InventoryProfileID', '')
            if self.runtime.backups.restoring and action != 'stop':
                raise ValueError('库存恢复作业进行中，快捷操作暂缓')
            if status.mode == 'automatic' and status.state != 'stopped' and action != 'stop':
                raise ValueError('自动扫描中，请先暂停或停止扫描')
            if action == 'capture':
                if not self.capture_once(profile, context=context):
                    raise ValueError('已有收录正在进行，请等待或先停止')
            elif action == 'stop':
                if status.mode == 'automatic':
                    self.stop_scan()
                else:
                    capture.stop()
            else:
                raise ValueError('未知快捷操作')
            self.shortcut_message = capture.status.message
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            self.shortcut_message = '快捷操作未完成：' + str(error)
        self.shortcut_message_at = time.monotonic()
        self.log_info(f'收录快捷操作 {action}：{self.shortcut_message}')


class InventoryIntakeControls:
    """Expose collection commands without passing the host task into the page."""

    def __init__(self, task):
        self._task = task
        self._capture, self._scan = task.runtime.capture, task.runtime.scan

    @property
    def status(self):
        return self._capture.status

    @property
    def ready(self):
        return self._task._capture_ready()

    @property
    def idle(self):
        return self.status.state == 'stopped' and self.scan_status.state not in (
            'preparing',
            'running',
            'paused',
            'draining',
        )

    @property
    def mode(self):
        return self._task.config.get('IntakeMode', 'manual')

    @property
    def minimum_level(self):
        return str(self._task.config.get('ScanMinimumLevel', '25'))

    @property
    def manual_backlog(self):
        queue = getattr(self._task, '_manual_queue', None)
        return queue.qsize() if queue is not None else 0

    @property
    def scan_status(self):
        return self._scan.status

    @property
    def scan_profile(self):
        return self._scan.profile_id

    @property
    def failed_positions(self):
        return self._scan.failed_positions

    def failure_snapshot(self):
        return self._scan.failure_snapshot()

    def set_mode(self, mode):
        if self.status.state == 'stopped':
            self._task.config['IntakeMode'] = mode

    def set_minimum_level(self, level):
        if self.status.state == 'stopped':
            self._task.config['ScanMinimumLevel'] = level

    def start(self, profile_id):
        from ww_echo_workshop.application.hotkeys import parse_bindings

        mode = self.mode
        if mode == 'manual' and 'capture' not in parse_bindings(
            self._task.config.get('CaptureHotkeysJSON', '')
        ):
            return IntakeStart.NEED_SHORTCUT
        if not self.ready:
            return IntakeStart.DISCONNECTED
        if mode == 'automatic':
            self._task.start_scan(profile_id)
        else:
            self._task.arm_capture(profile_id)
        return IntakeStart.STARTED

    def pause_or_resume(self):
        status = self.status
        if status.mode == 'automatic':
            self._task.pause_scan()
        elif status.state == 'paused':
            if not self.ready:
                return False
            self._capture.resume()
        else:
            self._capture.pause()
        return True

    def stop(self):
        if self.status.mode == 'automatic':
            self._task.stop_scan()
        else:
            self._capture.stop()

    def reset_for_profile(self):
        self._capture.stop()
