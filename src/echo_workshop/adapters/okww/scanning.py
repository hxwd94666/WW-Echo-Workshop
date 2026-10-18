# 在宿主任務线程协调背包扫描与取消。
from contextlib import nullcontext
from dataclasses import replace
import time
import threading
from ok import BaseTask, og
from echo_workshop.capture.viewport import EchoViewport
from echo_workshop.capture.scan.models import ScanTile, ScanStartError
from echo_workshop.capture.scan.grid import BagGridDetector
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled
from .capture_device import scan_device_identity
from .scan_observation import ScanObservation
from .scan_input import ScanInput


class EchoWorkshopScanTask(ScanObservation, ScanInput, BaseTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = '声骸自动扫描'
        self.description = '在背包逐件收录达到最低等级的声骸'
        self.visible = False
        self._echo_owned_cleanup = True
        self._destroyed = False
        self._active_runtime = None
        self._pipeline = None

    def _measure(self, name):
        timings = getattr(self, '_timings', None)
        return timings.stage(name) if timings is not None else nullcontext()

    def _capture_identity(self):
        return scan_device_identity(getattr(og, 'device_manager', None))

    def run(self):
        tasks = self.get_tasks()
        self._settings = next(
            (t for t in tasks if t.__class__.__name__ == 'EchoWorkshopSettingsTask'), None
        )
        self._parser = next(
            (t for t in tasks if t.__class__.__name__ == 'EchoWorkshopOverlayTask'), None
        )
        runtime = getattr(self._settings, 'runtime', None)
        if (
            runtime is None
            or runtime._closed
            or self._parser is None
            or runtime.scan.status.state != 'preparing'
        ):
            return
        self._active_runtime, self._scan = runtime, runtime.scan
        self._generation = self._scan.generation
        self._identity = getattr(self, '_queued_identity', self._capture_identity())
        self._detector, self._last_token = BagGridDetector(), None
        started = time.monotonic()
        self._wheel_steps = 0
        self._fixed_mode = False
        self._scroll_batches = 0
        self._pending_scroll = None
        self._grade_pipeline = None
        self._mouse_paused = False
        self._frame_size = ()
        self._pipeline = None
        from echo_workshop.capture.scan.pipeline import ScanTimings

        self._timings = ScanTimings()
        self._selection_timer = None
        self._sort_probe_cache = None
        self._probe_read_level = True
        self._parse_failure_notice = False
        self._diagnostic_failure = False
        self._diagnostic_summary = None
        manager = getattr(og, 'device_manager', None)
        self._scan_job = begin_job(
            'automatic-scan',
            metadata={
                'minimum_level': self._scan.minimum_level,
                'capture_method': type(getattr(manager, 'capture_method', None)).__name__,
                'click_method': 'foreground-native',
                'wheel_method': 'foreground-native',
            },
        )
        self._scan_job.stage('preparing')
        try:
            deadline = time.monotonic() + 20
            while (
                not self._guard()
                and self._scan.status.state == 'preparing'
                and time.monotonic() < deadline
            ):
                self.sleep(0.1)
            if self._guard():
                overlay = self._parser._ensure_overlay()
                if overlay is not None:
                    self._parser._clear(overlay, True)
                self._scan_parser = self._make_scan_parser()
            with self._measure('prepare_sort_and_warmup'):
                observed = self._prepare_sort() if self._guard() else None
            if observed is None:
                self._diagnostic_failure = self._scan.generation == self._generation
                self._diagnostic_summary = (
                    'scan_start_failed' if self._diagnostic_failure else 'scan_cancelled'
                )
                if self._scan.generation == self._generation:
                    self._scan.stop(
                        getattr(self, '_prepare_error', '')
                        or (
                            '未切回游戏；请重新开始后切回鸣潮背包'
                            if not self._settings._game_foreground()
                            else '排序或列表未确认；请打开背包并回到顶部'
                        )
                    )
                return
            self._scan.begin(
                self._scan.profile_id,
                observed[-1],
                minimum_level=self._scan.minimum_level,
                weights=self._scan.weights,
                sonata=self._scan.sonata,
                descending=True,
                queued_generation=self._generation,
            )
            self._frame_size = (observed[3][2], observed[3][3])
            self._scan_job.metadata({'resolution': self._frame_size})
            self._scan_job.stage('scanning', {'batch_size': 18})
            from echo_workshop.capture.scan.pipeline import ScanPipeline, ScanSnapshot
            from echo_workshop.capture.scan.levels import FixedFrameGate, read_levels

            area = EchoViewport.checked(observed[3], observed[0].shape[1], observed[0].shape[0])
            # Calibrate ONCE at startup. All later batches reuse these 18 points.
            first = observed[-1].tiles[0]
            x, y, w, h = first.bounds
            rows = observed[-1].rows()
            pitch = rows[1][0].bounds[1] - y if len(rows) > 1 else round(area.height * 0.196)
            xs = [t.bounds[0] for t in rows[0]]
            dx = xs[1] - xs[0] if len(xs) > 1 else round(area.height * 0.1633)
            tiles = tuple(
                ScanTile(r, c, (x + c * dx, y + r * pitch, w, h), ())
                for r in range(3)
                for c in range(6)
            )
            self._fixed_grid = replace(observed[-1], tiles=tiles)
            self._fixed_area = area
            self._fixed_mode = True
            self._scan.enable_fixed(self._fixed_grid)
            reader = [None]

            def grades(job):
                if reader[0] is None:
                    reader[0] = self._make_grade_reader()
                with self._measure('page_grade_ocr'):
                    return read_levels(job, reader[0])

            self._grade_pipeline = ScanPipeline(
                grades,
                capacity=1,
                timings=None,
                name='echo-scan-levels',
                diagnostics=self._scan_job,
                channel='grade',
            )
            self._submit_page_levels(observed)
            self._frozen_settings = dict(self._settings.config)
            detail_cancel = threading.Event()
            self._pipeline = runtime.scan_pipeline = ScanPipeline(
                self._snapshot_parser(cancel_event=detail_cancel),
                timings=self._timings,
                diagnostics=self._scan_job,
                cancel_event=detail_cancel,
            )
            gate = FixedFrameGate()
            detail_deadline = time.monotonic() + 12
            missing = 0
            while (
                self._scan.status.state in ('running', 'paused')
                and self._scan.generation == self._generation
            ):
                if not self._guard():
                    if (
                        self._destroyed
                        or runtime._closed
                        or runtime.stop_event.is_set()
                        or self.executor.exit_event.is_set()
                    ):
                        self._scan.stop('扫描已取消')
                        break
                    gate.reset()
                    self._resume_fixed_if_requested()
                    self.sleep(0.1)
                    continue
                self._consume_results()
                self._consume_levels()
                observed = self._fresh()
                if observed is None:
                    gate.reset()
                    missing += 1
                    if missing >= 3 and self._scan.status.state == 'running':
                        self._scan.pause('画面未更新，扫描已暂停；恢复游戏画面后继续')
                        runtime.capture.pause()
                    continue
                missing = 0
                # Grade OCR runs once per batch. Let it finish before choosing
                # a new slot, so even the first below-threshold item is not clicked.
                # Pending detail jobs from the previous batch keep parsing here.
                if self._page_levels is None:
                    if time.monotonic() >= self._grade_deadline:
                        raise TimeoutError('等级线程未返回本批结果')
                    self.sleep(0.02)
                    self._grade_pipeline.cancelled.wait(0.001)
                    continue
                action = self._scan.advance(
                    observed[-1],
                    profile_id=self._settings.config.get('InventoryProfileID'),
                    generation=self._generation,
                )
                if action.kind == 'interrupted':
                    self._diagnostic_failure = True
                    self.log_warning(
                        '扫描位置检查停止：'
                        + action.message
                        + '；'
                        + self._scan.last_position_check
                    )
                if action.kind == 'click':
                    if action.message:
                        self.log_warning(action.message + '；' + self._scan.last_position_check)
                    clicked_at = time.perf_counter()
                    index = action.tile.row * 6 + action.tile.column
                    if (
                        self._page_levels is not None
                        and self._page_levels[index] is not None
                        and self._page_levels[index] < self._scan.minimum_level
                    ):
                        self._scan.fixed_threshold()
                        continue
                    if self._click_checked(action.tile.center, settle=0.02):
                        self._selection_timer = (self._scan.position, clicked_at)
                        gate.reset()
                        runtime.capture.rearm_scan(
                            self._scan.position, self._generation, observed[1]
                        )
                        detail_deadline = time.monotonic() + 12
                elif action.kind == 'scroll':
                    with self._measure('scroll_and_position'):
                        self._scroll_batch(observed)
                elif action.kind == 'read':
                    frame, token, acquired_at, viewport, metadata, grid = observed
                    position = self._scan.position
                    index = position[0] % 3 * 6 + position[1]
                    if self._page_levels is None:
                        if time.monotonic() >= self._grade_deadline:
                            raise TimeoutError('等级线程未返回本批结果')
                        self.sleep(0.02)
                        self._grade_pipeline.cancelled.wait(0.001)
                        continue
                    level = self._page_levels[index]
                    if level is None:
                        if self._grade_fallback is None:
                            self._fallback_level(frame, viewport, token, index)
                        elif self._grade_fallback == 'done':
                            self.log_warning('本件等级未读清，保留失败计数并继续')
                            self._scan.complete_detail(
                                'failed',
                                generation=self._generation,
                                position=position,
                                reason='level',
                            )
                            self._grade_fallback = None
                            gate.reset()
                        elif time.monotonic() >= self._grade_deadline:
                            raise TimeoutError('等级线程未返回单件补读结果')
                        self.sleep(0.02)
                        self._grade_pipeline.cancelled.wait(0.001)
                        continue
                    if level < self._scan.minimum_level:
                        self._scan.fixed_threshold()
                        continue
                    self._grade_fallback = None
                    proof = gate.observe(
                        frame,
                        viewport,
                        token,
                        acquired_at,
                        position,
                        self._generation,
                        level,
                        self._grade_tokens[index],
                    )
                    if proof is None:
                        if time.monotonic() >= detail_deadline:
                            self._scan.pause('详情暂未刷新，扫描已暂停；切回游戏后继续')
                            runtime.capture.pause()
                            gate.reset()
                        self.sleep(0.04)
                        continue
                    if self._selection_timer is not None and self._selection_timer[0] == position:
                        self._timings.record(
                            'selection_confirm',
                            max(0.0, time.perf_counter() - self._selection_timer[1]),
                        )
                        self._selection_timer = None
                    decision = self._scan.accept_detail(
                        proof.level, 'valid', generation=self._generation, position=position
                    )
                    if decision == 'commit':
                        # Backpressure is bounded: retain this verified selection
                        # while the single worker catches up; never skip a job.
                        with self._measure('producer_backpressure'):
                            while (
                                self._pipeline.pending >= self._pipeline.capacity and self._guard()
                            ):
                                self._consume_results()
                                self.sleep(0.05)
                                self._pipeline.cancelled.wait(0.001)
                        if self._guard():
                            snapshot = ScanSnapshot.freeze(frame, viewport, proof)
                            with self._scan._lock:
                                if self._guard() and self._pipeline.submit(snapshot):
                                    self._scan.complete_detail(
                                        'queued', generation=self._generation, position=position
                                    )
                            gate.reset()
                # A successful click/queued snapshot already has its own guard
                # and settle/evidence waits. Do not add another fixed 150 ms.
                # Missing selection and detail evidence keep the existing wait.
                self.sleep(0.01 if action.kind in ('click', 'read') else 0.04)
            self._scan.drain()
            self._scan_job.stage('draining')
            with self._measure('parse_tail'):
                while self._scan.status.state == 'draining' and self._guard():
                    self._consume_results()
                    self._scan.drain()
                    if self._scan.status.state == 'draining':
                        self.sleep(0.05)
                        self._pipeline.cancelled.wait(0.005)
        except OperationCancelled:
            self._diagnostic_summary = 'scan_cancelled'
            if self._scan.generation == self._generation:
                self._scan.stop('扫描已取消，已收录记录保留')
        except ScanStartError as error:
            self._diagnostic_failure = True
            self._diagnostic_summary = 'scan_not_top'
            self._scan_job.exception('preparing', error, 'scan_not_top')
            self._scan.stop(str(error))
            self.log_warning(str(error))
        except Exception as error:
            self._diagnostic_failure = True
            self._scan_job.exception(self._scan_job.current_stage, error, 'scan_failed')
            if self._scan.generation == self._generation and self._scan.status.state in (
                'preparing',
                'running',
                'paused',
                'draining',
            ):
                self._scan.stop('扫描中断；已收录记录保留，请查看日志')
            raise
        finally:
            if self._scan.status.state == 'draining':
                self._scan.stop('解析已中断；保留已入库声骸')
            if self._grade_pipeline is not None:
                self._grade_pipeline.close()
            self._fixed_mode = False
            if self._pipeline is not None:
                self._pipeline.close()
                if runtime.scan_pipeline is self._pipeline:
                    runtime.scan_pipeline = None
            self.log_info(
                f'扫描结束：{self._scan.status.message}；已处理 {self._scan.status.visited}'
            )
            if (
                runtime.capture.status.mode == 'automatic'
                and runtime.capture._scan_generation == self._generation
            ):
                runtime.capture.stop()
            if not runtime._closed:
                self._write_report(runtime, started)
            status = self._scan.status
            self._scan_job.performance(self._timings.snapshot())
            state = (
                'failed'
                if self._diagnostic_failure
                else 'completed'
                if status.state == 'finished'
                else 'cancelled'
            )
            code = self._diagnostic_summary or (
                'scan_threshold' if state == 'completed' and status.skipped else 'scan_' + state
            )
            self._scan_job.finish(
                state,
                code,
                counters=dict(
                    visited=status.visited,
                    captured=status.captured,
                    added=status.added,
                    pending=status.pending,
                    failed=status.failed,
                    skipped=status.skipped,
                    backlog=status.backlog,
                    wheel_steps=self._wheel_steps,
                    scroll_batches=self._scroll_batches,
                ),
            )
            self._active_runtime = None

    def _write_report(self, runtime, started):
        # A single, bounded local diagnostic summary. No names, item/profile
        # identifiers, OCR, image content or device paths enter the report.
        import json
        import os
        from pathlib import Path
        import tempfile

        status = self._scan.status
        payload = dict(
            schema=1,
            operation_id=self._scan_job.operation_id,
            state=status.state,
            minimum_level=self._scan.minimum_level,
            visited=status.visited,
            added=status.added,
            pending=status.pending,
            captured=status.captured,
            backlog=status.backlog,
            skipped=status.skipped,
            failed=status.failed,
            wheel_steps=self._wheel_steps,
            failed_positions=[list(position) for position in self._scan.failed_positions],
            failure_details=[
                dict(row=f.row, column=f.column, reason=f.reason)
                for f in self._scan.failure_snapshot()['details']
            ],
            failure_details_omitted=self._scan.failure_snapshot()['omitted'],
            content_size=list(self._frame_size),
            elapsed_seconds=round(max(0, time.monotonic() - started), 3),
        )
        payload['end_reason'] = (
            'threshold'
            if status.state == 'finished' and status.skipped
            else 'list_end'
            if status.state == 'finished'
            else 'interrupted'
        )
        timings = getattr(self, '_timings', None)
        if timings is not None:
            payload['performance'] = timings.snapshot()
        temporary = None
        try:
            directory = Path(runtime.data_dir)
            fd, temporary = tempfile.mkstemp(prefix='.scan-report-', suffix='.json', dir=directory)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / 'scan-last-report.json')
        except OSError as error:
            self._scan_job.exception('report_export', error, 'failed')
            self.log_warning('扫描报告保存失败：' + type(error).__name__)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def on_destroy(self):
        self._destroyed = True
        runtime = self._active_runtime
        if runtime is not None and not runtime._closed:
            runtime.stop_intake()
