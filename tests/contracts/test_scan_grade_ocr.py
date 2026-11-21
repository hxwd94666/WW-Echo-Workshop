# 验证扫描等级使用独立 OCR，并将宿主框和可变配置留在转换边界。
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from weakref import ref

try:
    import numpy as np
    from ok import Box
    from ww_echo_workshop.adapters.okww.panel_ocr import frame_reader
    from ww_echo_workshop.adapters.okww.scan_observation import ScanObservation
    from ww_echo_workshop.adapters.okww.private_ocr import PluginOcrService
except ImportError:
    ScanObservation = None


@unittest.skipIf(ScanObservation is None, '当前解释器没有宿主依赖')
class ScanGradeOcrContract(unittest.TestCase):
    def test_cutoff_grade_requires_stable_selected_detail_and_rejects_late_result(self):
        from ww_echo_workshop.capture.scan.levels import FixedFrameGate
        from ww_echo_workshop.capture.scan.models import BagGrid, ScanTile
        from ww_echo_workshop.capture.scan.traversal import ScanTraversal

        state = ScanObservation()
        state._scan = ScanTraversal()
        grid = BagGrid((ScanTile(0, 0, (80, 80, 80, 90), ()),), single_page=True)
        generation = state._scan.begin('synthetic', grid, minimum_level=5, descending=True)
        self.assertEqual('click', state._scan.advance(grid).kind)
        selected = BagGrid(grid.tiles, selected=(0, 0), single_page=True)
        self.assertEqual('read', state._scan.advance(selected).kind)
        state._generation, state._page = generation, 0
        state._page_levels, state._grade_tokens = [0], [18]
        state._grade_fallback = None
        state._grade_pipeline = SimpleNamespace(submit=Mock(return_value=True), poll=Mock())
        gate = FixedFrameGate()
        gate.arm((0, 0), generation, 20, 10.0)
        viewport = (0, 0, 1280, 720)
        original = np.zeros((720, 1280, 3), np.uint8)
        original[100:112, 890:991] = 230
        changed = original.copy()
        changed[139:157, 900:930] = 230

        def grade(frame, token):
            return state._read_selected_level(
                (frame, token, token * 0.6, viewport, None, None), gate, 0, (0, 0)
            )

        def deliver(value):
            job = state._grade_pipeline.submit.call_args.args[0]
            self.assertEqual(0, job.selected)
            state._grade_pipeline.poll.side_effect = [(job, (value,), None), None]
            state._consume_levels()

        self.assertEqual((False, None), grade(original, 21))
        state._grade_pipeline.submit.assert_not_called()
        self.assertEqual((False, None), grade(original, 22))
        state._grade_pipeline.submit.assert_called_once()
        deliver(0)
        self.assertEqual((False, None), grade(changed, 23))
        self.assertEqual([None], state._page_levels)
        self.assertEqual((False, None), grade(changed, 24))
        self.assertEqual((False, None), grade(changed, 25))
        self.assertEqual(2, state._grade_pipeline.submit.call_count)
        deliver(3)
        self.assertEqual((True, 3), grade(changed, 26))
        args = (changed, viewport)
        grade_token = state._grade_tokens[0]
        self.assertIsNone(
            gate.observe(*args, 26, 15.6, (0, 0), generation, 3, grade_token, page_offset=0)
        )
        proof = gate.observe(*args, 27, 16.2, (0, 0), generation, 3, grade_token, page_offset=0)
        self.assertIsNotNone(proof)
        self.assertEqual(
            'threshold',
            state._scan.accept_detail(proof.level, 'valid', generation=generation, position=(0, 0)),
        )
        self.assertEqual(
            ('finished', 1, 0),
            (state._scan.status.state, state._scan.status.skipped, state._scan.status.backlog),
        )

    def test_cutoff_grade_discards_snapshot_changed_during_capture_copy(self):
        from ww_echo_workshop.capture.scan.levels import FixedFrameGate

        state = ScanObservation()
        state._scan = SimpleNamespace(minimum_level=5)
        state._generation, state._page = 4, 0
        state._page_levels, state._grade_tokens = [0], [18]
        state._grade_fallback = None
        state._grade_pipeline = SimpleNamespace(submit=Mock(return_value=True))
        gate = FixedFrameGate()
        gate.arm((0, 0), state._generation, 20, 10.0)
        viewport = (0, 0, 1280, 720)
        frame = np.zeros((720, 1280, 3), np.uint8)
        grade = frame[139:157, 900:930]
        grade[:, :8] = 230

        def observe(token):
            return state._read_selected_level(
                (frame, token, token * 0.6, viewport, None, None), gate, 0, (0, 0)
            )

        self.assertEqual((False, None), observe(21))
        original_array = np.array

        def copy_with_reused_buffer(pixels, *args, **kwargs):
            if not np.shares_memory(pixels, frame):
                return original_array(pixels, *args, **kwargs)
            # A different grade appears only while copying the host pixels.
            # Checking the borrowed source after copying would miss it.
            grade[:, 15:30] = 230
            try:
                return original_array(pixels, *args, **kwargs)
            finally:
                grade[:, 15:30] = 0

        with patch.object(np, 'array', side_effect=copy_with_reused_buffer):
            self.assertEqual((False, None), observe(22))
        self.assertEqual(0, state._grade_pipeline.submit.call_count, 'changed crop reached OCR')
        self.assertIsNone(state._grade_fallback)
        self.assertEqual((False, None), observe(23))
        state._grade_pipeline.submit.assert_not_called()
        self.assertEqual((False, None), observe(24))
        state._grade_pipeline.submit.assert_called_once()
        job = state._grade_pipeline.submit.call_args.args[0]
        (crop,) = job.crops
        self.assertEqual((4, 0, 24, 0), (job.generation, job.page, job.token, job.selected))
        self.assertFalse(crop.flags.writeable)
        self.assertFalse(np.shares_memory(crop, frame))
        self.assertEqual(18 * 8, np.count_nonzero(crop[:, :, 0]))

    def test_grade_reader_uses_its_own_service_without_live_task(self):
        state = ScanObservation()
        state.executor = SimpleNamespace(config={}, text_fix={})
        state.ocr_default_threshold = 0.2
        state._frozen_settings = {'使用独立OCR引擎': True}
        grade = Mock()
        grade.infer.return_value = ([Box(2, 3, 10, 6, 0.91, '+5')], [])
        with (
            patch(
                'ww_echo_workshop.adapters.okww.private_ocr.PluginOcrService', return_value=grade
            ),
            patch('ww_echo_workshop.adapters.okww.panel_ocr.PLUGIN_OCR.infer') as live_engine,
        ):
            read = state._make_grade_reader()
            (result,) = read(np.zeros((20, 30, 3), np.uint8))
            self.assertEqual('+5', result.name)
            self.assertNotIsInstance(result, Box)
            grade.infer.assert_called_once()
            live_engine.assert_not_called()
            read.close()
            grade.close.assert_called_once()

    def test_disabled_ocr_does_not_touch_executor_or_engine(self):
        service = Mock()
        read = frame_reader(None, threshold=0.2, enabled=False, service=service)
        self.assertEqual([], read(None, (0, 0, 20, 20), 'echo-level'))
        service.infer.assert_not_called()

    def test_frame_reader_retains_only_frozen_text_rules_and_returns_pixel_coordinates(self):
        class Executor:
            config = {}
            text_fix = {'合成': '旧修正'}

        executor = Executor()
        executor_ref = ref(executor)
        engine = Mock()
        engine.ocr.return_value = [[([[2, 3], [12, 3], [12, 9], [2, 9]], (' 合成 ', 0.91))]]
        service = PluginOcrService(engine_factory=lambda: engine)
        read = frame_reader(executor, threshold=0.2, service=service)
        executor.text_fix = {'合成': '新修正'}
        del executor
        self.assertIsNone(executor_ref())
        (result,) = read(np.zeros((80, 100, 3), np.uint8), (10, 20, 40, 30), 'echo-level')
        self.assertNotIsInstance(result, Box)
        self.assertEqual(
            ('旧修正', 12, 23, 10, 6, 0.91),
            (result.name, result.x, result.y, result.width, result.height, result.confidence),
        )
        service.close()
