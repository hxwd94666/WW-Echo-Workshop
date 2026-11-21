# 回放脱敏合成帧验证实时评分、冻结收录和自动扫描保护。
from importlib.metadata import version
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ww_echo_workshop.adapters.okww.capture_device import capture_source_identity
from ww_echo_workshop.adapters.okww.manual_capture import parse_manual_snapshot, run_manual_capture
from ww_echo_workshop.adapters.okww.overlay_task import WWEchoWorkshopOverlayTask
from ww_echo_workshop.adapters.okww.panel_ocr import make_panel_reader
from ww_echo_workshop.adapters.okww.private_ocr import PLUGIN_OCR
from ww_echo_workshop.adapters.okww.scan_observation import ScanObservation
from ww_echo_workshop.adapters.okww.scanning import WWEchoWorkshopScanTask
from ww_echo_workshop.application.compute_jobs import ComputeJobs
from ww_echo_workshop.application.diagnostics import begin_job
from ww_echo_workshop.application.inventory_query import InventoryQueryService
from ww_echo_workshop.application.live_snapshot import LiveSnapshotStore
from ww_echo_workshop.application.observations import ObservationService
from ww_echo_workshop.application.runtime import PluginRuntime
from ww_echo_workshop.capture.intake import CaptureSession, SingleCaptureRequest
from ww_echo_workshop.capture.evidence import evidence_bounds, prepare_evidence
from ww_echo_workshop.capture.manual_snapshot import ManualFrameBuffer
from ww_echo_workshop.capture.snapshot_parse import parse_snapshot_record
from ww_echo_workshop.capture.viewport import detect_echo_viewport
from ww_echo_workshop.capture.scan.levels import FixedFrameGate
from ww_echo_workshop.capture.scan.pipeline import ScanPipeline, ScanSnapshot, detail_signature
from ww_echo_workshop.capture.scan.traversal import ScanTraversal
from ww_echo_workshop.core.cancellation import OperationCancelled
from ww_echo_workshop.resources.weights import WeightSnapshot
from ww_echo_workshop.storage.evidence import EvidenceStore
from ww_echo_workshop.storage.inventory import EchoRepository


VIEW = (0, 0, 1600, 900)
FONT = Path('C:/Windows/Fonts/msyh.ttc')
ENGINE = Path(__file__).resolve().parents[2] / 'build/native/release/ww-echo-workshop-compute.exe'
SETTINGS = {
    '启用声骸评分': True,
    '策略浮层模式': 'qt',
    '角色评分模板': '今汐-通用',
    'TemplateCharacterID': '1304',
    'TemplateModalID': 'default',
}


def synthetic_frame(
    scene, *, bag_sub='13.8%', bag_stat='暴击伤害', bag_grade='+5', bag_cost='COST 4'
):
    # Only geometry and synthetic text. Never read any game screenshot or profile.
    canvas = Image.new('RGB', VIEW[2:], (32, 32, 32))
    draw, font = ImageDraw.Draw(canvas), ImageFont.truetype(str(FONT), 28)

    def text(x, y, value):
        draw.text((x, y), value, fill='white', font=font)

    def panel(x, value_x, top, skill, sub, stat='暴击伤害', cost='COST 4'):
        text(x, top - 54, cost)
        for i, (name, value) in enumerate((('暴击', '22.0%'), ('攻击', '150'), (stat, sub))):
            text(x, top + 38 * i, name)
            text(value_x, top + 38 * i, value)
        if skill:
            text(x, skill, '声骸技能')

    if scene == 'enhancement':
        text(300, 34, '声骸强化')
        panel(195, 455, 220, 0, '13.8%')
    elif scene == 'comparison':
        panel(850, 1100, 220, 390, '13.8%')
        panel(1260, 1480, 220, 390, '15.0%')
    elif scene == 'bag':
        text(205, 34, '声骸管理方案')
        text(1140, 100, '无妄者')
        text(1140, 162, bag_grade)
        panel(1140, 1460, 390, 664, bag_sub, bag_stat, bag_cost)
    else:
        text(250, 34, '声骸推荐')
        panel(1240, 1460, 222, 400, '13.8%')
    return np.asarray(canvas).copy()


def adapter():
    executor = SimpleNamespace(scene=None, config={}, ocr_po_translation=None, text_fix={})
    return WWEchoWorkshopOverlayTask(executor=executor, app=None)


def scan_slot_frames():
    frames = []
    grade_font = ImageFont.truetype(str(FONT), 20)
    label_font = ImageFont.truetype(str(FONT), 28)
    for selected, sub in enumerate(('13.8%', '15.0%', '16.2%')):
        image = Image.fromarray(synthetic_frame('bag', bag_sub=sub))
        draw = ImageDraw.Draw(image)
        for column in range(3):
            x, y = 146 + column * 147, 122
            border = (170, 220, 240) if selected == column else (220, 220, 220)
            draw.rectangle((x, y, x + 124, y + 151), outline=border, width=3)
            draw.text((x + 74, y + 121), '+5', font=grade_font, fill='white')
        draw.text((230, 797), '等级顺序', font=label_font, fill='white')
        draw.rectangle((560, 828, 570, 840), fill='white')
        frames.append(np.asarray(image).copy())
    return frames


@unittest.skipUnless(
    FONT.is_file() and ENGINE.is_file(), '需要 Windows 合成文字字体及已构建原生引擎'
)
class FrameReplayContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = ComputeJobs(ENGINE)
        cls.observations = ObservationService(compute=cls.jobs)
        cls.weights = WeightSnapshot.builtin()

    @classmethod
    def tearDownClass(cls):
        PLUGIN_OCR.close()
        cls.jobs.close()
        if not cls.jobs.wait_closed():
            raise AssertionError('Replay native compute worker is still running')

    def _live_fixture(self):
        task = adapter()
        runtime = SimpleNamespace(
            _closed=False,
            weights=SimpleNamespace(active=self.weights),
            observations=self.observations,
            stop_event=threading.Event(),
            live_snapshots=LiveSnapshotStore(),
            capture=SimpleNamespace(single_request=None, status=SimpleNamespace(state='stopped')),
            hud_state=Mock(),
        )
        settings_task = SimpleNamespace(runtime=runtime, config=dict(SETTINGS))
        task._settings_task = lambda: settings_task
        task._ensure_overlay = lambda: None
        task._ensure_recovery = lambda: None
        task.get_overlay_view = lambda: None
        return task, runtime, settings_task

    def test_live_result_is_not_published_after_intake_takes_ownership(self):
        frame = synthetic_frame('bag')
        for when in ('ocr', 'ocr_cycle', 'policy'):
            with self.subTest(when=when):
                task, runtime, settings = self._live_fixture()
                changed = []

                def start_intake():
                    runtime.capture.status = SimpleNamespace(state='armed')
                    if when == 'ocr_cycle':
                        runtime.capture.status = SimpleNamespace(state='stopped')
                    changed.append(True)

                def reader(*args, **kwargs):
                    panel = make_panel_reader(*args, **kwargs)
                    original = panel.read_region

                    def read_region(*region):
                        result = original(*region)
                        if when.startswith('ocr') and not changed:
                            start_intake()
                        return result

                    panel.read_region = read_region
                    return panel

                if when == 'policy':
                    settings.config['启用强化策略'] = True

                    def policy(*args):
                        start_intake()
                        return '策略：旧结果'

                    task._live_policy.observe = policy
                with (
                    patch(
                        'ww_echo_workshop.adapters.okww.overlay_task.device_manager',
                        return_value=SimpleNamespace(),
                    ),
                    patch(
                        'ww_echo_workshop.adapters.okww.overlay_task.read_capture_frame',
                        return_value=frame,
                    ),
                    patch(
                        'ww_echo_workshop.adapters.okww.overlay_task.make_panel_reader',
                        side_effect=reader,
                    ),
                ):
                    task.run()
                self.assertEqual([True], changed)
                self.assertIsNone(runtime.live_snapshots.latest())
                runtime.hud_state.update.assert_not_called()
                runtime.hud_state.hide.assert_called_once()

    def test_real_ocr_and_native_score_for_four_scenes(self):
        task, runtime, settings = self._live_fixture()
        settings.config['策略浮层模式'], overlay = 'gdi', Mock()
        task._ensure_overlay = lambda: overlay
        host = SimpleNamespace()
        summaries = []
        previous_bag = None
        for scene, layout, score in (
            ('display', 'display', 17.02),
            ('bag', 'bag', 17.02),
            ('bag-digit', 'bag', 17.73),
            ('comparison', 'comparison', 17.73),
            ('enhancement', 'enhancement', 17.02),
        ):
            with self.subTest(scene=scene):
                if scene == 'bag-digit':
                    frame = synthetic_frame('bag', bag_sub='15.0%')
                    # Preserve the coarse lattice exactly so only the detailed
                    # pixel change can trigger OCR during the full-read cooldown.
                    frame[::32, ::32] = previous_bag[::32, ::32]
                    np.testing.assert_array_equal(previous_bag[::32, ::32], frame[::32, ::32])
                    self.assertFalse(np.array_equal(previous_bag, frame))
                else:
                    frame = synthetic_frame(scene)
                    if scene == 'bag':
                        previous_bag = frame
                with (
                    patch(
                        'ww_echo_workshop.adapters.okww.overlay_task.device_manager',
                        return_value=host,
                    ),
                    patch(
                        'ww_echo_workshop.adapters.okww.overlay_task.read_capture_frame',
                        return_value=frame,
                    ),
                ):
                    if scene == 'bag-digit':
                        task._live._full_at = time.monotonic()
                    task.run()
                result = runtime.live_snapshots.latest()
                self.assertEqual(score, result.current_score)
                _, keywords = runtime.hud_state.update.call_args
                self.assertEqual(layout, keywords['layout'])
                self.assertEqual(VIEW, keywords['viewport'])
                if scene == 'comparison':
                    text = runtime.hud_state.update.call_args.args[0]
                    self.assertIn('17.02 / 17.73', text)
                if scene == 'bag-digit':
                    self.assertEqual((('暴击伤害', 15.0),), result.sub_rows)
                    print(
                        f'CAPTURE_DIGIT_REFRESH_OK {version("ok-script")}: {result.current_score}'
                    )
                else:
                    summaries.append((layout, score))
        print(f'CAPTURE_LIVE_REPLAY_OK {version("ok-script")}: {summaries}')
        overlay.draw.assert_called_with('echo-stat-boxes', task.painter.paint, duration=6.0)

    def test_manual_cancellation_and_scan_detail_replay(self):
        frame = synthetic_frame('bag')
        with (
            tempfile.TemporaryDirectory() as folder,
            closing(EchoRepository(Path(folder))) as repo,
        ):
            evidence = EvidenceStore(Path(folder))
            profile = repo.create_profile('合成档案')
            now = [1.0]
            capture = CaptureSession(
                repo,
                evidence,
                clock=lambda: now[0],
                job_factory=begin_job,
                quality=InventoryQueryService(compute=self.jobs).quality,
            )
            runtime = SimpleNamespace(
                capture=capture,
                observations=self.observations,
                _closed=False,
                stop_event=threading.Event(),
                backups=SimpleNamespace(restoring=False),
                weights=SimpleNamespace(active=self.weights),
                sonata=SimpleNamespace(active=None),
            )
            settings = SimpleNamespace(
                _destroyed=False,
                config=dict(SETTINGS, InventoryProfileID=profile),
                _game_foreground=Mock(return_value=True),
                _capture_ready=lambda: True,
            )
            task = adapter()
            task.executor._last_frame_time = 10
            task._enqueue_manual = Mock(return_value=True)
            task.executor.next_frame = lambda *, time_out: grab()
            host = SimpleNamespace(capture_method=object(), hwnd_window=object())
            original_capture = host.capture_method
            identity = capture_source_identity(host)
            samples = iter(())

            def grab():
                task.executor._last_frame_time += 1
                return next(samples, frame)

            def wait(delta):
                now[0] += delta
                return False

            def freeze(*frames):
                nonlocal samples
                samples = iter(frames)
                self.assertTrue(
                    capture.request_once(profile, weights=self.weights, source_identity=identity)
                )
                request = capture.single_request
                run_manual_capture(task, settings, runtime, request, wait=wait)
                return request, task._enqueue_manual.call_args.args[0]

            with patch(
                'ww_echo_workshop.adapters.okww.manual_capture.device_manager', return_value=host
            ):
                capture.arm_manual(profile)
                different = Image.fromarray(frame)
                draw = ImageDraw.Draw(different)
                font = ImageFont.truetype(str(FONT), 28)
                draw.text((1140, 505), '防御力', fill='white', font=font)
                draw.text((1460, 505), '40', fill='white', font=font)
                unstable, unstable_frame = freeze(np.asarray(different).copy(), frame)
                parse_manual_snapshot(
                    task, settings, runtime, unstable, unstable_frame, identity, settings.config
                )
                self.assertTrue(unstable.cancelled.is_set())
                self.assertIn('采样期间声骸信息发生变化', capture.status.message)
                self.assertEqual([], repo.list(profile))
                self.assertEqual([], list(evidence.folder.glob('*.png')))

                old, old_frame = freeze()
                self.assertFalse(capture.request_once(profile))
                capture.stop()
                self.assertTrue(old.cancelled.is_set())
                capture.arm_manual(profile)
                animation = frame.copy()
                animation[200:450, 650:920] = 215
                animation[540, 1430] = 255  # A lone sparkle in the detail background.
                current, current_frame = freeze(animation, frame)
                parse_manual_snapshot(
                    task, settings, runtime, old, old_frame, identity, settings.config
                )
                self.assertIs(capture.single_request, current)
                self.assertEqual([], repo.list(profile))
                # Focus may change after the two stable samples have frozen.
                settings._game_foreground.return_value = False
                parse_manual_snapshot(
                    task, settings, runtime, current, current_frame, identity, settings.config
                )
                (manual,) = repo.list(profile)
                self.assertEqual((4, 5), (manual.cost, manual.level))
                self.assertTrue(manual.screenshot_ref)
                self.assertEqual('armed', capture.status.state)
                settings._game_foreground.return_value = True
                changed, changed_frame = freeze()
                host.capture_method = object()
                parse_manual_snapshot(
                    task, settings, runtime, changed, changed_frame, identity, settings.config
                )
                self.assertTrue(changed.cancelled.is_set())
                self.assertEqual([manual.item_id], [entry.item_id for entry in repo.list(profile)])

                host.capture_method = original_capture
                late, late_frame = freeze()
                existing_pngs = set(evidence.folder.glob('*.png'))
                original_candidates = repo.duplicate_candidates

                def switched_candidates(record):
                    candidates = original_candidates(record)
                    self.assertEqual([manual.item_id], [item.item_id for item in candidates])
                    host.capture_method = object()
                    return candidates

                with patch.object(repo, 'duplicate_candidates', side_effect=switched_candidates):
                    parse_manual_snapshot(
                        task, settings, runtime, late, late_frame, identity, settings.config
                    )
                self.assertTrue(late.cancelled.is_set())
                self.assertIn('截图来源', capture.status.message)
                self.assertEqual([manual.item_id], [entry.item_id for entry in repo.list(profile)])
                self.assertEqual(0, capture.status.pending)
                self.assertEqual([], repo.pending_captures(profile))
                self.assertEqual(existing_pngs, set(evidence.folder.glob('*.png')))

                host.capture_method = original_capture
                writing, writing_frame = freeze()
                original_read = evidence.read

                def switch_during_png_verification(reference):
                    data = original_read(reference)
                    host.capture_method = object()
                    return data

                with patch.object(evidence, 'read', side_effect=switch_during_png_verification):
                    parse_manual_snapshot(
                        task,
                        settings,
                        runtime,
                        writing,
                        writing_frame,
                        identity,
                        settings.config,
                    )
                self.assertTrue(writing.cancelled.is_set(), 'source switched during PNG write')
                self.assertIn('截图来源', capture.status.message)
                self.assertEqual([], repo.pending_captures(profile))
                self.assertEqual(existing_pngs, set(evidence.folder.glob('*.png')))
                self.assertEqual([manual.item_id], [entry.item_id for entry in repo.list(profile)])
                with evidence.operation():
                    original_png = evidence.read(manual.screenshot_ref)
                    before = set(evidence.folder.iterdir())
                    self.assertIsNone(
                        evidence.put(str(uuid4()), original_png, publish_guard=lambda: False)
                    )
                    self.assertEqual(before, set(evidence.folder.iterdir()))
                    self.assertIsNone(
                        evidence.put(manual.item_id, original_png, publish_guard=lambda: False)
                    )
                    self.assertEqual(
                        manual.screenshot_ref, evidence.put(manual.item_id, original_png)
                    )
                    self.assertEqual(original_png, evidence.read(manual.screenshot_ref))

            gate = FixedFrameGate()
            gate.arm((0, 0), 7, 20, 10.0)
            self.assertIsNone(gate.observe(frame, VIEW, 20, 11.0, (0, 0), 7, 5, 18, page_offset=0))
            self.assertIsNone(gate.observe(frame, VIEW, 21, 11.0, (0, 0), 7, 5, 18, page_offset=0))
            proof = gate.observe(frame, VIEW, 22, 11.3, (0, 0), 7, 5, 18, page_offset=0)
            self.assertIsNotNone(proof)
            scan = ScanObservation()
            scan._scan_parser = make_panel_reader(
                task.executor, settings.config, threshold=task.ocr_default_threshold
            )
            scan._frozen_settings = settings.config
            scan._active_runtime = runtime
            scan._scan = SimpleNamespace(profile_id=profile, weights=self.weights, sonata=None)
            original = scan._scan_parser.read_region
            crops = []

            def read(frame, region, name):
                crops.append(name)
                if name == 'echo-bag-packed':
                    # Model a packed detector miss without changing the frozen original.
                    frame = np.zeros_like(frame)
                return original(frame, region, name)

            scan._scan_parser.read_region = read
            record, image = scan._snapshot_parser()(ScanSnapshot.freeze(frame, VIEW, proof))
            self.assertEqual(
                (manual.cost, manual.level, manual.main_stats, manual.sub_stats),
                (record.cost, record.level, record.main_stats, record.sub_stats),
            )
            self.assertIn('echo-detail-retry', crops)
            decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
            np.testing.assert_array_equal(decoded, frame[72:837, 1064:1592])
            capture.stop()
            now[0] = 9.0
            capture.start_scan(profile, 7)
            # The final check runs after native quality but before any evidence write.
            self.assertFalse(
                capture.commit_scan_snapshot(
                    record, image, proof, generation=7, commit_guard=Mock(side_effect=(True, False))
                )
            )
            self.assertEqual([manual.item_id], [item.item_id for item in repo.list(profile)])
            self.assertEqual(0, capture.status.pending)
            # Focus may also change while staging a duplicate is being planned.
            foreground = True

            def unfocused_candidates(record):
                nonlocal foreground
                candidates = original_candidates(record)
                self.assertEqual([manual.item_id], [item.item_id for item in candidates])
                foreground = False
                return candidates

            with patch.object(repo, 'duplicate_candidates', side_effect=unfocused_candidates):
                self.assertFalse(
                    capture.commit_scan_snapshot(
                        record, image, proof, generation=7, commit_guard=lambda: foreground
                    )
                )
            self.assertEqual([manual.item_id], [item.item_id for item in repo.list(profile)])
            self.assertEqual(0, capture.status.pending)
            self.assertEqual([], repo.pending_captures(profile))
            self.assertEqual(existing_pngs, set(evidence.folder.glob('*.png')))
            print(f'CAPTURE_LATE_GUARD_OK {version("ok-script")}: saved=1, pending=0, png=1')
        print(
            f'CAPTURE_INTAKE_REPLAY_OK {version("ok-script")}: frozen, cancelled, safe fallback, detail PNG'
        )

    def test_scaled_letterbox_has_original_pixel_evidence(self):
        content = cv2.resize(synthetic_frame('bag'), (1280, 720), interpolation=cv2.INTER_AREA)
        frame = cv2.copyMakeBorder(content, 32, 48, 80, 112, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        self.assertEqual((80, 32, 1280, 720), detect_echo_viewport(frame))
        task = adapter()
        parser = make_panel_reader(task.executor, SETTINGS, threshold=task.ocr_default_threshold)
        metadata, record = parse_snapshot_record(
            parser,
            frame,
            (80, 32, 1280, 720),
            SETTINGS,
            self.weights,
            '550e8400-e29b-41d4-a716-446655440000',
            None,
            scene='bag',
            observations=self.observations,
            cancel_event=threading.Event(),
        )
        self.assertEqual('bag', metadata.analysis.layout)
        self.assertTrue(metadata.analysis.layout_provisional)
        self.assertEqual((4, 5, 13.8), (record.cost, record.level, record.sub_stats[0].value))
        self.assertEqual(17.02, metadata.analysis.current_score)
        bounds = evidence_bounds(metadata.analysis, frame.shape[1], frame.shape[0])
        self.assertEqual((931, 90, 423, 612), bounds)
        previous = frame.copy()
        previous[220:430, 450:700] = 220
        request = SingleCaptureRequest(record.profile_id, 0.0, 20.0, None)
        buffer = ManualFrameBuffer(request)
        self.assertTrue(buffer.add(previous, 1, 1.0))
        self.assertTrue(buffer.add(frame, 2, 1.3))
        proof = buffer.freeze().prove(metadata, record)
        self.assertIsNotNone(proof)
        self.assertTrue(proof.valid(request, stable_frames=2, stable_seconds=0.3))
        _, png = prepare_evidence(record, frame, metadata.analysis)
        decoded = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(decoded, frame[90:702, 931:1354])
        print(
            f'CAPTURE_SCALED_EVIDENCE_OK {version("ok-script")}: viewport=(80,32,1280,720), crop={bounds}'
        )

    def test_scan_task_cancel_with_committed_row_and_inflight_ocr(self):
        from ok.device.capture_methods.bitblt import BitBltCaptureMethod
        from ww_echo_workshop.application.hud_state import HudState
        from ww_echo_workshop.capture.scan.levels import read_levels

        class WWEchoWorkshopSettingsTask(SimpleNamespace):
            pass

        frames = scan_slot_frames()
        executor = SimpleNamespace(
            scene=None,
            config={},
            ocr_po_translation=None,
            text_fix={},
            paused=False,
            exit_event=threading.Event(),
            _last_frame_time=0,
        )
        tool = WWEchoWorkshopScanTask(executor=executor, app=None)
        overlay = adapter()
        native_capture = object.__new__(BitBltCaptureMethod)
        host = SimpleNamespace(
            hwnd_window=SimpleNamespace(exists=True, pos_valid=True, hwnd=10, top_hwnd=10),
            capture_method=native_capture,
        )
        native_capture._hwnd_window = host.hwnd_window
        second_entered = threading.Event()
        cancelled_detail = threading.Event()
        selected, clicks = [0], []
        stopped = []
        corrected_grid_reads = []

        def read_with_one_grid_error(job, read):
            values = read_levels(job, read)
            if job.selected is None and job.page == 0:
                # Simulate a plausible bad list OCR before any item is clicked.
                return (0, *values[1:])
            if job.selected == 0:
                corrected_grid_reads.append(values[0])
            return values

        with (
            tempfile.TemporaryDirectory() as folder,
            closing(EchoRepository(Path(folder))) as repo,
        ):
            profile = repo.create_profile('合成扫描档案')
            evidence = EvidenceStore(Path(folder))
            capture = CaptureSession(
                repo,
                evidence,
                job_factory=begin_job,
                quality=InventoryQueryService(compute=self.jobs).quality,
            )
            scan = ScanTraversal()
            generation = scan.queue(profile, minimum_level=5, weights=self.weights)
            capture.start_scan(profile, generation)
            runtime = SimpleNamespace(
                capture=capture,
                scan=scan,
                _closed=False,
                stop_event=threading.Event(),
                observations=self.observations,
                data_dir=Path(folder),
                scan_pipeline=None,
                hud_state=HudState(),
            )
            runtime.hud_state.update('实时评分 17.02')
            settings = WWEchoWorkshopSettingsTask(
                runtime=runtime,
                config=dict(SETTINGS, InventoryProfileID=profile),
                _game_foreground=lambda: True,
                _capture_ready=lambda: True,
            )
            tool.get_tasks = lambda: (settings, overlay)
            overlay._settings_task = lambda: settings
            deadline = time.monotonic() + 15

            def step_sleep(delay):
                self.assertLess(time.monotonic(), deadline, 'synthetic scan did not settle')
                if (
                    not stopped
                    and scan.status.state == 'draining'
                    and second_entered.is_set()
                    and repo.list(profile)
                ):
                    stopped.append(len(clicks))
                    PluginRuntime.stop_intake(runtime)
                time.sleep(min(0.012, delay))

            def grab(*, time_out):
                self.assertEqual(0.5, time_out)
                self.assertFalse(
                    runtime.hud_state.snapshot()['visible'], 'old score survived into scan'
                )
                executor._last_frame_time += 1
                return frames[selected[0]]

            def click(x, y):
                self.assertFalse(stopped, 'input was sent after cancellation')
                self.assertTrue(120 < y < 275, (x, y))
                selected[0] = min(range(3), key=lambda col: abs(x - (208 + 147 * col)))
                clicks.append(selected[0])
                return True

            build_parser = tool._snapshot_parser

            def snapshot_parser(*, cancel_event):
                parse = build_parser(cancel_event=cancel_event)

                def wrapped(snapshot):
                    if snapshot.proof.position == (0, 1):
                        second_entered.set()
                        if not cancel_event.wait(7):
                            raise AssertionError('cancel did not reach in-flight parse')
                        cancelled_detail.set()
                        raise OperationCancelled()
                    return parse(snapshot)

                return wrapped

            tool._snapshot_parser = snapshot_parser
            tool._send_scan_click = click
            tool._send_scan_wheel = Mock(side_effect=AssertionError('short page must not scroll'))
            tool.sleep = step_sleep
            executor.next_frame = grab
            with (
                patch(
                    'ww_echo_workshop.adapters.okww.scanning.og',
                    SimpleNamespace(device_manager=host),
                ),
                patch(
                    'ww_echo_workshop.adapters.okww.scan_observation.device_manager',
                    return_value=host,
                ),
                patch(
                    'ww_echo_workshop.capture.scan.levels.read_levels',
                    side_effect=read_with_one_grid_error,
                ),
            ):
                tool.bind_queued_scan(runtime, generation)
                tool.run()

            self.assertEqual([5], corrected_grid_reads, 'grid misread stopped the scan')
            self.assertTrue(stopped)
            self.assertTrue(cancelled_detail.is_set())
            self.assertEqual([0, 1], clicks[:2])
            self.assertEqual(stopped[0], len(clicks))
            tool._send_scan_wheel.assert_not_called()
            self.assertIsNone(runtime.scan_pipeline)
            self.assertEqual('stopped', capture.status.state)
            self.assertEqual('interrupted', scan.status.state)
            (record,) = repo.list(profile)
            self.assertEqual((5, 13.8), (record.level, record.sub_stats[0].value))
            self.assertTrue(evidence.exists(record.screenshot_ref))
            self.assertEqual(1, len(list(evidence.folder.glob('*.png'))))
            report = json.loads((Path(folder) / 'scan-last-report.json').read_text('utf-8'))
            self.assertEqual((1, 0, 0), (report['added'], report['pending'], report['backlog']))
        print(f'CAPTURE_SCAN_CHAIN_OK {version("ok-script")}: committed=1, cancelled_ocr=1, png=1')


class CaptureCancellationContract(unittest.TestCase):
    def test_pipeline_cancel_releases_pending_without_publishing_late_result(self):
        entered = threading.Event()
        exit_worker = threading.Event()

        def parse(work):
            entered.set()
            if not exit_worker.wait(2):
                raise AssertionError('test did not release scan worker')
            return work

        closed_on = []
        pipeline = ScanPipeline(
            parse, on_worker_exit=lambda: closed_on.append(threading.current_thread())
        )
        try:
            self.assertTrue(pipeline.submit('active'))
            self.assertTrue(entered.wait(1))
            self.assertTrue(pipeline.submit('waiting'))
            self.assertEqual(2, pipeline.pending)
            pipeline.cancel()
            self.assertEqual(0, pipeline.pending)
            self.assertFalse(pipeline.submit('late'))
        finally:
            exit_worker.set()
            pipeline.close()
        self.assertIsNone(pipeline.poll())
        self.assertFalse(pipeline._thread.is_alive())
        self.assertEqual([pipeline._thread], closed_on)

    def test_packed_cancellation_does_not_retry_original_frame(self):
        from ww_echo_workshop.capture.scan.snapshot import snapshot_parser

        parser = SimpleNamespace(
            read_region=Mock(return_value=[]),
            read_metadata=Mock(side_effect=OperationCancelled()),
            read_record=Mock(),
        )
        parse = snapshot_parser(parser, {}, 'profile', None, None, None, threading.Event())
        snapshot = SimpleNamespace(
            frame=np.full((900, 1600, 3), 32, np.uint8),
            viewport=VIEW,
            proof=SimpleNamespace(level=5),
        )
        with self.assertRaises(OperationCancelled):
            parse(snapshot)
        parser.read_region.assert_called_once()
        self.assertEqual('echo-bag-packed', parser.read_region.call_args.args[2])
        parser.read_metadata.assert_called_once()
        parser.read_record.assert_not_called()


@unittest.skipUnless(FONT.is_file(), '需要 Windows 合成文字字体')
class ScanDetailProofContract(unittest.TestCase):
    def test_snapshot_checks_the_owned_copy_not_the_borrowed_frame(self):
        frame = synthetic_frame('bag')
        changed = synthetic_frame('bag', bag_sub='15.0%')
        gate = FixedFrameGate()
        gate.arm((0, 0), 7, 20, 10.0)
        self.assertIsNone(gate.observe(frame, VIEW, 21, 11.0, (0, 0), 7, 5, 18, page_offset=0))
        proof = gate.observe(frame, VIEW, 22, 11.3, (0, 0), 7, 5, 18, page_offset=0)
        self.assertIsNotNone(proof)
        self.assertFalse(replace(proof, grade_page=()).valid())
        self.assertFalse(replace(proof, grade_page=(8, 0, 18)).valid())
        self.assertFalse(replace(proof, position=(0, 6)).valid())

        class ReusedCaptureBuffer(np.ndarray):
            def copy(self):
                # Simulate a host reusing its borrowed buffer at the copy boundary.
                return changed.copy()

        with self.assertRaisesRegex(ValueError, 'Unverified scan snapshot'):
            ScanSnapshot.freeze(frame.view(ReusedCaptureBuffer), VIEW, proof)

        source = frame.copy()
        snapshot = ScanSnapshot.freeze(source, VIEW, proof)
        self.assertFalse(snapshot.frame.flags.writeable)
        source[:] = changed
        np.testing.assert_array_equal(frame, snapshot.frame)

    def test_two_frames_must_agree_on_grade_cost_and_stat_names(self):
        current = synthetic_frame('bag')
        animated = current.copy()
        animated[210:400, 650:920, :] = 250  # Model only, outside the selected panel.
        for field, earlier in (
            ('stat', synthetic_frame('bag', bag_stat='共鸣效率')),
            ('grade', synthetic_frame('bag', bag_grade='+15')),
            ('cost', synthetic_frame('bag', bag_cost='COST 3')),
        ):
            with self.subTest(field=field):
                gate = FixedFrameGate()
                gate.arm((0, 0), 7, 20, 10.0)
                for frame, token, at in ((earlier, 21, 11.0), (current, 22, 11.3)):
                    self.assertIsNone(
                        gate.observe(frame, VIEW, token, at, (0, 0), 7, 5, 18, page_offset=0)
                    )
                proof = gate.observe(animated, VIEW, 23, 11.6, (0, 0), 7, 5, 18, page_offset=0)
                self.assertIsNotNone(proof)
                self.assertTrue(proof.valid())
                self.assertEqual((22, 23), proof.tokens)
                self.assertEqual(detail_signature(current, VIEW), proof.signature)
                self.assertNotEqual(detail_signature(earlier, VIEW), proof.signature)
