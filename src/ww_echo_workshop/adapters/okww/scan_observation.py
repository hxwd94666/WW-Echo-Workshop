# 获取宿主扫描帧并解析冻结观察。
from dataclasses import replace
import time
from ww_echo_workshop.capture.viewport import EchoViewport, detect_echo_viewport
from ww_echo_workshop.capture.scan.grid import bag_scrollbar, bag_sort_direction
from ww_echo_workshop.core.cancellation import OperationCancelled
from ww_echo_workshop.capture.scan.access import ScanAccess, scan_access
from .capture_device import (
    device_manager,
    foreground_scan_supported,
    read_task_sample,
    scan_device_identity,
)
from .panel_ocr import frame_reader, make_panel_reader


def _same_page_without_selection(expected, observed):
    from ww_echo_workshop.capture.scan.signatures import _same_grid

    return (
        expected.single_page == observed.single_page
        and expected.bottom_confirmed == observed.bottom_confirmed
        and _same_grid(replace(expected, selected=observed.selected), observed)
    )


class _ObservationStop:
    def __init__(self, runtime_event, scan_event=None, *, obsolete=None):
        self.runtime_event, self.scan_event = runtime_event, scan_event
        self.obsolete = obsolete

    def is_set(self):
        return (
            self.runtime_event.is_set()
            or (self.scan_event is not None and self.scan_event.is_set())
            or (self.obsolete is not None and self.obsolete())
        )


class ScanObservation:
    def _guard(self, operation='observe'):
        runtime, scan = self._active_runtime, getattr(self, '_scan', None)
        if runtime is None or scan is None:
            return False
        generation = self._generation
        if scan.status_for(generation) is None:
            return False

        def cancelled():
            return (
                self._destroyed
                or runtime._closed
                or runtime.stop_event.is_set()
                or self.executor.exit_event.is_set()
            )

        def context_matches():
            capture = runtime.capture.scan_status(generation)
            return (
                capture is not None
                and capture.state != 'stopped'
                and self._settings.config.get('InventoryProfileID', '') == scan.profile_id
                and self._capture_identity() == self._identity
                and foreground_scan_supported(device_manager())
            )

        if cancelled():
            scan.stop('扫描已取消，已收录记录保留', generation=generation)
            return False
        matches = context_matches()
        ready = self._settings._capture_ready() if matches else False
        foreground = self._settings._game_foreground() if matches else False
        # Probes may return after the user stopped or replaced this scan.
        matches = matches and context_matches()
        status = scan.status_for(generation)
        if status is None:
            return False
        if cancelled():
            scan.stop('扫描已取消，已收录记录保留', generation=generation)
            return False
        state = status.state
        decision = scan_access(
            state,
            operation=operation,
            current=True,
            cancelled=False,
            context_matches=matches,
            host_paused=self.executor.paused,
            ready=ready,
            foreground=foreground,
        )
        if decision is ScanAccess.CHANGED:
            scan.stop('档案、捕获或输入目标已变化；扫描已停止', generation=generation)
        elif decision is ScanAccess.DISCONNECTED:
            scan.stop('捕获连接已断开；扫描已停止', generation=generation)
        elif decision in (ScanAccess.HOST_PAUSED, ScanAccess.OUT_OF_FOCUS):
            if state == 'draining':
                scan.stop('游戏已切出或 OKWW 已暂停；保留已收录记录', generation=generation)
            if state == 'running':
                scan.pause(
                    'OKWW 已暂停，扫描已暂停'
                    if decision is ScanAccess.HOST_PAUSED
                    else '游戏不在前台，扫描已暂停；在助手点继续后切回游戏',
                    generation=generation,
                )
                runtime.capture.pause(generation=generation)
        return decision is ScanAccess.ALLOW

    def _observe(self, *, operation='observe'):
        if not self._guard(operation):
            return None
        with self._measure('capture_frame'):
            sample = read_task_sample(
                self,
                self._active_runtime.capture.clock,
                device_manager,
                identify=scan_device_identity,
            )
        if not self._guard(operation) or sample is None or sample.token == self._last_token:
            return None
        frame, token, acquired_at = sample.pixels, sample.token, sample.acquired_at
        self._last_token = token
        if getattr(self, '_page_ready', False):
            from types import SimpleNamespace
            from ww_echo_workshop.capture.scan.levels import (
                selected_slot,
                page_content,
                page_moved,
            )

            viewport = self._fixed_grid.viewport
            if frame.shape[:2] != self._fixed_frame_shape:
                self._scan.stop('捕获画面大小已变化；请重新开始扫描', generation=self._generation)
                return None
            measured = detect_echo_viewport(frame)
            if measured is None:
                return None
            if measured != viewport:
                self._scan.stop('捕获画面边界已变化；请重新开始扫描', generation=self._generation)
                return None
            if bag_sort_direction(frame, viewport) != 'descending':
                return None
            if operation == 'resume':
                self._short_grid_candidate = None
                if self._pending_scroll is not None:
                    # The fixed batch resumes with its existing wheel count;
                    # only the finished batch can publish the next page.
                    return frame, token, acquired_at, viewport, None, self._fixed_grid
                with self._measure('resume_grid_detection'):
                    grid = self._detector.detect(frame, viewport, context=self._fixed_grid.context)
                if grid is None or not self._guard(operation):
                    return None
                # Selecting another slot is harmless: the task will reclick
                # the intended target. Compare measured geometry and content.
                if not _same_page_without_selection(self._fixed_grid, grid):
                    self._scan.stop(
                        '暂停期间列表位置或内容已变化；请回到顶部重新扫描',
                        generation=self._generation,
                    )
                    return None
                return frame, token, acquired_at, viewport, None, grid
            if self._pending_scroll is not None:
                with self._measure('scroll_grid_detection'):
                    grid = self._detector.detect(frame, viewport, context=self._fixed_grid.context)
                if not self._guard() or grid is None:
                    return None
                grid = replace(grid, capture_token=token)
                return frame, token, acquired_at, viewport, None, grid
            if len(self._fixed_grid.tiles) < 3:
                if not self._check_short_page(frame, viewport, acquired_at):
                    return None
            else:
                thumb, expected = bag_scrollbar(frame, viewport), self._fixed_grid.scrollbar
                # A temporarily hidden thumb is not proof of the old position.
                if thumb is None and expected is not None:
                    return None
                moved = thumb is not None and (expected is None or abs(thumb - expected) >= 0.003)
                if moved or page_moved(
                    self._page_signature, page_content(frame, self._fixed_grid.tiles)
                ):
                    self._scan.stop(
                        '列表内容或位置已变化；请回到顶部重新扫描', generation=self._generation
                    )
                    return None
            grid = replace(
                self._fixed_grid,
                capture_token=token,
                selected=selected_slot(frame, self._scan._target, viewport),
            )
            metadata = SimpleNamespace(
                level=None,
                original_boxes=(),
                analysis=SimpleNamespace(layout='bag', layout_provisional=False),
            )
            return frame, token, acquired_at, viewport, metadata, grid
        with self._measure('viewport'):
            viewport = detect_echo_viewport(frame)
        if viewport is None:
            return None
        self._settings._scan_hud_viewport = viewport
        # Only preflight needs this metadata read; active scanning uses frozen detail jobs.
        scan, generation = self._scan, self._generation
        with self._measure('preflight_metadata'):
            metadata = self._scan_parser.read_metadata(
                frame,
                self._frozen_settings,
                scan.weights,
                viewport=viewport,
                scan=True,
                observation_service=self._active_runtime.observations,
                cancel_event=_ObservationStop(
                    self._active_runtime.stop_event,
                    obsolete=lambda: scan.status_for(generation) is None,
                ),
            )
        if not self._guard():
            return None
        if getattr(metadata.analysis, 'layout', '') != 'bag' or getattr(
            metadata.analysis, 'layout_provisional', False
        ):
            self._scan.stop('请保持声骸背包页面；扫描已停止', generation=self._generation)
            return None
        area = EchoViewport.checked(viewport, frame.shape[1], frame.shape[0])
        labels = tuple(
            sorted(
                str(b.name).strip()
                for b in metadata.original_boxes
                if area.y + area.height * 0.87 <= b.y < area.y + area.height * 0.96
                and area.x + area.reference_width * 0.13
                <= b.x
                < area.x + area.reference_width * 0.33
            )
        )
        context = ('bag', labels, bag_sort_direction(frame, viewport))
        with self._measure('grid_detection'):
            grid = self._detector.detect(frame, viewport, context=context)
            if getattr(self, '_timings', None) is not None:
                self._timings.increment('grid_full_measurements')
        if grid is not None:
            grid = replace(grid, capture_token=token)
        return (frame, token, acquired_at, viewport, metadata, grid)

    def _check_short_page(self, frame, viewport, at):
        # The regular three-thumbnail threshold cannot detect a replaced
        # short page or an insertion outside its previously sampled slots.
        reference = self._fixed_grid
        with self._measure('short_grid_detection'):
            grid = self._detector.detect(frame, viewport, context=reference.context)
        if grid is None or not self._guard():
            self._short_grid_candidate = None
            return False
        previous = getattr(self, '_short_grid_candidate', None)
        if _same_page_without_selection(reference, grid):
            self._short_grid_candidate = None
            return True
        self._short_grid_candidate = (reference, grid, at)
        if (
            previous is not None
            and previous[0] is reference
            and 0 < at - previous[2] <= 5
            and _same_page_without_selection(previous[1], grid)
        ):
            self._scan.stop(
                '短列表内容或位置已变化；请回到顶部重新扫描', generation=self._generation
            )
        return False

    def _make_scan_parser(self):
        return make_panel_reader(
            self.executor,
            self._frozen_settings,
            threshold=self.ocr_default_threshold,
            remembered=getattr(self._parser, 'auto_matched_ids', None),
            timings=self._timings,
            scan_icons=True,
        )

    def _snapshot_parser(self, *, crop_mode='packed', cancel_event=None):
        from ww_echo_workshop.capture.scan.snapshot import snapshot_parser

        # Bind the whole frozen job context now, without capturing runtime/UI.
        return snapshot_parser(
            self._scan_parser,
            self._frozen_settings,
            self._scan.profile_id,
            self._scan.weights,
            self._scan.sonata,
            self._active_runtime.observations,
            _ObservationStop(self._active_runtime.stop_event, cancel_event),
            crop_mode=crop_mode,
            timings=getattr(self, '_timings', None),
        )

    def _consume_results(self):
        if self._pipeline is None:
            return
        while self._guard('commit'):
            result = self._deferred_detail or self._pipeline.poll()
            self._deferred_detail = None
            if result is None:
                return
            snapshot, value, error = result
            proof = snapshot.proof
            with self._scan._lock:
                if (
                    self._scan.generation != self._generation
                    or proof.position not in self._scan._submitted
                ):
                    continue
            if isinstance(error, OperationCancelled):
                self._diagnostic_summary = 'scan_cancelled'
                self._scan.stop('扫描已取消，已收录记录保留', generation=self._generation)
                return
            outcome = 'failed'
            if error is None:
                with self._measure('inventory_commit'):
                    committed = self._active_runtime.capture.commit_scan_snapshot(
                        value[0],
                        value[1],
                        proof,
                        generation=self._generation,
                        commit_guard=lambda: self._guard('commit'),
                        cancel_event=self._pipeline.cancelled,
                    )
                if committed is not None:
                    outcome = committed
                elif self._scan.status.state == 'paused':
                    # A guard may pause before committing. Preserve
                    # the original immutable result instead of losing a slot.
                    self._deferred_detail = result
                    return
            elif not self._parse_failure_notice:
                self._parse_failure_notice = True
                self.log_warning('声骸解析失败；已收录记录保留，请导出诊断报告查看错误类型与阶段')
            self._scan.resolve_detail(
                outcome,
                generation=self._generation,
                position=proof.position,
                reason='parse' if error is not None else 'evidence',
            )

    def _make_grade_reader(self):
        from .private_ocr import PluginOcrService

        service = PluginOcrService()
        read_region = frame_reader(
            self.executor,
            threshold=self.ocr_default_threshold,
            enabled=self._frozen_settings.get('使用独立OCR引擎', True),
            service=service,
        )

        def read(image):
            return read_region(image, (0, 0, image.shape[1], image.shape[0]), 'echo-level')

        read.close = service.close
        return read

    def _submit_page_levels(self, observed):
        from ww_echo_workshop.capture.scan.levels import crop_levels

        self._slot_indices = {
            (tile.row, tile.column): index for index, tile in enumerate(self._fixed_grid.tiles)
        }
        self._page_levels = None
        self._grade_tokens = None
        self._grade_fallback = None
        self._grade_fallback_detail = None
        self._grade_deadline = time.monotonic() + 30
        job = crop_levels(
            observed[0], self._fixed_grid.tiles, self._generation, self._page, observed[1]
        )
        if not self._grade_pipeline.submit(job):
            raise RuntimeError('Grade worker queue is occupied')

    def _consume_levels(self):
        while (result := self._grade_pipeline.poll()) is not None:
            job, values, error = result
            if job.generation != self._generation or job.page != self._page:
                continue
            if error is not None:
                self.log_warning('本批等级块识别失败；请导出诊断报告并检查宿主日志')
                raise RuntimeError('等级线程识别失败') from error
            if job.selected is None:
                self._page_levels = list(values)
                self._grade_tokens = [job.token] * len(values)
            else:
                self._page_levels[job.selected] = values[0]
                self._grade_tokens[job.selected] = job.token
                self._grade_fallback = 'done'

    def _read_selected_level(self, observed, gate, index, position):
        from ww_echo_workshop.capture.scan.pipeline import (
            _detail_observation,
            _same_detail_observation,
        )

        frame, token, at, viewport, _, _ = observed
        level = self._page_levels[index]
        if self._grade_fallback == 'done':
            if _same_detail_observation(
                self._grade_fallback_detail, _detail_observation(frame, viewport)
            ):
                return True, level
            # The result belongs to an older selected detail. Wait for a
            # stable panel again instead of using a late low grade as cutoff.
            self._page_levels[index] = None
            self._grade_fallback = None
            gate.clear_samples()
        elif self._grade_fallback is not None:
            if time.monotonic() >= self._grade_deadline:
                raise TimeoutError('等级线程未返回单件补读结果')
        elif level is not None and level >= self._scan.minimum_level:
            return True, level
        elif (
            gate.stable_pair(
                frame,
                viewport,
                token,
                at,
                position,
                self._generation,
                scope=('grade', self._page),
            )
            is not None
        ):
            if not self._fallback_level(frame, viewport, token, index, detail=gate.detail):
                gate.clear_samples()
        return False, None

    def _fallback_level(self, frame, viewport, token, index, *, detail):
        from ww_echo_workshop.capture.scan.levels import GradeJob, freeze_selected_grade

        crop = freeze_selected_grade(frame, viewport, detail)
        if crop is None or not self._grade_pipeline.submit(
            GradeJob(self._generation, self._page, token, (crop,), index)
        ):
            return False
        self._grade_fallback = index
        self._grade_fallback_detail = detail
        self._grade_deadline = time.monotonic() + 30
        return True
