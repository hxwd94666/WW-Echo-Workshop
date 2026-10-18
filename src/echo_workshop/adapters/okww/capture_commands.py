# 将助手收录与快捷键命令接入宿主设备。
import time
from ok import og, TriggerTask


class CaptureCommands:
    def _game_foreground(self):
        window = getattr(getattr(og, 'device_manager', None), 'hwnd_window', None)
        return bool(
            window is not None
            and window.exists
            and window.visible
            and getattr(window, 'pos_valid', True)
            and callable(getattr(window, 'is_foreground', None))
            and window.is_foreground()
        )

    def _capture_ready(self):
        capture = getattr(getattr(og, 'device_manager', None), 'capture_method', None)
        return bool(not self.executor.paused and capture is not None and capture.connected())

    def _scan_background(self):
        # Retained for manual one-shot capture only. Automatic scans require
        # _game_foreground even if this window-message backend is available.
        from echo_workshop.adapters.okww.capture_device import background_scan_available

        return background_scan_available(getattr(og, 'device_manager', None))

    def _scan_input_ready(self):
        if not self._scan_background():
            return self._game_foreground()
        window = getattr(getattr(og, 'device_manager', None), 'hwnd_window', None)
        return bool(window is not None and window.exists and getattr(window, 'pos_valid', False))

    def _configure_shortcuts(self, raw):
        from echo_workshop.adapters.okww.shortcuts import ShortcutService
        from echo_workshop.application.hotkeys import parse_bindings

        if not parse_bindings(raw) and self.runtime.shortcuts is None:
            return
        # Start from the canonical GUI entry/arming path, not a disposable legacy card.
        self._ensure_signals(None)
        if self.runtime.shortcuts is None:

            def dispatch(generation, action):
                if not self._destroyed and self._window_signals is not None:
                    self._window_signals.shortcut.emit(generation, action)

            self.runtime.shortcuts = ShortcutService(
                self._game_foreground, dispatch, stop_event=self.runtime.stop_event
            )
        self.runtime.shortcuts.configure(raw)

    def apply_shortcuts(self, raw):
        from echo_workshop.application.hotkeys import parse_bindings

        parse_bindings(raw)
        self.config['CaptureHotkeysJSON'] = raw
        self._configure_shortcuts(raw)

    def start_capture(self, profile_id):
        if self.runtime.backups.restoring:
            raise ValueError('库存恢复作业进行中；核对完成后再明确开启收录')
        if profile_id not in self.runtime.repository.profiles():
            raise ValueError('请先选择有效本地档案')
        self.runtime.capture.start(profile_id)
        for task in self.get_tasks():
            if task.__class__.__name__ == 'EchoWorkshopOverlayTask' and not task.enabled:
                task.enable()

    def arm_capture(self, profile_id):
        from echo_workshop.application.hotkeys import parse_bindings

        if 'capture' not in parse_bindings(self.config.get('CaptureHotkeysJSON', '')):
            raise ValueError('请先在设置中配置“收录当前声骸”快捷键')
        if self.runtime.backups.restoring:
            raise ValueError('库存恢复作业进行中，请稍后开始收录')
        if not self._capture_ready():
            raise ValueError('先在 OKWW 开启并连接游戏捕获')
        self._configure_shortcuts(self.config.get('CaptureHotkeysJSON', ''))
        self.runtime.capture.arm_manual(profile_id)
        self.log_info('手动收录已就绪：快捷键监听及事件分发已连接，等待按键')

    def capture_once(self, profile_id, *, from_ui=False):
        if from_ui:
            raise ValueError('点击开始仅等待快捷键，请回到游戏按收录快捷键')
        if self.runtime.backups.restoring:
            raise ValueError('库存恢复作业进行中，请稍后收录')
        if self.runtime.capture.status.state != 'armed':
            raise ValueError('请先点击“开始收录”；已有请求时请等待完成')
        if not self._capture_ready() or not self._game_foreground():
            raise ValueError('请连接游戏捕获，并在游戏前台按收录快捷键')
        from .capture_device import capture_source_identity

        accepted = self.runtime.capture.request_once(
            profile_id,
            after_token=getattr(self.executor, '_last_frame_time', None),
            weights=self.runtime.weights.active,
            sonata=self.runtime.sonata.active,
            source_identity=capture_source_identity(getattr(og, 'device_manager', None)),
        )
        if accepted:
            wake = getattr(self.executor, '_wake_executor', None)
            if callable(wake):
                wake()
            self.log_info('手动收录请求已接收：等待新画面，实时评分开关不影响收录')
            for task in self.get_tasks():
                if task.__class__.__name__ == 'EchoWorkshopOverlayTask' and not task.enabled:
                    task.enable()
        return accepted

    def start_scan(self, profile_id):
        if self.runtime.backups.restoring:
            raise ValueError('库存恢复进行中，请稍后扫描')
        if not self._capture_ready():
            raise ValueError('先在 OKWW 开启并连接游戏捕获')
        if profile_id not in self.runtime.repository.profiles():
            raise ValueError('请先选择有效档案')
        if self.runtime.capture.status.state != 'stopped':
            raise ValueError('请先结束当前收录')
        tasks = self.get_tasks()
        task = next((t for t in tasks if t.__class__.__name__ == 'EchoWorkshopScanTask'), None)
        if task is None or not any(
            t.__class__.__name__ == 'EchoWorkshopOverlayTask' for t in tasks
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
        enqueue = getattr(self.executor, 'enqueue_onetime_task', None)
        if not callable(enqueue):
            raise ValueError('当前 OKWW 缺少一次性任务队列')
        from echo_workshop.capture.scan import SCAN_LEVELS

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
                from echo_workshop.adapters.okww.scan_progress import ScanProgressOverlay

                self.scan_progress_overlay = ScanProgressOverlay(self)
            self.scan_progress_overlay.start()
            self.runtime.capture.start_scan(profile_id, generation)
            task._queued_identity = task._capture_identity()
            # BaseTask.enable() initializes the interaction backend. Queue only
            # here so that initialization and all input stay on the host thread.
            task._enabled = True
            if not enqueue(task):
                raise ValueError('扫描任务排队失败，请重试')
        except Exception:
            task._enabled = False
            self.runtime.stop_intake()
            raise
        self._show_scan_start_notice()
        return True

    def _show_scan_start_notice(self):
        # Non-modal and owned by the already visible assistant: no extra
        # top-level popup, confirmation click, game-focus steal or wait loop.
        from qfluentwidgets import InfoBar, InfoBarPosition

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
        self.scan_start_notice = InfoBar.info(
            title='自动扫描已启动',
            content='已启动自动扫描，请将游戏切至前台，并打开背包-声骸界面。',
            duration=7000,
            isClosable=True,
            position=InfoBarPosition.NONE,
            parent=parent.window(),
        )
        # One owned banner, not the shared InfoBar stack manager: disposal of
        # the plugin/page must not leave a stale bar in a host-global list.
        self.scan_start_notice.move(
            max(0, (parent.window().width() - self.scan_start_notice.width()) // 2), 16
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
            if task.__class__.__name__ == 'EchoWorkshopScanTask':
                remove = getattr(self.executor, 'remove_onetime_task', None)
                if callable(remove):
                    remove(task)
                task._enabled = False

    def handle_shortcut(self, generation, action):
        service = self.runtime.shortcuts
        if (
            self._destroyed
            or self.runtime._closed
            or service is None
            or generation != service.generation
        ):
            return
        try:
            if not self._game_foreground():
                return
            capture = self.runtime.capture
            status = capture.status
            profile = self.config.get('InventoryProfileID', '')
            if self.runtime.backups.restoring and action != 'stop':
                raise ValueError('库存恢复作业进行中，快捷操作暂缓')
            if (
                status.mode == 'automatic'
                and status.state != 'stopped'
                and action not in ('stop', 'toggle')
            ):
                raise ValueError('自动扫描中，请先暂停或停止扫描')
            if action == 'capture':
                if not self.capture_once(profile):
                    raise ValueError('已有收录正在进行，请等待或先停止')
            elif action == 'toggle':
                if status.mode == 'automatic' and status.state != 'stopped':
                    self.pause_scan()
                elif status.state == 'active':
                    capture.pause()
                else:
                    if not self._capture_ready():
                        raise ValueError('先在 OKWW 开启并连接游戏捕获，且宿主未暂停')
                    if status.state == 'paused':
                        if status.profile_id != profile:
                            raise ValueError('档案已变，请先停止再明确开始新档案')
                        capture.resume()
                    else:
                        self.start_capture(profile)
            elif action == 'stop':
                if status.mode == 'automatic':
                    self.stop_scan()
                else:
                    capture.stop()
            elif action == 'mark':
                if status.state != 'active':
                    raise ValueError('请先开始/继续收录再标记当前项')
                capture.mark_current()
            else:
                raise ValueError('未知快捷操作')
            self.shortcut_message = capture.status.message
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            self.shortcut_message = '快捷操作未完成：' + str(error)
        self.shortcut_message_at = time.monotonic()
        self.log_info(f'收录快捷操作 {action}：{self.shortcut_message}')
