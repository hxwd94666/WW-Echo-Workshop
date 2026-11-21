# 验证扫描结束报告在重新排队时仍绑定原会话，不调用真实采集。
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ww_echo_workshop.adapters.okww.scanning import WWEchoWorkshopScanTask
from ww_echo_workshop.adapters.okww.capture_commands import CaptureCommands
from ww_echo_workshop.application.runtime import PluginRuntime
from ww_echo_workshop.capture.intake import CaptureSession
from ww_echo_workshop.capture.scan.models import BagGrid, ScanTile
from ww_echo_workshop.capture.scan.traversal import ScanTraversal


class ScanReportContract(unittest.TestCase):
    def test_late_focus_loss_or_pause_yields_without_disarming_scan(self):
        settings_type = type('WWEchoWorkshopSettingsTask', (SimpleNamespace,), {})
        overlay_type = type('WWEchoWorkshopOverlayTask', (SimpleNamespace,), {})
        for paused, foreground in ((False, False), (True, True)):
            with self.subTest(paused=paused, foreground=foreground):
                now = [100.0]
                with (
                    patch('ww_echo_workshop.adapters.okww.scanning.time.monotonic', lambda: now[0]),
                    patch('ww_echo_workshop.adapters.okww.scanning.begin_job') as job,
                ):
                    scan = ScanTraversal()
                    generation = scan.queue('synthetic')
                    capture = CaptureSession(SimpleNamespace(profiles=lambda: {'synthetic'}), None)
                    capture.start_scan('synthetic', generation)
                    runtime = SimpleNamespace(scan=scan, capture=capture, _closed=False)
                    executor = SimpleNamespace(scene=None, config={}, paused=paused)
                    task = WWEchoWorkshopScanTask(executor=executor, app=None)
                    settings = settings_type(runtime=runtime, _game_foreground=Mock())
                    overlay = overlay_type(_hide_live=Mock())
                    task.get_tasks = lambda: (settings, overlay)
                    task._capture_identity = Mock(return_value='synthetic')
                    task._guard = Mock(return_value=False)
                    task._write_report = Mock()
                    task.sleep = Mock(side_effect=lambda _: now.__setitem__(0, 120.0))
                    task.bind_queued_scan(runtime, generation)
                    # The host can already have elected this task and be fetching a frame.
                    settings._game_foreground.return_value = foreground
                    task.run()

                    task.sleep.assert_not_called()
                    job.assert_not_called()
                    overlay._hide_live.assert_not_called()
                    self.assertIsNone(task._active_runtime)
                    self.assertTrue(task.enabled)
                    self.assertEqual('preparing', scan.status_for(generation).state)
                    self.assertEqual('active', capture.scan_status(generation).state)
                    # Deferral keeps the original queue timeout and owner.
                    now[0] = 119.9
                    self.assertFalse(task.expire_queued_scan())
                    now[0] = 120.0
                    self.assertTrue(task.expire_queued_scan())
                    self.assertFalse(task.enabled)
                    self.assertEqual('stopped', capture.status.state)

    def test_explicit_scan_uses_host_dispatch_without_forced_window_activation(self):
        from threading import Event, Lock

        from ok import TriggerTask
        from ok.task.TaskExecutor import TaskExecutor

        repo = SimpleNamespace(profiles=lambda: {'synthetic'})
        capture, scan = CaptureSession(repo, None), ScanTraversal()
        runtime = SimpleNamespace(
            scan=scan,
            capture=capture,
            repository=repo,
            _closed=False,
            backups=SimpleNamespace(restoring=False),
            weights=SimpleNamespace(active=None),
            sonata=SimpleNamespace(active=None),
        )
        executor = SimpleNamespace(
            scene=None,
            config={},
            lock=Lock(),
            exit_event=Event(),
            paused=False,
            onetime_task_queue=[],
            onetime_tasks=[],
            trigger_tasks=[],
            trigger_task_index=-1,
            trigger_sleep=Mock(),
            _wake_executor=Mock(),
            interaction=SimpleNamespace(on_run=Mock()),
        )
        executor.enqueue_onetime_task = lambda task: (
            executor.onetime_task_queue.append(task) or True
        )
        task = WWEchoWorkshopScanTask(executor=executor, app=None)
        task._capture_identity = lambda: 'synthetic'
        (
            executor.trigger_tasks if isinstance(task, TriggerTask) else executor.onetime_tasks
        ).append(task)
        overlay_type = type('WWEchoWorkshopOverlayTask', (SimpleNamespace,), {})
        command = CaptureCommands()
        command.runtime, command.executor, command.config = runtime, executor, {}
        command._capture_ready = lambda: True
        command.get_tasks = lambda: (task, overlay_type())
        command._ensure_signals = Mock()
        command.scan_progress_overlay = Mock()
        command._show_scan_start_notice = Mock()
        now = [100.0]
        with (
            patch(
                'ww_echo_workshop.adapters.okww.capture_commands.foreground_scan_supported',
                return_value=True,
            ),
            patch('ww_echo_workshop.adapters.okww.scanning.time.monotonic', lambda: now[0]),
            patch(
                'ww_echo_workshop.adapters.okww.scanning.game_foreground', return_value=False
            ) as foreground,
        ):
            self.assertTrue(command.start_scan('synthetic'))
            self.assertFalse(executor.onetime_task_queue)
            self.assertIsNone(TaskExecutor.next_task(executor)[0])
            foreground.return_value = True
            selected, _, is_trigger = TaskExecutor.next_task(executor)
            self.assertIs(task, selected)
            self.assertTrue(is_trigger)
            TaskExecutor._prepare_task_for_run(executor, is_trigger)
            # The host can skip an elected trigger if no capture frame arrives.
            executor.paused = True
            now[0] = 120.0
            command.expire_queued_scan()
        self.assertEqual('interrupted', scan.status.state)
        self.assertEqual('stopped', capture.status.state)
        self.assertFalse(task.enabled)
        executor.interaction.on_run.assert_not_called()

    def test_control_transition_keeps_accounting_for_an_already_committed_snapshot(self):
        from contextlib import closing
        from itertools import product

        from tests.contracts.test_capture_persistence import synthetic_png
        from ww_echo_workshop.capture.scan.pipeline import ScanProof
        from ww_echo_workshop.core.models import EchoRecord
        from ww_echo_workshop.storage.evidence import EvidenceStore
        from ww_echo_workshop.storage.inventory import EchoRepository

        for action, outcome in product(('pause', 'stop', 'replace'), ('added', 'pending')):
            with (
                self.subTest(action=action, outcome=outcome),
                tempfile.TemporaryDirectory() as folder,
                closing(EchoRepository(Path(folder))) as repo,
            ):
                profile = repo.create_profile('合成扫描档案')
                record = EchoRecord(profile, 4, (), level=25, name='synthetic')
                if outcome == 'pending':
                    repo.add(EchoRecord(profile, 4, (), level=25, name='synthetic'))
                capture = CaptureSession(
                    repo,
                    EvidenceStore(Path(folder)),
                    clock=lambda: 1.0,
                    quality=lambda rows, **_: [SimpleNamespace(record=rows[0], issues=())],
                )
                grid = BagGrid((ScanTile(0, 0, (0, 0, 40, 50), ()),), single_page=True)
                scan = ScanTraversal()
                generation = scan.begin(profile, grid, descending=True)
                capture.start_scan(profile, generation)
                self.assertEqual('click', scan.advance(grid).kind)
                self.assertEqual('read', scan.advance(replace(grid, selected=(0, 0))).kind)
                self.assertTrue(
                    scan.complete_detail('queued', generation=generation, position=(0, 0))
                )
                proof = ScanProof(
                    (0, 0), generation, 25, (2, 3), (1.1, 1.2), 'a' * 64, (generation, 0, 1)
                )
                parsed = (SimpleNamespace(proof=proof), (record, synthetic_png()), None)
                task = WWEchoWorkshopScanTask(
                    executor=SimpleNamespace(scene=None, config={}), app=None
                )
                task._scan, task._generation = scan, generation
                task._active_runtime = SimpleNamespace(capture=capture)
                task._pipeline = SimpleNamespace(
                    poll=Mock(side_effect=[parsed, parsed, None]), cancelled=None
                )
                task._deferred_detail = None
                task._guard = lambda _: scan.status.state in ('running', 'draining')
                commit = capture.commit_scan_snapshot

                def change_state_after_write(*args, **kwargs):
                    saved = commit(*args, **kwargs)
                    if saved and action == 'pause':
                        scan.pause(generation=generation)
                        capture.pause(generation=generation)
                    elif saved:
                        scan.stop(generation=generation)
                        capture.stop(generation=generation)
                        if action == 'replace':
                            capture.start_scan(profile, scan.queue(profile))
                    return saved

                with patch.object(
                    capture, 'commit_scan_snapshot', side_effect=change_state_after_write
                ) as write:
                    task._consume_results()
                    result = (
                        scan.status if action == 'pause' else scan.result_for(generation).status
                    )
                    self.assertEqual('paused' if action == 'pause' else 'interrupted', result.state)
                    self.assertEqual(
                        1, getattr(result, outcome), 'committed result was not counted'
                    )
                    self.assertEqual(1, result.added + result.pending)
                    self.assertEqual(0, result.backlog)
                    if action == 'pause':
                        self.assertTrue(scan.resume(generation=generation))
                        capture.resume(generation=generation)
                        # Even a late duplicate callback must not write again.
                        task._consume_results()
                        self.assertEqual('finished', scan.advance(grid).kind)
                        scan.drain(generation=generation)
                        self.assertEqual('finished', scan.status.state)
                    elif action == 'replace':
                        self.assertEqual('preparing', scan.status.state)
                        self.assertEqual(0, scan.status.added + scan.status.pending)
                        self.assertEqual(0, capture.status.added + capture.status.pending)
                    else:
                        self.assertEqual(result, scan.status)
                    self.assertFalse(
                        scan.resolve_detail(outcome, generation=generation, position=(0, 0))
                    )
                    write.assert_called_once()
                self.assertEqual(1, len(list(capture.evidence.folder.glob('*.png'))))

    def test_unload_cancels_only_the_scan_owned_by_that_task(self):
        settings_type = type('WWEchoWorkshopSettingsTask', (SimpleNamespace,), {})
        overlay_type = type('WWEchoWorkshopOverlayTask', (SimpleNamespace,), {})
        profile = 'synthetic'
        for phase in ('queued', 'running', 'replaced_by_scan', 'replaced_by_manual', 'late_start'):
            with self.subTest(phase=phase):
                repo = SimpleNamespace(profiles=lambda: {profile})
                capture, scan = CaptureSession(repo, None), ScanTraversal()
                runtime = SimpleNamespace(
                    scan=scan,
                    capture=capture,
                    repository=repo,
                    _closed=False,
                    scan_pipeline=None,
                    backups=SimpleNamespace(restoring=False),
                    weights=SimpleNamespace(active=None),
                    sonata=SimpleNamespace(active=None),
                )
                runtime.stop_intake = lambda: PluginRuntime.stop_intake(runtime)
                executor = SimpleNamespace(
                    scene=None,
                    config={},
                    enqueue_onetime_task=Mock(return_value=True),
                )
                task = WWEchoWorkshopScanTask(executor=executor, app=None)
                task._capture_identity = lambda: 'synthetic'
                command = CaptureCommands()
                command.runtime, command.executor, command.config = runtime, executor, {}
                command._capture_ready = lambda: True
                command.get_tasks = lambda: (task, overlay_type())
                task.get_tasks = lambda: (settings_type(runtime=runtime), overlay_type())
                command._ensure_signals = Mock()
                command.scan_progress_overlay = Mock()
                command._show_scan_start_notice = Mock()
                with patch(
                    'ww_echo_workshop.adapters.okww.capture_commands.foreground_scan_supported',
                    return_value=True,
                ):
                    self.assertTrue(command.start_scan(profile))
                executor.enqueue_onetime_task.assert_not_called()
                old = scan.generation
                pipe = Mock()
                if phase not in ('queued', 'late_start'):
                    task._active_runtime, task._generation = runtime, old
                    task._pipeline = runtime.scan_pipeline = pipe
                    scan.begin(
                        profile,
                        BagGrid((ScanTile(0, 0, (0, 0, 40, 50), ()),), single_page=True),
                        queued_generation=old,
                    )
                if phase.startswith('replaced') or phase == 'late_start':
                    runtime.stop_intake()
                    pipe.reset_mock()
                if phase in ('replaced_by_scan', 'late_start'):
                    current = scan.queue(profile)
                    capture.start_scan(profile, current)
                    runtime.scan_pipeline = Mock()
                elif phase == 'replaced_by_manual':
                    capture.arm_manual(profile)
                    runtime.scan_pipeline = None

                if phase == 'late_start':
                    task._guard = Mock(side_effect=AssertionError('obsolete task ran preflight'))
                    task.run()
                    task._guard.assert_not_called()
                else:
                    task.on_destroy()

                if phase in ('queued', 'running'):
                    self.assertEqual('interrupted', scan.status.state)
                    self.assertEqual('stopped', capture.status.state)
                    if phase == 'running':
                        pipe.cancel.assert_called_once()
                elif phase in ('replaced_by_scan', 'late_start'):
                    self.assertEqual(current, scan.generation)
                    self.assertEqual('preparing', scan.status.state)
                    self.assertEqual('active', capture.scan_status(current).state)
                    runtime.scan_pipeline.cancel.assert_not_called()
                else:
                    self.assertEqual('armed', capture.status.state)

    def test_old_finalizer_uses_own_counts_and_cannot_replace_newer_report(self):
        settings_type = type('WWEchoWorkshopSettingsTask', (SimpleNamespace,), {})
        overlay_type = type('WWEchoWorkshopOverlayTask', (SimpleNamespace,), {})
        profile = 'synthetic'
        grid = BagGrid(
            tuple(ScanTile(0, col, (col * 40, 0, 40, 50), ()) for col in range(2)),
            single_page=True,
        )
        for phase in ('current', 'before_report', 'during_fsync'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                scan = ScanTraversal()
                generation = scan.queue(profile, minimum_level=5)
                capture = CaptureSession(SimpleNamespace(profiles=lambda: {profile}), None)
                capture.start_scan(profile, generation)
                runtime = SimpleNamespace(
                    scan=scan,
                    capture=capture,
                    _closed=False,
                    data_dir=Path(folder),
                )
                report = runtime.data_dir / 'scan-last-report.json'
                previous = '{"operation_id":"previous"}'
                report.write_text(previous, 'utf-8')
                executor = SimpleNamespace(scene=None, config={}, paused=False)
                task = WWEchoWorkshopScanTask(executor=executor, app=None)
                task.get_tasks = lambda: (
                    settings_type(runtime=runtime, config={}, _game_foreground=lambda: True),
                    overlay_type(_hide_live=lambda: None),
                )
                task._guard = lambda: True
                task._capture_identity = lambda: 'synthetic'
                task.bind_queued_scan(runtime, generation)
                task._make_scan_parser = lambda: None

                def queue_replacement():
                    capture.stop()
                    capture.start_scan(profile, scan.queue(profile, minimum_level=20))

                def prepare_then_cancel():
                    scan.begin(profile, grid, minimum_level=5, queued_generation=generation)
                    for column, outcome in ((0, 'queued'), (1, 'failed')):
                        self.assertEqual('click', scan.advance(grid).kind)
                        position = (0, column)
                        self.assertEqual(
                            'read', scan.advance(replace(grid, selected=position)).kind
                        )
                        scan.complete_detail(
                            outcome, generation=generation, position=position, reason='level'
                        )
                        if outcome == 'queued':
                            scan.resolve_detail('added', generation=generation, position=position)
                    scan.stop(generation=generation)
                    if phase == 'before_report':
                        queue_replacement()
                    return None

                job = Mock(operation_id='old-scan')
                fsync = os.fsync

                def flush(fd):
                    fsync(fd)
                    if phase == 'during_fsync':
                        queue_replacement()

                task._prepare_sort = prepare_then_cancel
                with (
                    patch('ww_echo_workshop.adapters.okww.scanning.begin_job', return_value=job),
                    patch('os.fsync', side_effect=flush),
                ):
                    task.run()
                if phase == 'current':
                    payload = json.loads(report.read_text('utf-8'))
                    self.assertEqual('old-scan', payload['operation_id'])
                    self.assertEqual(
                        (5, 2, 1, 1),
                        tuple(
                            payload[field]
                            for field in ('minimum_level', 'visited', 'added', 'failed')
                        ),
                    )
                    self.assertEqual('interrupted', payload['state'])
                    self.assertEqual(
                        [{'row': 0, 'column': 1, 'reason': 'level'}], payload['failure_details']
                    )
                else:
                    self.assertEqual(previous, report.read_text('utf-8'))
                    self.assertEqual('preparing', scan.status.state)
                    self.assertEqual('active', capture.status.state)
                args, kwargs = job.finish.call_args
                self.assertEqual('cancelled', args[0])
                self.assertEqual(
                    (2, 1, 1),
                    tuple(kwargs['counters'][field] for field in ('visited', 'added', 'failed')),
                )
                self.assertEqual([], list(Path(folder).glob('.scan-report-*')))
