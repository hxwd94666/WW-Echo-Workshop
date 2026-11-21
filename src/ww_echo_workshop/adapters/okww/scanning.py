# 在宿主任務线程协调背包扫描与取消。
from contextlib import nullcontext
import time
import threading
from ok import TriggerTask, og
from ok.task.exceptions import FinishedException, TaskDisabledException
from ww_echo_workshop.capture.scan.models import ScanStartError
from ww_echo_workshop.capture.scan.grid import BagGridDetector
from ww_echo_workshop.capture.viewport import EchoViewport
from ww_echo_workshop.application.diagnostics import begin_job
from ww_echo_workshop.core.cancellation import OperationCancelled
from .capture_device import device_manager, game_foreground, scan_device_identity
from .scan_observation import ScanObservation
from .scan_input import ScanInput


class WWEchoWorkshopScanTask(ScanObservation, ScanInput, TriggerTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = '声骸自动扫描'
        self.description = '在背包逐件收录达到最低等级的声骸'
        self.trigger_interval = 0.2
        self.visible = False
        self._echo_owned_cleanup = True
        self._destroyed = False
        self._owner_lock = threading.Lock()
        self._scan_owner = None
        self._active_runtime = None
        self._pipeline = None

    def on_create(self):
        # Always require a new explicit request; never restore an armed scan.
        self._enabled = False

    def _measure(self, name):
        timings = getattr(self, '_timings', None)
        return timings.stage(name) if timings is not None else nullcontext()

    def _capture_identity(self):
        return scan_device_identity(getattr(og, 'device_manager', None))

    def bind_queued_scan(self, runtime, generation):
        identity = self._capture_identity()
        with self._owner_lock:
            if self._destroyed or self._active_runtime is not None:
                raise ValueError('当前扫描任务不可排队，请重试')
            self._scan_owner = (runtime, generation, identity, time.monotonic() + 20)
            self._enabled = True

    def cancel_scan(self, runtime, generation, *, message='已停止；保留已收录的声骸'):
        with self._owner_lock:
            owner = self._scan_owner
            if owner is not None and owner[0] is runtime and owner[1] == generation:
                self._scan_owner = None
                self._enabled = False
            pipeline = (
                self._pipeline
                if self._active_runtime is runtime and self._generation == generation
                else None
            )
        if not runtime._closed:
            runtime.scan.stop(message, generation=generation)
            if pipeline is not None:
                pipeline.cancel()
            runtime.capture.stop(generation=generation)

    def cancel_bound_scan(self):
        with self._owner_lock:
            owner = self._scan_owner
        if owner is not None:
            self.cancel_scan(owner[0], owner[1])

    def expire_queued_scan(self):
        with self._owner_lock:
            owner = self._scan_owner if self._active_runtime is None else None
        if owner is None:
            return False
        runtime, generation, _, deadline = owner
        if runtime._closed or runtime.scan.status_for(generation) is None:
            self.cancel_scan(runtime, generation)
            return True
        if time.monotonic() < deadline:
            return False
        self.cancel_scan(runtime, generation, message='未切回游戏；请重新开始后切回鸣潮背包')
        return True

    def should_trigger(self):
        # One-time task dispatch calls the host's interaction.on_run(), which
        # can activate the window before this plugin's foreground guard runs.
        if self.expire_queued_scan():
            return False
        with self._owner_lock:
            armed = (
                not self._destroyed
                and self._scan_owner is not None
                and self._active_runtime is None
            )
        return armed and game_foreground(device_manager()) and super().should_trigger()

    def run(self):
        if self.expire_queued_scan():
            return
        tasks = self.get_tasks()
        self._settings = next(
            (t for t in tasks if t.__class__.__name__ == 'WWEchoWorkshopSettingsTask'), None
        )
        self._parser = next(
            (t for t in tasks if t.__class__.__name__ == 'WWEchoWorkshopOverlayTask'), None
        )
        runtime = getattr(self._settings, 'runtime', None)
        with self._owner_lock:
            owner = self._scan_owner
            if owner is None or self._destroyed or self._active_runtime is not None:
                return
            queued_runtime, generation, identity, _ = owner
            status = queued_runtime.scan.status_for(generation)
            start = (
                queued_runtime is runtime
                and not queued_runtime._closed
                and self._parser is not None
                and status is not None
                and status.state == 'preparing'
            )
            if start:
                # Frame acquisition can separate trigger selection from run().
                # Yield to the host with the same queue deadline if focus was lost.
                if self.executor.paused or not self._settings._game_foreground():
                    return
                self._active_runtime, self._scan = runtime, runtime.scan
                self._generation, self._identity = generation, identity
                self._pipeline = None
        if not start:
            self.cancel_scan(queued_runtime, generation)
            return
        self._detector, self._last_token = BagGridDetector(), None
        started = time.monotonic()
        self._wheel_steps = 0
        self._page_ready = False
        self._scroll_batches = 0
        self._pending_scroll = None
        self._grade_pipeline = None
        self._mouse_paused = False
        self._frame_size = ()
        from ww_echo_workshop.capture.scan.pipeline import ScanTimings

        self._timings = ScanTimings()
        self._selection_timer = None
        self._parse_failure_notice = False
        self._deferred_detail = None
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
            self._parser._hide_live()
            if self._guard():
                self._frozen_settings = dict(self._settings.config)
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
                        ),
                        generation=self._generation,
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
            self._scan_job.stage('scanning', {'batch_size': len(observed[-1].tiles)})
            from ww_echo_workshop.capture.scan.pipeline import ScanPipeline, ScanSnapshot
            from ww_echo_workshop.capture.scan.levels import (
                FixedFrameGate,
                page_content,
                read_levels,
            )

            self._fixed_grid = observed[-1]
            area = EchoViewport.checked(observed[3], observed[0].shape[1], observed[0].shape[0])
            self._wheel_position = self._fixed_wheel_point(self._fixed_grid, area)
            self._slot_points = self._fixed_slot_points(self._fixed_grid, area)
            self._fixed_frame_shape = observed[0].shape[:2]
            self._page_signature = page_content(observed[0], self._fixed_grid.tiles)
            self._page_ready = True
            self._page = 0
            reader = [None]

            def grades(job):
                if reader[0] is None:
                    reader[0] = self._make_grade_reader()
                with self._measure('page_grade_ocr'):
                    return read_levels(job, reader[0])

            def close_grade_reader():
                if reader[0] is not None:
                    reader[0].close()

            self._grade_pipeline = ScanPipeline(
                grades,
                capacity=1,
                timings=None,
                name='echo-scan-levels',
                diagnostics=self._scan_job,
                channel='grade',
                on_worker_exit=close_grade_reader,
            )
            self._submit_page_levels(observed)
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
                        self._scan.stop('扫描已取消', generation=self._generation)
                        break
                    # A click from before the pause is not a new selection receipt.
                    gate.reset()
                    self._resume_page_if_requested()
                    self.sleep(0.1)
                    continue
                if self._pending_scroll is not None:
                    with self._measure('scroll_and_position'):
                        self._scroll_batch()
                    continue
                observed = self._fresh()
                if observed is None:
                    gate.clear_samples()
                    missing += 1
                    if missing >= 3 and self._scan.status.state == 'running':
                        self._scan.pause(
                            '画面未更新，扫描已暂停；恢复游戏画面后继续',
                            generation=self._generation,
                        )
                        runtime.capture.pause(generation=self._generation)
                    continue
                missing = 0
                self._consume_results()
                self._consume_levels()
                if not self._guard():
                    continue
                # Grade OCR runs once per batch. A low or unknown grid result
                # must be checked against the selected panel before the cutoff.
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
                    self.log_warning('扫描位置检查停止：' + action.message)
                if action.kind == 'click':
                    if action.message:
                        self.log_warning(action.message)
                    clicked_at = time.perf_counter()
                    if self._click_scan_slot(action.tile):
                        self._selection_timer = (self._scan.position, clicked_at)
                        gate.arm(
                            self._scan.position,
                            self._generation,
                            observed[1],
                            runtime.capture.clock(),
                        )
                        detail_deadline = time.monotonic() + 12
                elif action.kind == 'scroll':
                    with self._measure('scroll_and_position'):
                        self._scroll_batch(observed)
                elif action.kind == 'read':
                    frame, token, acquired_at, viewport, metadata, grid = observed
                    position = self._scan.position
                    if not gate.armed_for(position, self._generation):
                        # The old selection is not a receipt for this attempt.
                        if self._click_scan_slot(action.tile):
                            gate.arm(position, self._generation, token, runtime.capture.clock())
                            detail_deadline = time.monotonic() + 12
                        continue
                    index = self._slot_indices[action.tile.row, action.tile.column]
                    confirmed, level = self._read_selected_level(observed, gate, index, position)
                    if confirmed and level is None:
                        self.log_warning('本件等级未读清，保留失败计数并继续')
                        self._scan.complete_detail(
                            'failed', generation=self._generation, position=position, reason='level'
                        )
                        self._grade_fallback = None
                        gate.reset()
                        continue
                    proof = (
                        gate.observe(
                            frame,
                            viewport,
                            token,
                            acquired_at,
                            position,
                            self._generation,
                            level,
                            self._grade_tokens[index],
                            page_offset=self._scan.page_offset,
                        )
                        if confirmed
                        else None
                    )
                    if proof is None:
                        if (
                            self._grade_fallback in (None, 'done')
                            and time.monotonic() >= detail_deadline
                        ):
                            self._scan.pause(
                                '详情暂未刷新，扫描已暂停；切回游戏后继续',
                                generation=self._generation,
                            )
                            runtime.capture.pause(generation=self._generation)
                            gate.reset()
                        self.sleep(0.04)
                        continue
                    self._grade_fallback = None
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
            self._scan.drain(generation=self._generation)
            self._scan_job.stage('draining')
            with self._measure('parse_tail'):
                while self._scan.status.state == 'draining' and self._guard('commit'):
                    self._consume_results()
                    self._scan.drain(generation=self._generation)
                    if self._scan.status.state == 'draining':
                        self.sleep(0.05)
                        self._pipeline.cancelled.wait(0.005)
        except (OperationCancelled, FinishedException, TaskDisabledException):
            self._diagnostic_summary = 'scan_cancelled'
            if self._scan.generation == self._generation:
                self._scan.stop('扫描已取消，已收录记录保留', generation=self._generation)
        except ScanStartError as error:
            self._diagnostic_failure = True
            self._diagnostic_summary = 'scan_not_top'
            self._scan_job.exception('preparing', error, 'scan_not_top')
            self._scan.stop(str(error), generation=self._generation)
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
                self._scan.stop('扫描中断；已收录记录保留，请查看日志', generation=self._generation)
            raise
        finally:
            self._deferred_detail = None
            if self._scan.status.state == 'draining':
                self._scan.stop('解析已中断；保留已入库声骸', generation=self._generation)
            if self._grade_pipeline is not None:
                self._grade_pipeline.close()
            self._page_ready = False
            if self._pipeline is not None:
                self._pipeline.close()
                if runtime.scan_pipeline is self._pipeline:
                    runtime.scan_pipeline = None
            runtime.capture.stop(generation=self._generation)
            result = self._scan.result_for(self._generation)
            status = result.status if result is not None else None
            counters = dict(wheel_steps=self._wheel_steps, scroll_batches=self._scroll_batches)
            if status is not None:
                self.log_info(f'扫描结束：{status.message}；已处理 {status.visited}')
                counters.update(
                    visited=status.visited,
                    captured=status.captured,
                    added=status.added,
                    pending=status.pending,
                    failed=status.failed,
                    skipped=status.skipped,
                    backlog=status.backlog,
                )
                if not runtime._closed and self._scan.can_publish_result(result.generation):
                    self._write_report(runtime, started, result)
            self._scan_job.performance(self._timings.snapshot())
            state = (
                'failed'
                if self._diagnostic_failure
                else 'completed'
                if status is not None and status.state == 'finished'
                else 'cancelled'
            )
            code = self._diagnostic_summary or (
                'scan_threshold' if state == 'completed' and status.skipped else 'scan_' + state
            )
            self._scan_job.finish(state, code, counters=counters)
            with self._owner_lock:
                if self._scan_owner is owner:
                    self._scan_owner = None
                    self._enabled = False
                self._active_runtime = None

    def _write_report(self, runtime, started, result):
        # A single, bounded local diagnostic summary. No names, item/profile
        # identifiers, OCR, image content or device paths enter the report.
        import json
        import os
        from pathlib import Path
        import tempfile

        status = result.status
        payload = dict(
            schema=1,
            operation_id=self._scan_job.operation_id,
            state=status.state,
            minimum_level=result.minimum_level,
            visited=status.visited,
            added=status.added,
            pending=status.pending,
            captured=status.captured,
            backlog=status.backlog,
            skipped=status.skipped,
            failed=status.failed,
            wheel_steps=self._wheel_steps,
            failed_positions=[list(position) for position in result.failed_positions],
            failure_details=[
                dict(row=f.row, column=f.column, reason=f.reason) for f in result.failure_details
            ],
            failure_details_omitted=max(0, status.failed - len(result.failure_details)),
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
            # A replacement can be queued while the report is being flushed.
            with self._scan._lock:
                if not runtime._closed and self._scan.can_publish_result(result.generation):
                    os.replace(temporary, directory / 'scan-last-report.json')
        except OSError as error:
            self._scan_job.exception('report_export', error, 'failed')
            self.log_warning('扫描报告保存失败：' + type(error).__name__)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def on_destroy(self):
        with self._owner_lock:
            self._destroyed = True
            self._enabled = False
        self.cancel_bound_scan()
