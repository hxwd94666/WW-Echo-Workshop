# 适配宿主实时声骸评分任务。
"""Portable OK Script adapter for Echo scoring."""

from ok import TriggerTask, og
from ok.feature.Box import Box
from contextlib import nullcontext
import queue
import threading
import time
from echo_workshop.application.templates import DEFAULT_TEMPLATE
from echo_workshop.adapters.okww.live_presentation import live_summary
from echo_workshop.adapters.okww.capture_recovery import CaptureRecoveryMonitor
from echo_workshop.adapters.okww.capture_device import read_capture_frame
from echo_workshop.adapters.okww.manual_worker import ManualWorkerMixin
from echo_workshop.adapters.okww.stat_overlay import (
    ECHO_STAT_PAINTER_KEY,
    EchoStatBoxPainter,
    analyze_echo_stats,
)
from echo_workshop.capture.viewport import EchoViewport, detect_echo_viewport
from echo_workshop.capture.metadata import (
    MetadataPass,
    refine_echo_metadata,
    record_with_metadata,
    scan_ocr_regions,
)
from echo_workshop.application.live_selection import resolve_live_selection
from .manual_capture import run_manual_capture
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.operations import OperationCancelled

STATUS_PAINTER_KEY = 'echo-workshop-status'


def _live_digest(frame):
    try:
        return hash(frame[::32, ::32].tobytes())
    except (AttributeError, TypeError):
        return None


class EchoWorkshopOverlayTask(ManualWorkerMixin, TriggerTask):
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
        self._score_thread = None
        self._score_stop = None
        self._loop_error_at = 0.0
        self._last_live_digest = None
        self._live_layout = ''
        self._live_full_at = 0.0
        self._live_comparison = False
        self._cost_cache = {}
        self._manual_queue = queue.Queue(maxsize=20)
        self._manual_pending = []
        self._manual_total_captures = 0
        self._manual_total_added = 0
        self._manual_parsing = False
        self._manual_stop = threading.Event()
        threading.Thread(target=self._manual_worker, name='EchoManualParse', daemon=True).start()
        self.default_config['_enabled'] = True
        self._start_score_loop()

    def ocr_fun(self, lib):
        from echo_workshop.capture.scan.pipeline import OCR_LOCK

        fun = super().ocr_fun(lib)

        def infer(*args, **kwargs):
            with OCR_LOCK:
                return fun(*args, **kwargs)

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

    def _score_active(self):
        if self._destroyed:
            return False
        return bool(self._settings().get('启用声骸评分', True))

    def _cached_cost(self, rows):
        return self._cost_cache.get(tuple(rows))

    def _direct_frame(self):
        """Read the current capture directly; the recovery monitor reconnects it."""
        manager = getattr(og, 'device_manager', None)
        return read_capture_frame(manager) if manager is not None else None

    def post_init(self):
        self._ensure_overlay()
        self._ensure_recovery()
        self._start_score_loop()

    def _start_score_loop(self):
        if self._score_thread is not None:
            return
        self._score_stop = threading.Event()
        self._score_thread = threading.Thread(
            target=self._score_loop, name='EchoScoreLoop', daemon=True
        )
        self._score_thread.start()
        self.log_info('实时评分独立线程已启动')

    def _stop_score_loop(self):
        stop = self._score_stop
        if stop is not None:
            stop.set()
        thread = self._score_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._score_thread = None
        self._score_stop = None

    def _score_loop(self):
        while not self._score_stop.wait(0.15):
            try:
                if self._destroyed or not self._score_active():
                    continue
            except Exception as error:
                now = time.monotonic()
                if now - getattr(self, '_active_error_at', 0.0) >= 30:
                    self._active_error_at = now
                    self.log_warning(f'实时评分开关检查失败：{error}')
                continue
            executor = getattr(self, 'executor', None)
            if executor is None or not getattr(executor, 'paused', False):
                continue
            settings_task = self._settings_task()
            runtime = getattr(settings_task, 'runtime', None)
            if runtime is not None:
                status = runtime.capture.status
                if status.mode == 'manual' and status.state in ('armed', 'single'):
                    continue
            try:
                recovery = getattr(self, 'capture_recovery', None)
                if recovery is not None:
                    recovery.poll()
                with self._run_lock:
                    self._run_frame(direct_only=True)
            except Exception as error:
                now = time.monotonic()
                if now - self._loop_error_at >= 30:
                    self._loop_error_at = now
                    self.log_warning(f'独立实时评分失败：{error}')

    def _ensure_recovery(self):
        if (
            getattr(self, 'capture_recovery', None) is None
            and getattr(og, 'device_manager', None) is not None
        ):
            self.capture_recovery = CaptureRecoveryMonitor(
                og.device_manager,
                self.executor.exit_event,
                allow_paused=self._score_active,
            )
            self.capture_recovery.start()

    def _settings_task(self):
        for task in self.get_tasks():
            if task.__class__.__name__ == 'EchoWorkshopSettingsTask' and task.config is not None:
                return task
        return None

    def _settings(self):
        task = self._settings_task()
        if task is not None:
            return task.config
        return {
            '启用声骸评分': True,
            '自动匹配评分模板': False,
            '角色评分模板': DEFAULT_TEMPLATE,
            'Show Debug Boxes': False,
        }

    def read_metadata(
        self,
        frame,
        settings,
        snapshot,
        *,
        observation_service,
        cancel_event=None,
        viewport=None,
        boxes=None,
        scan=False,
        comparison_side='right',
        fast=False,
        layout_hint='',
        comparison_hint=False,
        cost_lookup=None,
    ):
        """Shared same-frame selected-panel parser for manual and automatic intake."""
        height, width = frame.shape[:2]
        viewport = viewport or detect_echo_viewport(frame)
        if viewport is None:
            raise ValueError('未获取到游戏画面')
        if boxes is None:
            regions = (
                scan_ocr_regions(viewport, width, height)
                if scan
                else EchoViewport.checked(viewport, width, height).ocr_regions()
            )
            boxes = [
                b
                for region in regions
                for b in self.ocr(frame=frame, box=Box(*region, name='echo-content'))
            ]
        resolver = None
        if snapshot is not None:

            def resolver(normalized, kind):
                return resolve_live_selection(
                    snapshot,
                    normalized,
                    fixed=(
                        settings.get('TemplateCharacterID', ''),
                        settings.get('TemplateModalID', ''),
                    ),
                    auto=bool(settings.get('自动匹配评分模板', False)),
                    remembered=getattr(self, 'auto_matched_ids', None),
                    page_kind=kind,
                )

        def analyze(observed):
            return analyze_echo_stats(
                observed,
                width,
                height,
                settings.get('角色评分模板', DEFAULT_TEMPLATE),
                auto_match=bool(settings.get('自动匹配评分模板', False)),
                template_resolver=resolver,
                viewport=viewport,
                comparison_side=comparison_side,
                observation_service=observation_service,
                weight_version=snapshot.version,
                cancel_event=cancel_event,
                layout_hint=layout_hint,
                comparison_hint=comparison_hint,
                cost_lookup=cost_lookup,
            )

        analysis = analyze(boxes)
        if fast:
            return MetadataPass(analysis, tuple(boxes), tuple(boxes))
        if comparison_side == 'left':
            return MetadataPass(analysis, tuple(boxes), tuple(boxes))
        return refine_echo_metadata(
            analysis,
            boxes,
            width,
            height,
            lambda region: self.ocr(frame=frame, box=Box(*region, name='echo-metadata')),
            analyze,
            complete_only=scan,
            characters=getattr(snapshot, 'characters', None),
        )

    def read_record(self, metadata, frame, viewport, profile, snapshot, sonata):
        timings = getattr(self, '_scan_timings', None)
        icon = None
        layout = getattr(metadata.analysis, 'layout', '')
        if layout in ('bag', 'display') and sonata is not None:
            from echo_workshop.capture.sonata_icons import SonataMatcher

            matcher = getattr(self, '_sonata_matcher', None)
            if matcher is None or matcher.snapshot.version != sonata.version:
                matcher = self._sonata_matcher = SonataMatcher(sonata)
            with timings.stage('sonata_match') if timings is not None else nullcontext():
                icon = (
                    matcher.match(frame, layout, viewport, scan=True)
                    if getattr(self, '_scan_icon_fast', False) and layout == 'bag'
                    else matcher.match(frame, layout, viewport)
                )
            if timings is not None:
                timings.increment('sonata_' + getattr(matcher, 'last_search', 'unknown'))
        height, width = frame.shape[:2]
        with timings.stage('record_and_game_status') if timings is not None else nullcontext():
            return record_with_metadata(
                metadata,
                profile,
                width,
                height,
                icon=icon,
                characters=getattr(snapshot, 'characters', None),
                frame=frame,
            )

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
                return run_manual_capture(self, settings_task, runtime, request)
            return self._run_frame()

        try:
            run_lock = getattr(self, '_run_lock', None)
            if run_lock is None:
                return run_once()
            with run_lock:
                return run_once()
        except OperationCancelled:
            overlay = self.get_overlay_view()
            if overlay is not None:
                self._clear(overlay, True)
            return False
        except ComputeFailure as error:
            if runtime is not None and not runtime._closed:
                runtime.capture.interrupt()
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

    def _validate_single(self, settings_task, runtime, request):
        if runtime.capture.single_request is not request:
            return False
        if runtime.capture.expire_once():
            return False
        if settings_task.config.get('InventoryProfileID', '') != request.profile_id:
            runtime.capture.cancel_once('档案已切换，本次收录已取消')
            return False
        if request.background_identity is not None:
            from echo_workshop.adapters.okww.capture_device import scan_device_identity

            valid = (
                settings_task._scan_background()
                and settings_task._scan_input_ready()
                and scan_device_identity(getattr(og, 'device_manager', None))
                == request.background_identity
            )
        else:
            valid = settings_task._game_foreground()
        if not valid or not settings_task._capture_ready():
            runtime.capture.cancel_once('请保持游戏在前台并连接捕获，再按快捷键')
            return False
        return True

    def _run_frame(self, direct_only=False):
        if self._destroyed:
            return False
        started = time.monotonic()
        overlay = self._ensure_overlay()
        settings = self._settings()
        settings_task = self._settings_task()
        runtime = getattr(settings_task, 'runtime', None)
        if getattr(settings_task, 'startup_error', '') or runtime is None or runtime._closed:
            if overlay is not None:
                self._clear(overlay, True)
            return False
        self._ensure_recovery()
        if runtime.capture.status.mode == 'manual' and runtime.capture.status.state in (
            'armed',
            'single',
        ):
            if overlay is not None:
                self._clear(overlay, True)
            return False
        collecting = (
            runtime is not None
            and not runtime._closed
            and runtime.capture.status.state in ('active', 'single')
            and runtime.capture.status.mode != 'automatic'
        )
        request = runtime.capture.single_request if collecting else None
        if not settings.get('启用声骸评分', True) and not collecting:
            if overlay is not None:
                self._clear(overlay, True)
            return False

        hwnd_window = getattr(getattr(og, 'device_manager', None), 'hwnd_window', None)
        background_request = request is not None and request.background_identity is not None
        if (
            hwnd_window is not None
            and hwnd_window.exists
            and not hwnd_window.visible
            and not background_request
        ):
            if collecting:
                runtime.capture.pause()
            if overlay is not None:
                self._clear(overlay, True)
            return False
        try:
            captured_at = time.monotonic()
            frame = self._direct_frame()
            token = captured_at if frame is not None else None
            if frame is None:
                if direct_only:
                    now = time.monotonic()
                    if now - getattr(self, '_loop_noframe_at', 0.0) >= 10:
                        self._loop_noframe_at = now
                        window = getattr(getattr(og, 'device_manager', None), 'hwnd_window', None)
                        self.log_warning(
                            '独立实时评分取不到游戏帧：capture 未连接或窗口不可见'
                            f'（exists={getattr(window, "exists", None)}, '
                            f'visible={getattr(window, "visible", None)}, '
                            f'pos_valid={getattr(window, "pos_valid", None)}）'
                        )
                    return False
                frame = self.next_frame()
                token = getattr(self.executor, '_last_frame_time', None)
            if frame is None:
                if collecting:
                    runtime.capture.interrupt()
                if overlay is not None:
                    self._clear(overlay, True)
                return False
            recovery = getattr(self, 'capture_recovery', None)
            if recovery is not None:
                recovery.record_frame()
            digest = _live_digest(frame)
            page_changed = (
                digest is not None
                and self._last_live_digest is not None
                and digest != self._last_live_digest
                and (time.monotonic() - self._live_full_at >= 0.5)
            )
            if (
                digest is not None
                and not collecting
                and request is None
                and digest == self._last_live_digest
            ):
                return False
            self._last_live_digest = digest
            frame_at = time.monotonic()
            frame_height, frame_width = frame.shape[:2]
            viewport = detect_echo_viewport(frame)
            if viewport is None:
                if collecting:
                    runtime.capture.interrupt()
                if overlay is not None:
                    self._clear(overlay, True)
                return False
            view = EchoViewport.checked(viewport, frame_width, frame_height)
            force_full = (
                page_changed
                or not self._live_layout
                or (time.monotonic() - self._live_full_at >= 2.0)
            )
            comparison = self._live_comparison
            regions = (
                view.ocr_regions()
                if (force_full or comparison)
                else view.live_regions(self._live_layout)
            )
            if force_full:
                self._live_full_at = time.monotonic()
            boxes = [
                b
                for region in regions
                for b in self.ocr(frame=frame, box=Box(*region, name='echo-content'))
            ]
            ocr_at = time.monotonic()
        except Exception:
            if collecting:
                runtime.capture.interrupt()
            raise
        snapshot = None
        if runtime is not None and not runtime._closed:
            snapshot = (
                request.weights
                if request is not None and request.weights is not None
                else runtime.weights.active
            )
        try:
            metadata = self.read_metadata(
                frame,
                settings,
                snapshot,
                viewport=viewport,
                boxes=boxes,
                observation_service=runtime.observations,
                cancel_event=runtime.stop_event,
                fast=not collecting,
                layout_hint='' if force_full else self._live_layout,
                comparison_hint=comparison,
                cost_lookup=self._cached_cost,
            )
            analysis = metadata.analysis
            fell_back = False
            if not analysis.main_rows and self._live_layout and not force_full:
                regions = view.ocr_regions()
                boxes = [
                    b
                    for region in regions
                    for b in self.ocr(frame=frame, box=Box(*region, name='echo-content'))
                ]
                metadata = self.read_metadata(
                    frame,
                    settings,
                    snapshot,
                    viewport=viewport,
                    boxes=boxes,
                    observation_service=runtime.observations,
                    cancel_event=runtime.stop_event,
                    fast=not collecting,
                    cost_lookup=self._cached_cost,
                )
                analysis = metadata.analysis
                fell_back = True
            if force_full or fell_back:
                if analysis.layout:
                    self._live_layout = analysis.layout
                self._live_comparison = bool(analysis.comparison)
            signature = tuple((row.stat_name, row.value) for row in analysis.main_rows)
            if analysis.observed_cost is not None:
                self._cost_cache[signature] = analysis.observed_cost
                if len(self._cost_cache) > 64:
                    self._cost_cache.pop(next(iter(self._cost_cache)))
            analysis_at = time.monotonic()
        except Exception:
            if collecting:
                runtime.capture.interrupt()
            raise
        if getattr(analysis, 'selected_ids', None):
            self.auto_matched_ids = analysis.selected_ids
        record = None
        if collecting:
            try:
                profile = runtime.capture.status.profile_id
                layout = getattr(analysis, 'layout', '')
                sonata = (
                    request.sonata
                    if request is not None and request.sonata is not None
                    else runtime.sonata.active
                )
                record = self.read_record(metadata, frame, viewport, profile, snapshot, sonata)
                if request is not None:
                    self.log_info(
                        f'手动收录识别：页面={layout or "未识别"}，'
                        f'主词条={len(analysis.main_rows)}，副词条={len(analysis.sub_rows)}，'
                        f'解析耗时={max(0, time.monotonic() - captured_at):.2f}s'
                    )
                if collecting:
                    if request is None or self._validate_single(settings_task, runtime, request):
                        from echo_workshop.capture.evidence import (
                            describe_evidence,
                            encode_evidence,
                        )

                        record = describe_evidence(record, analysis, frame_width, frame_height)
                        runtime.capture.observe(
                            record,
                            token,
                            lambda: encode_evidence(frame, analysis),
                            context=(
                                layout,
                                analysis.page_kind,
                                frame_width,
                                frame_height,
                                getattr(analysis, 'viewport', ()),
                            ),
                            captured_at=captured_at,
                            request=request,
                            commit_guard=(
                                lambda: self._validate_single(settings_task, runtime, request)
                            )
                            if request is not None
                            else None,
                        )
            except (OSError, ValueError) as error:
                if collecting:
                    runtime.capture.interrupt()
                    self.log_warning(f'声骸收录失败，当前库存保持：{error}')
        if overlay is None:
            return False
        if background_request and not settings_task._game_foreground():
            self._clear(overlay, True)
            return False  # Intake is permitted, desktop scoring paint is not.
        if not settings.get('启用声骸评分', True):
            self._clear(overlay, True)
            return False
        reference = None
        if self._live_comparison:
            reference_settings = dict(settings)
            reference_settings['自动匹配评分模板'] = False
            if analysis.selected_ids:
                reference_settings['TemplateCharacterID'], reference_settings['TemplateModalID'] = (
                    analysis.selected_ids
                )
            reference = self.read_metadata(
                frame,
                reference_settings,
                snapshot,
                viewport=viewport,
                boxes=boxes,
                comparison_side='left',
                observation_service=runtime.observations,
                cancel_event=runtime.stop_event,
                comparison_hint=True,
            ).analysis
        summary, summary_colours = live_summary(analysis, reference)
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
            layout='comparison' if self._live_comparison else analysis.layout,
            viewport=viewport,
            summary_colours=summary_colours,
        )
        if analysis.rectangles or summary:
            overlay.draw(ECHO_STAT_PAINTER_KEY, self.painter.paint)
            overlay.clear_draw(STATUS_PAINTER_KEY)
        else:
            self._clear(overlay, True)
        now = time.monotonic()
        if now - getattr(self, '_timing_at', 0.0) >= 2.0:
            self._timing_at = now
            self.log_info(
                '实时评分耗时：'
                f'取帧={int((frame_at - started) * 1000)}ms '
                f'OCR={int((ocr_at - frame_at) * 1000)}ms '
                f'分析={int((analysis_at - ocr_at) * 1000)}ms '
                f'绘制={int((now - analysis_at) * 1000)}ms'
            )
        return False

    def _clear(self, overlay, include_status=False):
        self.painter.update([])
        overlay.clear_draw(ECHO_STAT_PAINTER_KEY)
        if include_status:
            overlay.clear_draw(STATUS_PAINTER_KEY)

    def on_destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        self._manual_stop.set()
        self._stop_score_loop()
        settings = self._settings_task()
        runtime = getattr(settings, 'runtime', None)
        if runtime is not None and not runtime._closed:
            runtime.capture.stop()
        if recovery := getattr(self, 'capture_recovery', None):
            recovery.stop()
        overlay = self.get_overlay_view()
        if overlay is not None:
            self._clear(overlay, True)
