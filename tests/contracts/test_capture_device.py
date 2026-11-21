# 验证宿主取帧身份、像素格式和扫描输入许可，不调用真实设备。
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from ww_echo_workshop.adapters.okww.capture_device import (
    capture_connected,
    capture_source_identity,
    foreground_scan_supported,
    read_capture_frame,
    read_task_sample,
)
from ww_echo_workshop.adapters.okww.capture_recovery import CaptureRecoveryMonitor
from ww_echo_workshop.adapters.okww.scan_input import ScanInput
from ww_echo_workshop.adapters.okww.scan_observation import ScanObservation
from ww_echo_workshop.capture.scan.levels import page_content, page_moved
from ww_echo_workshop.capture.scan.models import ScanTile


class CaptureDeviceContract(unittest.TestCase):
    def test_manual_comparison_keeps_the_right_panel_through_same_frame_retry(self):
        from dataclasses import replace

        import cv2

        from ww_echo_workshop.capture.evidence import evidence_bounds, prepare_evidence
        from ww_echo_workshop.capture.panel_reader import OcrTextBox, PanelReader
        from ww_echo_workshop.capture.snapshot_parse import parse_snapshot_record
        from ww_echo_workshop.resources.weights import WeightSnapshot

        frame = np.full((900, 1600, 3), 32, np.uint8)
        frame[94:837, 1240:1592] = 75
        viewport = (0, 0, 1600, 900)
        fixture = tuple(
            OcrTextBox(x, y, 55, 26, label, 0.99)
            for x, y, label in (
                (850, 165, 'COST 3'),
                (1220, 110, '声骸名称：角'),
                (1220, 146, '等级：20'),
                (850, 220, '暴击'),
                (1100, 220, '22.0%'),
                (850, 258, '攻击'),
                (1100, 258, '150'),
                (850, 296, '暴击伤害'),
                (1100, 296, '13.8%'),
                (850, 390, '声骸技能'),
                (1265, 110, '声骸名称：无妄者'),
                (1265, 146, '等级：5'),
                (1260, 172, 'COST 4'),
                (1260, 220, '暴击'),
                (1480, 220, '22.0%'),
                (1260, 258, '攻击'),
                (1480, 258, '150'),
                (1260, 296, '暴击伤害'),
                (1480, 296, '15.0%'),
                (1260, 390, '声骸技能'),
            )
        )
        weights = WeightSnapshot.builtin()
        score = SimpleNamespace(
            current_score=17.73, potential_score=100, row_scores=(), rating=None
        )
        observations = SimpleNamespace(
            evaluate=lambda _main, sub, **kw: SimpleNamespace(
                cost=kw.get('cost') or 4, score_for=lambda _: score, tiers=(None,) * len(sub)
            )
        )
        for retry in (False, True):
            with self.subTest(retry=retry):
                regions = []

                def read_region(pixels, rect, name):
                    self.assertIs(frame, pixels)
                    regions.append((name, rect))
                    x, y, w, h = rect
                    return [box for box in fixture if x <= box.x < x + w and y <= box.y < y + h]

                parser = PanelReader(read_region)
                read_metadata = parser.read_metadata

                def parse_metadata(*args, **kwargs):
                    result = read_metadata(*args, **kwargs)
                    if retry and kwargs['boxes'] is None:
                        return replace(result, issues=('ocr:synthetic_retry',))
                    return result

                parser.read_metadata = parse_metadata
                metadata, record = parse_snapshot_record(
                    parser,
                    frame,
                    viewport,
                    {'TemplateCharacterID': '1304', 'TemplateModalID': 'default'},
                    weights,
                    '550e8400-e29b-41d4-a716-446655440000',
                    None,
                    observations=observations,
                )
                self.assertTrue(metadata.analysis.comparison)
                self.assertEqual(
                    ('无妄者', 5, 4, 15.0),
                    (record.name, record.level, record.cost, record.sub_stats[0].value),
                )
                self.assertNotIn('等级：20', record.raw_text)
                self.assertEqual(
                    (1240, 94, 352, 743), evidence_bounds(metadata.analysis, 1600, 900)
                )
                retry_regions = [r for name, r in regions if name.startswith('echo-detail-retry')]
                if retry:
                    self.assertTrue(retry_regions)
                self.assertTrue(all(region[0] >= 1240 for region in retry_regions))
                _, png = prepare_evidence(record, frame, metadata.analysis)
                decoded = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
                np.testing.assert_array_equal(decoded, frame[94:837, 1240:1592])

    def test_live_score_cancels_obsolete_ocr_and_compute(self):
        from ww_echo_workshop.adapters.okww.overlay_task import WWEchoWorkshopOverlayTask
        from ww_echo_workshop.application.hud_state import HudState
        from ww_echo_workshop.capture.panel_reader import PanelReader
        from ww_echo_workshop.core.cancellation import checkpoint

        frame = np.full((900, 1920, 3), 32, np.uint8)
        for phase in ('ocr', 'compute'):
            with self.subTest(phase=phase):
                runtime = SimpleNamespace(
                    _closed=False,
                    capture=SimpleNamespace(
                        single_request=None, status=SimpleNamespace(state='stopped')
                    ),
                    stop_event=threading.Event(),
                    weights=SimpleNamespace(active=SimpleNamespace(version='original')),
                    observations=object(),
                    hud_state=HudState(),
                    live_snapshots=Mock(),
                )
                runtime.hud_state.update('实时评分 17.02')
                settings = SimpleNamespace(config={}, runtime=runtime)
                executor = SimpleNamespace(scene=None, config={})
                task = WWEchoWorkshopOverlayTask(executor=executor, app=None)
                task._settings_task = lambda: settings
                task._ensure_recovery = Mock()
                task._source_identity = lambda: 'synthetic'
                task.get_overlay_view = lambda: None
                regions = []

                def read_region(_frame, region, _name):
                    regions.append(region)
                    if phase == 'ocr':
                        if len(regions) > 1:
                            self.fail('obsolete frame entered another OCR region')
                        runtime.capture.status = SimpleNamespace(state='armed')
                    return []

                def analyze(*_args, cancel_event, **_kwargs):
                    self.assertEqual('compute', phase)
                    self.assertFalse(cancel_event.is_set())
                    runtime.weights.active = SimpleNamespace(version='replacement')
                    checkpoint(cancel_event)
                    self.fail('old weights still permitted native compute')

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
                        return_value=PanelReader(read_region),
                    ),
                    patch(
                        'ww_echo_workshop.capture.panel_reader.analyze_echo_stats',
                        side_effect=analyze,
                    ),
                ):
                    task.run()
                runtime.live_snapshots.capture.assert_not_called()
                self.assertFalse(runtime.hud_state.snapshot()['visible'])
                self.assertFalse(runtime.stop_event.is_set())
                self.assertEqual(1 if phase == 'ocr' else 2, len(regions))

    def test_manual_intake_hides_live_scores_before_sampling(self):
        from ww_echo_workshop.adapters.okww.overlay_task import (
            ECHO_STAT_PAINTER_KEY,
            WWEchoWorkshopOverlayTask,
        )
        from ww_echo_workshop.application.hud_state import HudState

        for native in (False, True):
            with self.subTest(native=native):
                request = object()
                runtime = SimpleNamespace(
                    _closed=False,
                    capture=SimpleNamespace(single_request=request),
                    hud_state=HudState(),
                )
                runtime.hud_state.update('实时评分 17.02')
                settings = SimpleNamespace(runtime=runtime)
                executor = SimpleNamespace(scene=None, config={})
                task = WWEchoWorkshopOverlayTask(executor=executor, app=None)
                native_overlay = Mock() if native else None
                task.get_overlay_view = lambda: native_overlay
                task._settings_task = lambda: settings

                def start_sampling(*args):
                    self.assertFalse(
                        runtime.hud_state.snapshot()['visible'], 'old score survived into intake'
                    )
                    if native:
                        native_overlay.clear_draw.assert_any_call(ECHO_STAT_PAINTER_KEY)
                    return False

                with patch(
                    'ww_echo_workshop.adapters.okww.overlay_task.run_manual_capture',
                    side_effect=start_sampling,
                ) as sample:
                    task.run()
                sample.assert_called_once_with(task, settings, runtime, request)

    def test_manual_sampling_rejects_replaced_context_before_read_or_enqueue(self):
        from ww_echo_workshop.adapters.okww.manual_capture import run_manual_capture
        from ww_echo_workshop.capture.intake import CaptureSession

        for phase in ('foreground_replaces_request', 'ready_changes_source', 'copy_changes_source'):
            with self.subTest(phase=phase):
                now = [1.0]
                host = SimpleNamespace(capture_method=object(), hwnd_window=None)
                capture = CaptureSession(
                    SimpleNamespace(profiles=lambda: {'synthetic'}),
                    None,
                    clock=lambda: now[0],
                    job_factory=Mock(),
                )
                self.assertTrue(
                    capture.request_once('synthetic', source_identity=capture_source_identity(host))
                )
                request = capture.single_request
                replacement = []
                runtime = SimpleNamespace(
                    _closed=False,
                    capture=capture,
                    backups=SimpleNamespace(restoring=False),
                    stop_event=threading.Event(),
                )
                executor = SimpleNamespace(_last_frame_time=0)
                task = SimpleNamespace(
                    _destroyed=False, executor=executor, log_info=Mock(), _enqueue_manual=Mock()
                )

                class CopyChangesSource(np.ndarray):
                    def copy(self):
                        host.capture_method = object()
                        return np.asarray(self).copy()

                def foreground():
                    if phase == 'foreground_replaces_request' and not replacement:
                        capture.stop()
                        self.assertTrue(capture.request_once('synthetic'))
                        replacement.append(capture.single_request)
                    return True

                def ready():
                    if phase == 'ready_changes_source':
                        host.capture_method = object()
                    return True

                def sample(*, time_out):
                    executor._last_frame_time += 1
                    frame = np.full((90, 160, 3), 32, np.uint8)
                    if phase == 'copy_changes_source' and executor._last_frame_time == 2:
                        return frame.view(CopyChangesSource)
                    return frame

                def wait(delay):
                    now[0] += delay
                    return False

                executor.next_frame = Mock(side_effect=sample)
                settings = SimpleNamespace(
                    _destroyed=False,
                    config={'InventoryProfileID': 'synthetic'},
                    _game_foreground=foreground,
                    _capture_ready=ready,
                )
                with patch(
                    'ww_echo_workshop.adapters.okww.manual_capture.device_manager',
                    return_value=host,
                ):
                    run_manual_capture(task, settings, runtime, request, wait=wait)
                reads = 2 if phase == 'copy_changes_source' else 0
                self.assertEqual(reads, executor.next_frame.call_count)
                self.assertEqual(0, task._enqueue_manual.call_count)
                self.assertTrue(request.cancelled.is_set())
                if replacement:
                    self.assertIs(replacement[0], capture.single_request)
                    self.assertFalse(replacement[0].cancelled.is_set())
                else:
                    self.assertIsNone(capture.single_request)
                    self.assertIn('截图来源', capture.status.message)

    def test_manual_geometry_is_measured_from_the_owned_sample(self):
        from ww_echo_workshop.capture.intake import SingleCaptureRequest
        from ww_echo_workshop.capture.manual_snapshot import ManualFrameBuffer

        original = np.full((90, 160, 3), 32, np.uint8)
        letterboxed = original.copy()
        letterboxed[:, :5] = 0
        extended = np.pad(original, ((0, 4), (0, 0), (0, 0)))
        request = SingleCaptureRequest('synthetic', 0.0, 20.0, None)

        for case, replacement in (
            ('letterbox', letterboxed),
            ('blank', np.zeros_like(original)),
            ('capture_size', extended),
        ):
            with self.subTest(case=case):

                class ReusedFrame(np.ndarray):
                    def copy(self):
                        return replacement.copy()

                buffer = ManualFrameBuffer(request)
                self.assertTrue(buffer.add(original, 1, 1.0))
                with self.assertRaisesRegex(ValueError, '采样画面无效或尺寸变化'):
                    buffer.add(original.view(ReusedFrame), 2, 1.3)
                self.assertTrue(buffer.add(original, 3, 1.6))
                snapshot = buffer.freeze()
                self.assertEqual((1, 3), snapshot.tokens)
                self.assertEqual((0, 0, 160, 90), snapshot.viewport)
                self.assertFalse(snapshot.frame.flags.writeable)
                self.assertFalse(np.shares_memory(snapshot.frame, original))
                np.testing.assert_array_equal(snapshot.frame, original)

    def test_capture_recovery_does_not_close_capture_during_other_host_work(self):
        from ok.device.capture_methods.bitblt import BitBltCaptureMethod

        for case in (
            'busy',
            'queued',
            'browser',
            'paused',
            'late_task',
            'late_choice',
            'late_stop',
            'idle',
            'restarted',
        ):
            with self.subTest(case=case):
                window = SimpleNamespace(hwnd=10, exists=True, pos_valid=True, visible=True)
                native = object.__new__(BitBltCaptureMethod)
                native._hwnd_window = window
                native.close = Mock()
                executor = SimpleNamespace(
                    paused=case == 'paused',
                    current_task=object() if case == 'busy' else None,
                    onetime_task_queue=[object()] if case == 'queued' else [],
                    _last_frame_time=0,
                )
                manager = SimpleNamespace(
                    hwnd_window=window,
                    capture_method=native,
                    executor=executor,
                    config={'preferred': 'pc_10'},
                    get_preferred_device=Mock(
                        return_value={
                            'device': 'browser' if case == 'browser' else 'windows',
                            'real_hwnd': 10,
                        }
                    ),
                    refresh=Mock(),
                )
                if case == 'restarted':
                    manager.config['preferred'] = 'pc_9'
                    manager.get_preferred_device.return_value = None
                monitor = CaptureRecoveryMonitor(manager, threading.Event())

                def connected():
                    if case == 'late_task':
                        executor.current_task = object()
                    if case == 'late_choice':
                        manager.get_preferred_device.return_value = {'device': 'browser'}
                    if case == 'late_stop':
                        monitor.stop()
                    return False

                native.connected = Mock(side_effect=connected)
                self.assertEqual(case in ('idle', 'restarted'), monitor.poll())
                if case in ('idle', 'restarted'):
                    self.assertFalse(monitor.poll())  # Keep the existing retry throttle.
                    native.close.assert_called_once()
                    manager.refresh.assert_called_once()
                else:
                    native.close.assert_not_called()
                    manager.refresh.assert_not_called()

    def test_overlay_only_allows_recovery_while_scoring_owns_an_idle_session(self):
        from ww_echo_workshop.adapters.okww.overlay_task import WWEchoWorkshopOverlayTask

        executor = SimpleNamespace(
            scene=None,
            config={},
            ocr_po_translation=None,
            text_fix={},
            exit_event=threading.Event(),
        )
        task = WWEchoWorkshopOverlayTask(executor=executor, app=None)
        runtime = SimpleNamespace(
            _closed=False,
            stop_event=threading.Event(),
            capture=SimpleNamespace(status=SimpleNamespace(state='stopped')),
            backups=SimpleNamespace(restoring=False),
        )
        settings = SimpleNamespace(runtime=runtime, config={'启用声骸评分': True})
        task._settings_task = lambda: settings
        manager = SimpleNamespace()
        with (
            patch(
                'ww_echo_workshop.adapters.okww.overlay_task.og',
                SimpleNamespace(device_manager=manager),
            ),
            patch('ww_echo_workshop.adapters.okww.overlay_task.CaptureRecoveryMonitor') as factory,
        ):
            task._ensure_recovery()
        self.assertIs(task, factory.call_args.kwargs['owner_task'])
        factory.return_value.start.assert_called_once()
        allowed = factory.call_args.kwargs['allowed']
        self.assertTrue(allowed())
        for state in ('armed', 'single', 'active', 'paused'):
            runtime.capture.status.state = state
            self.assertFalse(allowed(), state)
        runtime.capture.status.state = 'stopped'
        runtime.backups.restoring = True
        self.assertFalse(allowed())
        runtime.backups.restoring = False
        settings.config['启用声骸评分'] = False
        self.assertFalse(allowed())
        settings.config['启用声骸评分'] = True
        task._destroyed = True
        self.assertFalse(allowed())

    def test_capture_waits_for_native_capture_binding_to_follow_host_window(self):
        from ok.device.capture_methods.bitblt import BitBltCaptureMethod

        window = SimpleNamespace(exists=True, pos_valid=True, hwnd=10, top_hwnd=10)
        frame = np.zeros((21, 22, 3), dtype=np.uint8)
        capture = object.__new__(BitBltCaptureMethod)
        capture._hwnd_window = SimpleNamespace(**vars(window))  # Recycled HWNDs must rebind.
        capture.get_frame = Mock(return_value=frame)
        capture.connected = lambda: True
        host = SimpleNamespace(hwnd_window=window, capture_method=capture)
        executor = SimpleNamespace(paused=False, _last_frame_time=0)

        def grab(**_):
            executor._last_frame_time += 1
            return frame

        def sample():
            return read_task_sample(
                SimpleNamespace(executor=executor),
                lambda: 1.0,
                lambda: host,
            )

        executor.next_frame = Mock(side_effect=grab)
        self.assertIsNone(read_capture_frame(host))
        self.assertFalse(capture_connected(host, executor))
        self.assertIsNone(sample())
        capture.get_frame.assert_not_called()
        executor.next_frame.assert_not_called()
        capture._hwnd_window = window
        self.assertTrue(capture_connected(host, executor))
        self.assertIs(frame, read_capture_frame(host))
        self.assertIs(frame, sample().pixels)
        for change in ('manager', 'override', 'inflight'):
            with self.subTest(change=change):
                executor.device_manager = object() if change == 'manager' else host
                executor.method = object() if change == 'override' else capture
                executor.next_frame.reset_mock()

                def swap_during_capture(**kwargs):
                    result = grab(**kwargs)
                    executor.method = object()
                    return result

                executor.next_frame.side_effect = (
                    swap_during_capture if change == 'inflight' else grab
                )
                result = sample()
                self.assertTrue(result is None, 'executor pixels bypassed source binding')
                self.assertEqual(1 if change == 'inflight' else 0, executor.next_frame.call_count)
                self.assertFalse(capture_connected(host, executor))

    def test_capture_rejects_format_and_same_handle_binding_changes(self):
        from ok.task.exceptions import CaptureException

        window = SimpleNamespace(
            exists=True, pos_valid=True, hwnd=10, top_hwnd=10, capture_target_signature=(10, 100)
        )
        frame = np.full((21, 22, 3), 32, dtype=np.uint8)
        capture = SimpleNamespace(get_frame=lambda: frame)
        host = SimpleNamespace(hwnd_window=window, capture_method=capture)
        self.assertIs(frame, read_capture_frame(host))
        for mismatched in (
            frame.astype(np.float16),
            np.full((21, 22, 4), 32, dtype=np.uint8),
            np.full((21, 22), 32, dtype=np.uint8),
        ):
            with self.subTest(dtype=mismatched.dtype, shape=mismatched.shape):
                capture.get_frame = lambda: mismatched
                self.assertIsNone(read_capture_frame(host))

        def switched():
            window.capture_target_signature = (10, 120)
            return frame

        capture.get_frame = switched
        self.assertIsNone(read_capture_frame(host))

        def executor_grab(**_):
            executor._last_frame_time += 1
            window.capture_target_signature = (10, 140)
            return frame

        executor = SimpleNamespace(next_frame=executor_grab, _last_frame_time=2)
        task = SimpleNamespace(executor=executor)

        self.assertIsNone(read_task_sample(task, lambda: 3.0, lambda: host))
        executor.next_frame = Mock(side_effect=CaptureException('fixture capture reset'))
        self.assertIsNone(read_task_sample(task, lambda: 3.0, lambda: host))

    def test_host_tonemapped_hdr_pixels_are_read_only_and_not_automatic(self):
        import ctypes

        try:
            from ok.device.capture_methods.windows_graphics_hdr import (
                WindowsGraphicsCaptureHDRMethod,
            )
        except ImportError:
            self.skipTest('该宿主版本没有 WGC_HDR 捕获类')
        pixels = np.zeros((12, 16, 4), np.float16)
        pixels[:, :, :] = (1.0, 0.25, 0.0625, 1.0)
        capture = object.__new__(WindowsGraphicsCaptureHDRMethod)
        capture.exit_event = threading.Event()
        capture.last_size = SimpleNamespace(Height=12, Width=16)
        capture.do_get_frame = lambda: capture.read_mapped_frame(
            SimpleNamespace(pData=ctypes.c_void_p(pixels.ctypes.data), RowPitch=pixels.strides[0])
        )
        window = SimpleNamespace(exists=True, pos_valid=True)
        capture._hwnd_window = window
        host = SimpleNamespace(hwnd_window=window, capture_method=capture)
        sampled = read_capture_frame(host)
        self.assertIsNotNone(sampled)
        self.assertEqual(((12, 16, 3), np.dtype('uint8')), (sampled.shape, sampled.dtype))
        b, g, r = map(int, sampled[0, 0])
        self.assertLess(b, g)
        self.assertLess(g, r)
        self.assertFalse(foreground_scan_supported(host))

    def test_fresh_samples_and_native_foreground_capability(self):
        from ok.device.capture_methods.bitblt import BitBltCaptureMethod
        from ok.device.capture_methods.windows_graphics import WindowsGraphicsCaptureMethod

        window = SimpleNamespace(exists=True, pos_valid=True, hwnd=10, top_hwnd=10)
        host = SimpleNamespace(hwnd_window=window, capture_method=None)
        for cls in (WindowsGraphicsCaptureMethod, BitBltCaptureMethod):
            host.capture_method = object.__new__(cls)
            # Populate only the host class's backing field; a real WGC setter
            # would start the actual window capture service.
            host.capture_method._hwnd_window = window
            self.assertTrue(foreground_scan_supported(host))
            host.capture_method._hwnd_window = object()
            self.assertFalse(foreground_scan_supported(host))
        host.capture_method = SimpleNamespace(hwnd_window=window, get_abs_cords=lambda *_: ())
        self.assertFalse(foreground_scan_supported(host))
        task = SimpleNamespace(
            executor=SimpleNamespace(_last_frame_time=10, next_frame=lambda **_: object())
        )

        self.assertIsNone(read_task_sample(task, lambda: 1.0, lambda: host))

        def replaced(*, time_out):
            self.assertEqual(0.5, time_out)
            task.executor._last_frame_time += 1
            host.hwnd_window = SimpleNamespace(hwnd=10, top_hwnd=10)
            return np.zeros((21, 22, 3), np.uint8)

        task.executor.next_frame = replaced
        self.assertIsNone(read_task_sample(task, lambda: 1.0, lambda: host))

    def test_no_background_or_draining_input_and_no_unacknowledged_scroll(self):
        from ww_echo_workshop.capture.scan.access import ScanAccess, scan_access

        env = dict(
            current=True, cancelled=False, context_matches=True, host_paused=False, ready=True
        )
        self.assertIs(
            ScanAccess.OUT_OF_FOCUS,
            scan_access('running', operation='input', foreground=False, **env),
        )
        self.assertIs(
            ScanAccess.INACTIVE, scan_access('draining', operation='input', foreground=True, **env)
        )
        self.assertIs(
            ScanAccess.OUT_OF_FOCUS,
            scan_access('draining', operation='commit', foreground=False, **env),
        )
        tiles = [ScanTile(0, i, (i * 40, 0, 40, 40), ()) for i in range(4)]
        first = np.full((40, 160, 3), 30, np.uint8)
        other = first.copy()
        other[:, :40, :] = 210
        self.assertFalse(page_moved(page_content(first, tiles), page_content(other, tiles)))
        other[:, 40:, :] = 210
        self.assertTrue(page_moved(page_content(first, tiles), page_content(other, tiles)))

    def test_production_click_and_scroll_never_reach_native_input_when_blocked(self):
        class Fixture(ScanInput, ScanObservation):
            def _capture_identity(self):
                return 'source'

        task = Fixture()
        task._identity, task._generation, task._destroyed = 'source', 2, False
        task.executor = SimpleNamespace(paused=False, exit_event=threading.Event())
        task._scan = SimpleNamespace(
            status=SimpleNamespace(state='running'),
            generation=2,
            profile_id='profile',
            pause=Mock(),
            stop=Mock(),
        )
        capture = SimpleNamespace(
            status=SimpleNamespace(mode='automatic', state='active'), pause=Mock()
        )
        task._scan.status_for = lambda generation: (
            task._scan.status if task._scan.generation == generation else None
        )
        capture.scan_status = lambda generation: capture.status if generation == 2 else None
        task._active_runtime = SimpleNamespace(
            capture=capture, _closed=False, stop_event=threading.Event()
        )
        task._settings = SimpleNamespace(
            config={'InventoryProfileID': 'profile'},
            _capture_ready=lambda: True,
            _game_foreground=Mock(return_value=False),
        )
        with (
            patch(
                'ww_echo_workshop.adapters.okww.scan_observation.foreground_scan_supported',
                return_value=True,
            ),
            patch('mouse.press') as press,
            patch('mouse.wheel') as wheel,
        ):
            self.assertFalse(task._send_scan_click(1, 1))
            task._scan.pause.assert_called_once()
            self.assertFalse(task._send_scan_wheel(1, 1, -1))
            task._settings._game_foreground.return_value = True
            task._settings.config['InventoryProfileID'] = 'replacement'
            self.assertFalse(task._send_scan_click(1, 1))
            task._scan.stop.assert_called_once()
            press.assert_not_called()
            wheel.assert_not_called()

    def test_scan_rechecks_button_and_hover_before_foreground_input(self):
        import sys

        point = (210, 180)
        for act in ('click', 'wheel'):
            for change in ('stable', 'user_pressed', 'pointer_drift'):
                with self.subTest(action=act, change=change):
                    task = ScanInput()
                    position_reads = 0
                    task._generation = 4
                    task._scan = SimpleNamespace(pause=Mock())
                    capture = SimpleNamespace(pause=Mock())
                    task._active_runtime = SimpleNamespace(capture=capture)
                    task._guard = Mock(return_value=True)
                    task._move_scan_pointer = Mock(return_value=True)
                    task.sleep = Mock()
                    task.executor = SimpleNamespace(reset_scene=Mock())
                    manager = SimpleNamespace(
                        capture_method=SimpleNamespace(get_abs_cords=lambda *_: point)
                    )

                    def get_position():
                        nonlocal position_reads
                        position_reads += 1
                        if change == 'pointer_drift' and position_reads > 1:
                            return (point[0] + 14, point[1])
                        return point

                    mouse = SimpleNamespace(
                        get_position=get_position,
                        is_pressed=lambda button: (
                            change == 'user_pressed' and position_reads > 0 and button == 'left'
                        ),
                        press=Mock(),
                        release=Mock(),
                        wheel=Mock(),
                    )
                    with (
                        patch.dict(sys.modules, {'mouse': mouse}),
                        patch(
                            'ww_echo_workshop.adapters.okww.scan_input.og',
                            SimpleNamespace(device_manager=manager),
                        ),
                    ):
                        sent = (
                            task._send_scan_click(100, 100)
                            if act == 'click'
                            else task._send_scan_wheel(100, 100, -1)
                        )
                    if change == 'stable':
                        self.assertTrue(sent)
                        if act == 'click':
                            mouse.press.assert_called_once_with('left')
                            mouse.release.assert_called_once_with('left')
                        else:
                            mouse.wheel.assert_called_once_with(-1)
                        capture.pause.assert_not_called()
                    else:
                        self.assertFalse(sent, 'stale hover authorized native input')
                        mouse.press.assert_not_called()
                        mouse.release.assert_not_called()
                        mouse.wheel.assert_not_called()
                        capture.pause.assert_called_once_with(generation=4)
                        self.assertTrue(task._mouse_paused)

    def test_late_foreground_probe_does_not_authorize_a_replaced_scan(self):
        from ww_echo_workshop.capture.intake import CaptureSession
        from ww_echo_workshop.capture.scan.models import BagGrid
        from ww_echo_workshop.capture.scan.traversal import ScanTraversal

        scan = ScanTraversal()
        profile = 'synthetic'
        grid = BagGrid((ScanTile(0, 0, (0, 0, 40, 40), ()),), single_page=True)
        generation = scan.begin(profile, grid, descending=True)
        capture = CaptureSession(SimpleNamespace(profiles=lambda: {profile}), None)
        capture.start_scan(profile, generation)
        task = ScanObservation()
        task._scan, task._generation, task._destroyed = scan, generation, False
        task._identity = 'source'
        task._capture_identity = lambda: 'source'
        task.executor = SimpleNamespace(paused=False, exit_event=threading.Event())
        task._active_runtime = SimpleNamespace(
            capture=capture, _closed=False, stop_event=threading.Event()
        )

        def probe_foreground():
            scan.stop()
            capture.stop()
            capture.start_scan(profile, scan.queue(profile))
            return True

        task._settings = SimpleNamespace(
            config={'InventoryProfileID': profile},
            _capture_ready=lambda: True,
            _game_foreground=probe_foreground,
        )
        with patch(
            'ww_echo_workshop.adapters.okww.scan_observation.foreground_scan_supported',
            return_value=True,
        ):
            self.assertFalse(task._guard('input'), 'old scan accepted a late foreground result')
        self.assertEqual('preparing', scan.status.state)
        self.assertEqual('active', capture.status.state)
        self.assertIsNone(scan.status_for(generation))
        self.assertIsNone(capture.scan_status(generation))
        self.assertFalse(scan.stop(generation=generation))
        capture.pause(generation=generation)
        capture.stop(generation=generation)
        self.assertEqual('preparing', scan.status.state)
        self.assertEqual('active', capture.status.state)

        capture.stop()
        capture.arm_manual(profile)
        capture.stop(generation=generation)
        self.assertEqual('armed', capture.status.state)
