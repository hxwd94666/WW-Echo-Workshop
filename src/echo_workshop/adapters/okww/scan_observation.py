# 获取宿主扫描帧并解析冻结观察。
from contextlib import nullcontext
from dataclasses import replace
import time
from ok import og, Box
from echo_workshop.capture.viewport import EchoViewport
from echo_workshop.capture.scan.grid import bag_sort_direction
from echo_workshop.core.cancellation import OperationCancelled
from .capture_device import _window_capture_available
from .snapshot_parse import parse_snapshot_record


class _ObservationStop:
    def __init__(self, runtime_event, scan_event):
        self.runtime_event, self.scan_event = runtime_event, scan_event

    def is_set(self):
        return self.runtime_event.is_set() or (
            self.scan_event is not None and self.scan_event.is_set()
        )


class ScanObservation:
    def _guard(self, *, allow_paused=False, observation_only=False):
        runtime, scan = self._active_runtime, getattr(self, '_scan', None)
        if runtime is None or scan is None:
            return False
        if (
            self._destroyed
            or runtime._closed
            or runtime.stop_event.is_set()
            or self.executor.exit_event.is_set()
            or scan.generation != self._generation
        ):
            return False
        if (
            self._settings.config.get('InventoryProfileID', '') != scan.profile_id
            or self._capture_identity() != self._identity
            or runtime.capture.status.mode != 'automatic'
            or runtime.capture.status.state == 'stopped'
        ):
            scan.stop('档案、捕获或输入目标已变化；扫描已停止')
            return False
        if self.executor.paused:
            scan.pause('OKWW 已暂停，扫描已暂停')
            runtime.capture.pause()
            return False
        if not self._settings._capture_ready():
            scan.stop('捕获连接已断开；扫描已停止')
            return False
        manager = getattr(og, 'device_manager', None)
        window = getattr(manager, 'hwnd_window', None)
        passive = (
            observation_only
            and allow_paused
            and scan.status.state == 'paused'
            and _window_capture_available(manager)
            and getattr(window, 'exists', False)
            and getattr(window, 'pos_valid', False)
        )
        if (
            not passive
            and scan.status.state != 'draining'
            and not self._settings._game_foreground()
        ):
            if scan.status.state == 'running':
                scan.pause('游戏不在前台，扫描已暂停；在助手点继续后切回游戏')
                runtime.capture.pause()
            return False
        return scan.status.state in (
            ('preparing', 'running', 'paused')
            if allow_paused
            else ('preparing', 'running', 'draining')
        )

    def _observe(self, *, allow_paused=False, observation_only=False):
        if not self._guard(allow_paused=allow_paused, observation_only=observation_only):
            return None
        acquired_at = self._active_runtime.capture.clock()
        with self._measure('capture_frame'):
            frame = self.next_frame()
        token = getattr(self.executor, '_last_frame_time', None)
        if frame is None or token is None or token == self._last_token:
            return None
        self._last_token = token
        if getattr(self, '_fixed_mode', False):
            from types import SimpleNamespace
            from echo_workshop.capture.scan.levels import selected_slot, list_bottom

            viewport = self._fixed_grid.viewport
            grid = replace(
                self._fixed_grid,
                capture_token=token,
                selected=selected_slot(frame, self._fixed_grid.tiles, self._scan._target, viewport),
                bottom_confirmed=list_bottom(frame, viewport),
            )
            metadata = SimpleNamespace(
                level=None,
                original_boxes=(),
                analysis=SimpleNamespace(layout='bag', layout_provisional=False),
            )
            return frame, token, acquired_at, viewport, metadata, grid
        from echo_workshop.capture.viewport import detect_echo_viewport

        with self._measure('viewport'):
            viewport = detect_echo_viewport(frame)
        if viewport is None:
            return None
        self._settings._scan_hud_viewport = viewport
        # Selection/grid observations do not need grade OCR. The gate asks for
        # an actual reading only after traversal confirms the selected target.
        self._probe_read_level = self._pipeline is None
        if allow_paused:
            self._sort_probe_cache = None
        with self._measure('probe' if self._pipeline is not None else 'preflight_metadata'):
            metadata = (
                self._read_probe(frame, viewport)
                if self._pipeline is not None
                else self._parser.read_metadata(
                    frame,
                    self._settings.config,
                    self._scan.weights,
                    viewport=viewport,
                    scan=True,
                    observation_service=self._active_runtime.observations,
                    cancel_event=self._active_runtime.stop_event,
                )
            )
        if not self._guard(allow_paused=allow_paused, observation_only=observation_only):
            return None
        if getattr(metadata.analysis, 'layout', '') != 'bag' or getattr(
            metadata.analysis, 'layout_provisional', False
        ):
            self._scan.stop('请保持声骸背包页面；扫描已停止')
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
            cache_allowed = (
                self._pipeline is not None
                and not allow_paused
                and self._scan._phase in ('select', 'confirm', 'read')
            )
            grid = (
                self._detector.observe_cached(frame, viewport, context=context)
                if cache_allowed
                else None
            )
            cache_hit = grid is not None
            if grid is None:
                grid = self._detector.detect(frame, viewport, context=context)
            if getattr(self, '_timings', None) is not None:
                self._timings.increment(
                    'grid_cached_observations' if cache_hit else 'grid_full_measurements'
                )
        if grid is not None:
            edge = None
            if self._scan._anchor_active:
                from echo_workshop.capture.scan.anchor import gold_boundary

                edge = gold_boundary(frame, grid, self._scroll_anchor)
            grid = replace(grid, capture_token=token, anchor_boundary=edge)
        return (frame, token, acquired_at, viewport, metadata, grid)

    def _make_scan_parser(self):
        # A separate parser facade owns its caches. Its OCR bypasses task.ocr's
        # pause/sleep/device side effects and reads only the supplied image.
        import copy
        from echo_workshop.capture.scan.pipeline import OCR_LOCK

        parser = copy.copy(self._parser)
        parser._sonata_matcher = None
        parser._scan_icon_fast = True
        timings = getattr(self, '_timings', None)
        parser._scan_timings = timings
        fun = parser.ocr_fun('default')

        def frozen_ocr(*, frame, box):
            image = frame[box.y : box.y + box.height, box.x : box.x + box.width]
            waiting = time.perf_counter()
            with OCR_LOCK:
                if timings is not None:
                    timings.record('ocr_lock_wait', max(0.0, time.perf_counter() - waiting))
                name = (
                    'level_ocr'
                    if box.name.startswith('echo-level')
                    else 'sort_ocr'
                    if box.name == 'echo-sort'
                    else 'detail_ocr'
                )
                with timings.stage(name) if timings is not None else nullcontext():
                    detected, _ = fun(
                        box, image, None, 1.0, parser.ocr_default_threshold, 'default'
                    )
            return sorted(detected, key=lambda b: (b.y, b.x))

        parser.ocr = frozen_ocr
        return parser

    def _read_probe(self, frame, viewport):
        from types import SimpleNamespace
        from echo_workshop.capture.scan.pipeline import bag_region, observed_level

        boxes, level = [], None
        if getattr(self, '_probe_read_level', True):
            grade = bag_region(frame, viewport, (0.68, 0.17, 0.755, 0.23))
            boxes = self._scan_parser.ocr(frame=frame, box=Box(*grade, name='echo-level'))
            level = observed_level(boxes)
        if getattr(self, '_probe_read_level', True) and level is None:
            # Still only the header's grade band, not full stats OCR. More
            # margin preserves the plus sign across resolutions/font metrics.
            grade = bag_region(frame, viewport, (0.665, 0.15, 0.795, 0.235))
            boxes = self._scan_parser.ocr(frame=frame, box=Box(*grade, name='echo-level-retry'))
            level = observed_level(boxes)
        sort = bag_region(frame, viewport, (0.13, 0.87, 0.335, 0.96), side='left')
        # Cache only a nonempty caption with identical bright text pixels and
        # geometry. A changed caption/viewport is re-read, never silently reused.
        # The sort direction is still observed separately from every new frame.
        import numpy as np

        x, y, w, h = sort
        crop = frame[y : y + h, x : x + w, :3]
        mask = (crop.min(axis=2) > 170) & (crop.max(axis=2) - crop.min(axis=2) < 80)
        packed = np.packbits(mask).tobytes()
        key = (tuple(viewport), sort, packed)
        cached = getattr(self, '_sort_probe_cache', None)
        if cached is not None and cached[0] == key:
            labels = cached[1]
            if getattr(self, '_timings', None) is not None:
                self._timings.increment('sort_ocr_cache_hits')
        else:
            labels = self._scan_parser.ocr(frame=frame, box=Box(*sort, name='echo-sort'))
            self._sort_probe_cache = (key, tuple(labels)) if labels and mask.sum() >= 8 else None
        return SimpleNamespace(
            level=level,
            probe_texts=tuple(str(b.name) for b in boxes),
            original_boxes=tuple(labels),
            analysis=SimpleNamespace(layout='bag', layout_provisional=False),
        )

    def _snapshot_parser(self, *, crop_mode='packed', cancel_event=None):
        # Bind the complete job environment now. A cancelled inference may
        # finish after this task/runtime is gone; it retains no repository/UI.
        from echo_workshop.capture.evidence import prepare_evidence
        from echo_workshop.capture.scan.crop import read_fields

        if crop_mode not in ('original', 'packed'):
            raise ValueError('Unknown scan snapshot crop mode')
        parser, settings = self._scan_parser, self._frozen_settings
        profile, weights, sonata = self._scan.profile_id, self._scan.weights, self._scan.sonata
        observations = self._active_runtime.observations
        stop_event = _ObservationStop(self._active_runtime.stop_event, cancel_event)
        timings = getattr(self, '_timings', None)

        def measure(name):
            return timings.stage(name) if timings is not None else nullcontext()

        def parse(snapshot):
            frame, viewport = snapshot.frame, snapshot.viewport
            boxes = []
            metadata = None
            packed_observation = None
            if crop_mode == 'packed':
                try:
                    boxes = read_fields(frame, viewport, parser.ocr, mode='packed', box_factory=Box)
                    with measure('metadata'):
                        metadata = parser.read_metadata(
                            frame,
                            settings,
                            weights,
                            viewport=viewport,
                            boxes=boxes,
                            scan=True,
                            observation_service=observations,
                            cancel_event=stop_event,
                        )
                    packed_observation = metadata
                except ValueError:
                    # Geometry/crop ambiguity gets one legacy attempt on THIS
                    # frozen image. Engine/runtime exceptions still propagate.
                    if timings is not None:
                        timings.increment('packed_crop_errors')
                if timings is not None:
                    timings.increment('packed_crop_attempts')
            if crop_mode == 'packed' and timings is not None and metadata is not None:
                if (
                    getattr(metadata, 'issues', ())
                    or getattr(metadata.analysis, 'layout', '') != 'bag'
                ):
                    timings.increment('packed_legacy_fallbacks')
            with measure('metadata'):
                metadata, record = parse_snapshot_record(
                    parser,
                    frame,
                    viewport,
                    settings,
                    weights,
                    profile,
                    sonata,
                    scene='bag',
                    boxes=boxes or None,
                    initial=metadata,
                    expected_level=snapshot.proof.level,
                    observations=observations,
                    cancel_event=stop_event,
                )
            boxes = getattr(metadata, 'original_boxes', boxes)
            if getattr(metadata.analysis, 'layout', '') != 'bag' or getattr(
                metadata.analysis, 'layout_provisional', False
            ):
                analysis = metadata.analysis
                row, column = snapshot.proof.position
                raise ValueError(
                    'Queued snapshot is not a complete bag panel; '
                    f'position=({row + 1},{column + 1}); level={snapshot.proof.level}; '
                    f'layout={getattr(analysis, "layout", "")!r}; '
                    f'provisional={getattr(analysis, "layout_provisional", False)}; '
                    f'rows=({len(getattr(analysis, "main_rows", ()))},{len(getattr(analysis, "sub_rows", ()))}); '
                    f'boxes={len(boxes)}'
                )
            if record is None or record.level != snapshot.proof.level:
                raise ValueError('Queued snapshot level/details disagree')
            if packed_observation is not metadata and getattr(packed_observation, 'issues', ()):
                # A later same-frame read must not erase an earlier conflicting
                # observation. Preserve its raw boxes and unresolved issues, but
                # drop warnings the final record has already resolved.
                from dataclasses import replace

                from echo_workshop.capture.metadata import settle_observation_issues

                packed_issues = tuple(settle_observation_issues(record, packed_observation.issues))
                record = replace(
                    record,
                    issues=tuple(dict.fromkeys(record.issues + packed_issues)),
                    raw_text=record.raw_text
                    + '\n[echo-scan-packed-observation]\n'
                    + packed_observation.raw_text(),
                )
            with measure('png_encode'):
                record, evidence = prepare_evidence(record, frame, metadata.analysis)
            return record, evidence

        return parse

    def _consume_results(self):
        if self._pipeline is None or not self._guard():
            return
        while (result := self._pipeline.poll()) is not None:
            snapshot, value, error = result
            proof = snapshot.proof
            with self._scan._lock:
                if not self._guard() or proof.position not in self._scan._submitted:
                    return
                if isinstance(error, OperationCancelled):
                    self._diagnostic_summary = 'scan_cancelled'
                    self._scan.stop('扫描已取消，已收录记录保留')
                    return
                outcome = 'failed'
                if error is None:
                    before = self._active_runtime.capture.status
                    with self._measure('inventory_commit'):
                        committed = self._active_runtime.capture.commit_scan_snapshot(
                            value[0],
                            value[1],
                            proof,
                            generation=self._generation,
                            commit_guard=self._guard,
                        )
                    if committed:
                        after = self._active_runtime.capture.status
                        outcome = 'pending' if after.pending > before.pending else 'added'
                else:
                    if not self._parse_failure_notice:
                        self._parse_failure_notice = True
                        self.log_warning(
                            '声骸解析失败；已收录记录保留，请导出诊断报告查看错误类型与阶段'
                        )
                self._scan.resolve_detail(
                    outcome,
                    generation=self._generation,
                    position=proof.position,
                    reason='parse' if error is not None else 'evidence',
                )

    def _make_grade_reader(self):
        # Independent host-configured engine, created INSIDE the grade thread.
        # No shared OCR_LOCK and no mutation of the executor's engine cache.
        import copy
        from types import SimpleNamespace

        facade = copy.copy(self._parser)
        engine = self.executor._create_ocr_lib('default')
        facade._executor = SimpleNamespace(
            config=self.executor.config,
            ocr_lib=lambda name: engine,
            ocr_po_translation=getattr(self.executor, 'ocr_po_translation', None),
            locale=getattr(self.executor, 'locale', 'zh_CN'),
            text_fix=getattr(self.executor, 'text_fix', {}),
        )
        facade.screenshot = lambda *a, **kw: None
        fun = facade.ocr_fun('default')

        def read(image):
            boxes, _ = fun(
                Box(0, 0, image.shape[1], image.shape[0]),
                image,
                None,
                1.0,
                facade.ocr_default_threshold,
                'default',
            )
            return boxes

        return read

    def _submit_page_levels(self, observed):
        from echo_workshop.capture.scan.levels import crop_levels

        self._page = self._scan._offset // 3
        self._page_levels = None
        self._grade_tokens = None
        self._grade_fallback = None
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
                self._grade_tokens = [job.token] * 18
            else:
                self._page_levels[job.selected] = values[0]
                self._grade_tokens[job.selected] = job.token
                self._grade_fallback = 'done'

    def _fallback_level(self, frame, viewport, token, index):
        import numpy as np
        from echo_workshop.capture.scan.pipeline import bag_region
        from echo_workshop.capture.scan.levels import GradeJob

        x, y, w, h = bag_region(frame, viewport, (0.665, 0.15, 0.795, 0.235))
        crop = np.array(frame[y : y + h, x : x + w, :3], copy=True)
        crop.flags.writeable = False
        if self._grade_pipeline.submit(
            GradeJob(self._generation, self._page, token, (crop,), index)
        ):
            self._grade_fallback = index
            self._grade_deadline = time.monotonic() + 30
