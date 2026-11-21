# 适配宿主实时声骸评分任务。
"""Portable OK Script adapter for Echo scoring."""

from ok import TriggerTask, og
import queue
import threading
import time
from types import SimpleNamespace
from ww_echo_workshop.adapters.okww.live_policy import LivePolicyHint
from ww_echo_workshop.adapters.okww.live_presentation import live_summary
from ww_echo_workshop.adapters.okww.capture_recovery import CaptureRecoveryMonitor
from ww_echo_workshop.adapters.okww.capture_device import (
    capture_source_identity,
    device_manager,
    read_capture_frame,
    read_task_sample,
)
from ww_echo_workshop.capture.live_observation import LiveObservationState
from ww_echo_workshop.adapters.okww.manual_worker import ManualWorkerMixin
from ww_echo_workshop.adapters.okww.score_painter import EchoStatBoxPainter
from ww_echo_workshop.capture.viewport import EchoViewport, detect_echo_viewport
from .panel_ocr import make_panel_reader
from .manual_capture import run_manual_capture
from ww_echo_workshop.application.compute_protocol import ComputeFailure
from ww_echo_workshop.application.hud_state import LIVE_HUD_TTL_SECONDS
from ww_echo_workshop.application.operations import OperationCancelled, checkpoint
from ww_echo_workshop.adapters.okww.live_guards import combat_active as _combat_active

ECHO_STAT_PAINTER_KEY = 'echo-stat-boxes'
STATUS_PAINTER_KEY = 'ww-echo-workshop-status'


class WWEchoWorkshopOverlayTask(ManualWorkerMixin, TriggerTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = '声骸评分后台识别'
        self.description = '识别单个声骸并按 XW-UID 模版评分'
        self.trigger_interval = 0.15
        self.visible = False
        self.painter = EchoStatBoxPainter()
        self._echo_owned_cleanup = True
        self._destroyed = False
        self._run_lock = threading.RLock()
        self._live = LiveObservationState()
        self._combat_hold_until = 0.0
        self._live_policy = LivePolicyHint()
        self._live_policy_left = LivePolicyHint()
        self._manual_queue = queue.Queue(maxsize=1)
        self._manual_total_captures = 0
        self._manual_total_added = 0
        self._manual_parsing = False
        self._manual_stop = threading.Event()
        self._manual_lock = threading.Lock()
        self._manual_thread = None
        self.default_config['_enabled'] = True

    def ocr_fun(self, lib):
        from ww_echo_workshop.adapters.okww.private_ocr import PLUGIN_OCR

        task = self._settings_task()
        if task is not None and not task.config.get('使用独立OCR引擎', True):
            return lambda *args, **kwargs: ([], [])

        def infer(box, image, match, scale_factor, threshold, requested_lib):
            return PLUGIN_OCR.infer(self, box, image, match, scale_factor, threshold, requested_lib)

        return infer

    def on_create(self):
        self._destroyed = False
        self._enabled = True
        if not self.config.get('_enabled', False):
            self.config['_enabled'] = True

    def _ensure_overlay(self):
        try:
            from ok.ui.overlay import win32_gdi

            win32_gdi.HWND_TOPMOST = -2  # HWND_NOTOPMOST
        except ImportError:
            pass
        app = getattr(og, 'app', None)
        if app is None:
            return None
        if not app.ok_config.get('use_overlay', False):
            app.set_overlay_setting('boxes', True)
        overlay = app.get_overlay_view()
        window = getattr(getattr(og, 'device_manager', None), 'hwnd_window', None)
        overlay = self._repair_overlay(app, overlay, window)
        if overlay is not None and getattr(overlay, '_boxes_enabled', False):
            overlay.set_boxes_enabled(False)
            if window is not None:
                try:
                    overlay.sync_source_window(window)
                except Exception:
                    pass
        return overlay

    def _repair_overlay(self, app, overlay, window):
        """Recreate a native overlay whose game owner was destroyed or replaced."""
        if overlay is None or getattr(overlay, '_native_available', False) is not True:
            return overlay
        hwnd = getattr(overlay, '_hwnd', 0)
        owner = getattr(overlay, '_owner_hwnd', 0)
        target = getattr(window, 'hwnd', 0) if getattr(window, 'exists', False) else 0
        dead = not hwnd
        if hwnd:
            try:
                import win32gui

                dead = not win32gui.IsWindow(hwnd)
            except Exception:
                dead = False
        actual_owner = 0
        if hwnd and not dead:
            try:
                import win32gui

                actual_owner = win32gui.GetWindow(hwnd, 4)  # GW_OWNER
            except Exception:
                actual_owner = 0
        wrong_owner = bool(target and (owner != target or actual_owner != target))
        if not (dead or wrong_owner) or not target:
            return None if dead else overlay
        self.log_info(f'重建声骸涂层：native={hwnd}, old_owner={owner}, new_owner={target}')
        try:
            app._close_overlay(wait=False)
        except Exception:
            return overlay
        return app.get_overlay_view()

    def post_init(self):
        self._ensure_overlay()
        self._ensure_recovery()

    def _ensure_recovery(self):
        if (
            getattr(self, 'capture_recovery', None) is None
            and getattr(og, 'device_manager', None) is not None
        ):
            self.capture_recovery = CaptureRecoveryMonitor(
                og.device_manager,
                self.executor.exit_event,
                owner_task=self,
                allowed=self._may_recover_capture,
            )
            self.capture_recovery.start()

    def _may_recover_capture(self):
        if self._destroyed:
            return False
        settings = self._settings_task()
        runtime = getattr(settings, 'runtime', None)
        return bool(
            runtime is not None
            and not runtime._closed
            and not runtime.stop_event.is_set()
            and settings.config.get('启用声骸评分', True)
            and not getattr(getattr(runtime, 'backups', None), 'restoring', False)
            and runtime.capture.status.state == 'stopped'
        )

    def _settings_task(self):
        for task in self.get_tasks():
            if task.__class__.__name__ == 'WWEchoWorkshopSettingsTask' and task.config is not None:
                return task
        return None

    def run(self):
        settings_task = self._settings_task()
        runtime = getattr(settings_task, 'runtime', None)
        request = (
            runtime.capture.single_request if runtime is not None and not runtime._closed else None
        )
        failed_owner = getattr(self, '_compute_failed_owner', None)
        if failed_owner is not None and runtime is not None and runtime.compute is failed_owner:
            if request is not None:
                runtime.capture.cancel_once(
                    '计算服务待重新打开插件后重试',
                    diagnostic_state='failed',
                    summary_code='capture_failed',
                )
            return False

        def run_once():
            if request is not None:
                self._hide_live()
                return run_manual_capture(self, settings_task, runtime, request)
            return self._run_frame()

        try:
            run_lock = getattr(self, '_run_lock', None)
            if run_lock is None:
                return run_once()
            with run_lock:
                return run_once()
        except OperationCancelled:
            self._hide_live()
            return False
        except ComputeFailure as error:
            if runtime is not None and not runtime._closed:
                if error.kind in (
                    'restart_limit',
                    'engine_missing',
                    'engine_integrity',
                    'engine_version',
                ):
                    self._compute_failed_owner = runtime.compute
            overlay = self.get_overlay_view()
            if overlay is not None:
                self._clear(overlay, True)
            self.log_warning(f'声骸观测计算中断：{error.kind}')
            return False
        finally:
            if (
                request is not None
                and not runtime._closed
                and runtime.capture.single_request is None
                and runtime.capture.status.profile_id == request.profile_id
            ):
                settings_task.shortcut_message = runtime.capture.status.message
                settings_task.shortcut_message_at = time.monotonic()

    @staticmethod
    def _source_identity():
        return capture_source_identity(device_manager())

    def _run_frame(self):
        if self._destroyed:
            return False
        from ww_echo_workshop.adapters.okww.private_ocr import PLUGIN_OCR

        started = time.monotonic()
        PLUGIN_OCR.release_if_idle(300)
        settings_task = self._settings_task()
        runtime = getattr(settings_task, 'runtime', None)
        if runtime is None or runtime._closed or getattr(settings_task, 'startup_error', ''):
            self._hide_live()
            return False
        settings = dict(settings_task.config)
        if not settings.get('启用声骸评分', True):
            self._hide_live()
            if runtime.capture.status.state == 'stopped':
                PLUGIN_OCR.close()
            return False
        capture_status = runtime.capture.status
        if capture_status.state != 'stopped':
            # Manual/automatic sessions own intake; the live path never writes.
            self._hide_live()
            return False
        executor = self.executor
        active = getattr(executor, 'current_task', None)
        combat, self._combat_hold_until = _combat_active(executor, active, self._combat_hold_until)
        if combat:
            self._hide_live()
            return False
        self._ensure_recovery()
        window = getattr(device_manager(), 'hwnd_window', None)
        if window is not None and window.exists and not window.visible:
            self._hide_live()
            return False
        source = self._source_identity()
        frame = read_capture_frame(device_manager())
        if frame is None:
            sample = read_task_sample(self, time.monotonic, device_manager)
            frame = sample.pixels if sample is not None else None
        viewport = detect_echo_viewport(frame)
        if viewport is None or source != self._source_identity():
            self._hide_live()
            return False
        recovery = getattr(self, 'capture_recovery', None)
        if recovery is not None:
            recovery.record_frame()
        frame_at = time.monotonic()
        frame_height, frame_width = frame.shape[:2]
        view = EchoViewport.checked(viewport, frame_width, frame_height)
        snapshot = runtime.weights.active

        def current_frame():
            return (
                not self._destroyed
                and not runtime._closed
                and not runtime.stop_event.is_set()
                and settings_task.runtime is runtime
                and runtime.capture.status is capture_status
                and capture_status.state == 'stopped'
                and self._source_identity() == source
                and runtime.weights.active is snapshot
                and dict(settings_task.config) == settings
            )

        frame_cancel = SimpleNamespace(is_set=lambda: not current_frame())
        context = (
            source,
            snapshot.version,
            tuple(
                settings.get(key)
                for key in (
                    'InventoryProfileID',
                    '角色评分模板',
                    'TemplateCharacterID',
                    'TemplateModalID',
                    '自动匹配评分模板',
                    '使用独立OCR引擎',
                )
            ),
        )

        parser = make_panel_reader(
            executor,
            settings,
            threshold=self.ocr_default_threshold,
            remembered=getattr(self, 'auto_matched_ids', None),
        )

        def read_regions(regions):
            for region in regions:
                checkpoint(frame_cancel)
                yield from parser.read_region(frame, region, 'echo-content')

        def analyze(boxes, layout, comparison, cost_lookup):
            return parser.read_metadata(
                frame,
                settings,
                snapshot,
                viewport=viewport,
                boxes=boxes,
                observation_service=runtime.observations,
                cancel_event=frame_cancel,
                fast=True,
                layout_hint=layout,
                comparison_hint=comparison,
                cost_lookup=cost_lookup,
                retry_live_cost=True,
            ).analysis

        live = self._live.observe(
            frame, view, context=context, now=frame_at, read_regions=read_regions, analyze=analyze
        )
        if not current_frame():
            self._hide_live()
            return False
        if live is None:
            return False
        analysis, boxes = live.analysis, live.boxes
        analysis_at = time.monotonic()
        reference = None
        left_policy_text = ''
        if analysis.comparison:
            reference_settings = dict(settings)
            reference_settings['自动匹配评分模板'] = False
            if analysis.selected_ids:
                reference_settings['TemplateCharacterID'], reference_settings['TemplateModalID'] = (
                    analysis.selected_ids
                )
            reference = parser.read_metadata(
                frame,
                reference_settings,
                snapshot,
                viewport=viewport,
                boxes=boxes,
                comparison_side='left',
                observation_service=runtime.observations,
                cancel_event=frame_cancel,
                comparison_hint=True,
                retry_live_cost=True,
            ).analysis
            if not current_frame():
                self._hide_live()
                return False
            reference = self._live.confirm_reference(
                reference, frame, view, frame_at, template=analysis.selected_ids
            )
            if reference is not None:
                left_policy_text = self._live_policy_left.observe(
                    reference, reference_settings, snapshot, runtime
                )
        observed_policy = self._live_policy.observe(analysis, settings, snapshot, runtime)
        policy_enabled = bool(settings.get('启用强化策略'))
        if policy_enabled and not observed_policy:
            now = time.monotonic()
            if now - getattr(self, '_policy_log_at', 0.0) >= 5.0:
                self._policy_log_at = now
                self.log_info(
                    '实时策略未生成：'
                    f'layout={getattr(analysis, "layout", "")!r}, '
                    f'score={getattr(analysis, "current_score", None)!r}, '
                    f'target={settings.get("强化策略目标分", 0)!r}, '
                    f'snapshot={snapshot is not None}, '
                    f'policy={getattr(runtime, "policy", None) is not None}'
                )
        summary, summary_colours = live_summary(
            analysis, reference, observed_policy, left_policy_text, policy_enabled=policy_enabled
        )
        if not current_frame():
            self._hide_live()
            return False
        overlay = self._ensure_overlay()
        if not current_frame():
            self._hide_live()
            return False
        if analysis.selected_ids:
            self.auto_matched_ids = analysis.selected_ids
        runtime.live_snapshots.capture(analysis)
        settings_task.live_policy_text = observed_policy
        hud_state = getattr(runtime, 'hud_state', None)
        if summary:
            if hud_state is not None:
                hud_state.update(
                    summary,
                    summary_colours,
                    layout='comparison' if analysis.comparison else getattr(analysis, 'layout', ''),
                    viewport=getattr(view, 'bounds', None),
                    frame_size=(frame_width, frame_height),
                )
        elif hud_state is not None:
            hud_state.hide()
        if overlay is None:
            return False
        if settings.get('策略浮层模式', 'gdi') == 'qt':
            self._clear(overlay, True, hide_hud=False)
            return False
        if not summary:
            self._clear(overlay, True)
            return False
        paired = reference is not None and reference.current_score is not None
        rectangles = reference.rectangles + analysis.rectangles if paired else analysis.rectangles
        tier_labels = (
            reference.tier_labels + analysis.tier_labels if paired else analysis.tier_labels
        )
        tier_colors = (
            reference.tier_colors + analysis.tier_colors if paired else analysis.tier_colors
        )
        row_scores = reference.row_scores + analysis.row_scores if paired else analysis.row_scores
        self.painter.update(
            rectangles,
            summary,
            tier_labels,
            tier_colors,
            row_scores,
            layout='comparison' if analysis.comparison else analysis.layout,
            viewport=viewport,
            summary_colours=summary_colours,
        )
        if analysis.rectangles or summary:
            overlay.draw(ECHO_STAT_PAINTER_KEY, self.painter.paint, duration=LIVE_HUD_TTL_SECONDS)
            overlay.clear_draw(STATUS_PAINTER_KEY)
        else:
            self._clear(overlay, True)
        now = time.monotonic()
        if now - getattr(self, '_timing_at', 0.0) >= 2.0:
            self._timing_at = now
            self.log_info(
                '实时评分耗时：'
                f'取帧={int((frame_at - started) * 1000)}ms '
                f'OCR及分析={int((analysis_at - frame_at) * 1000)}ms '
                f'绘制={int((now - analysis_at) * 1000)}ms'
            )
        return False

    def _hide_live(self):
        self._live.reset()
        overlay = self.get_overlay_view()
        if overlay is not None:
            self._clear(overlay, True)
        else:
            settings = self._settings_task()
            runtime = getattr(settings, 'runtime', None)
            if runtime is not None and not runtime._closed:
                runtime.hud_state.hide()

    def _clear(self, overlay, include_status=False, hide_hud=True):
        self.painter.update([])
        overlay.clear_draw(ECHO_STAT_PAINTER_KEY)
        if include_status:
            overlay.clear_draw(STATUS_PAINTER_KEY)
        if hide_hud:
            settings = self._settings_task()
            runtime = getattr(settings, 'runtime', None)
            if runtime is not None and not runtime._closed:
                runtime.hud_state.hide()

    def on_destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        from ww_echo_workshop.adapters.okww.private_ocr import PLUGIN_OCR

        self._manual_stop.set()
        self._live_policy.clear()
        self._live_policy_left.clear()
        settings = self._settings_task()
        runtime = getattr(settings, 'runtime', None)
        if runtime is not None and not runtime._closed:
            runtime.capture.stop()
        PLUGIN_OCR.close()
        self._close_manual_worker()
        if recovery := getattr(self, 'capture_recovery', None):
            recovery.stop()
        overlay = self.get_overlay_view()
        if overlay is not None:
            self._clear(overlay, True)
