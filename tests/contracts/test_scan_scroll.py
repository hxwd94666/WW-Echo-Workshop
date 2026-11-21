# 回放合成列表的滚动回执、缺口和短页，不触发 Windows 鼠标。
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from ww_echo_workshop.adapters.okww.scan_input import ScanInput
from ww_echo_workshop.adapters.okww.scan_observation import ScanObservation
from ww_echo_workshop.adapters.okww.capture_device import FrameSample
from ww_echo_workshop.capture.scan.grid import BagGridDetector, wheel_hover_point
from ww_echo_workshop.capture.scan.levels import crop_levels, page_content, page_moved
from ww_echo_workshop.capture.scan.scroll_receipt import ScrollEvidence
from ww_echo_workshop.capture.scan.traversal import ScanTraversal


VIEW = (0, 0, 1600, 900)
CONTEXT = ('bag', ('等级顺序',), 'descending')


def pixels(first_row, scroll_y, *, same_rows=False, last_columns=6, top=122):
    frame = np.full((900, 1600, 3), 34, dtype=np.uint8)
    for row in range(3):
        columns = last_columns if row == 2 else 6
        for col in range(columns):
            x, y = 146 + col * 146, top + row * 176
            seed = col if same_rows else (first_row + row) * 29 + col
            channels = np.random.default_rng(seed).integers(30, 195, size=3)
            colour = tuple(int(c) for c in channels)
            cv2.rectangle(frame, (x, y), (x + 124, y + 151), (222, 222, 222), 2)
            cv2.rectangle(frame, (x + 3, y + 3), (x + 121, y + 148), colour, -1)
    cv2.rectangle(frame, (1024, 108), (1030, 761), (6, 6, 6), -1)
    cv2.rectangle(frame, (1024, scroll_y), (1030, scroll_y + 16), (240, 240, 240), -1)
    return frame


def short_page(*colors):
    frame = np.full((900, 1600, 3), 34, dtype=np.uint8)
    for column, color in enumerate(colors):
        x = 146 + column * 146
        cv2.rectangle(frame, (x, 122), (x + 124, 273), (222, 222, 222), 2)
        cv2.rectangle(frame, (x + 3, 125), (x + 121, 270), (color,) * 3, -1)
    return frame


class ScrollReplay(unittest.TestCase):
    def grid(self, detector, frame, token):
        grid = detector.detect(frame, VIEW, context=CONTEXT)
        self.assertIsNotNone(grid)
        return replace(grid, capture_token=token)

    def test_measured_short_page_and_real_traversal_skip_only_overlap(self):
        detector = BagGridDetector()
        old = self.grid(detector, pixels(0, 114), 5)
        tail_frame = pixels(1, 742, last_columns=3, top=130)
        next_page = self.grid(detector, tail_frame, 6)
        self.assertEqual([6, 6, 3], [len(row) for row in next_page.rows()])
        self.assertTrue(next_page.bottom_confirmed)
        grade = crop_levels(tail_frame, next_page.tiles, 1, 1, 6)
        self.assertEqual(15, len(grade.crops))

        evidence = ScrollEvidence(old)
        evidence.input_sent(old.capture_token)
        self.assertIsNone(evidence.observe(next_page))
        self.assertIsNone(evidence.observe(next_page))  # same frame cannot settle
        confirmation = evidence.observe(replace(next_page, capture_token=7))
        self.assertIsNotNone(confirmation)
        self.assertEqual(1, confirmation.shift)
        self.assertTrue(confirmation.has_new_positions)

        scan = ScanTraversal()
        generation = scan.begin('synthetic', old, descending=True)
        for tile in old.tiles:
            self.assertEqual(tile, scan.advance(old).tile)
            selected = replace(old, selected=(tile.row, tile.column))
            self.assertEqual('read', scan.advance(selected).kind)
            scan.complete_detail('failed', generation=generation, position=scan.position)
        self.assertEqual('scroll', scan.advance(old).kind)
        self.assertEqual('scroll', scan.advance(confirmation.destination).kind)
        self.assertEqual(0, scan.page_offset)
        self.assertFalse(scan.accept_scroll(confirmation, generation=generation + 1))
        wrong_context = replace(
            confirmation, destination=replace(confirmation.destination, context=('replacement',))
        )
        self.assertFalse(scan.accept_scroll(wrong_context, generation=generation))
        self.assertEqual(0, scan.page_offset)
        self.assertTrue(scan.accept_scroll(confirmation, generation=generation))
        first_new = scan.advance(confirmation.destination)
        self.assertEqual('click', first_new.kind)
        self.assertEqual((2, 0), (first_new.tile.row, first_new.tile.column))
        self.assertEqual((3, 0), scan.position)

    def test_repeated_or_missing_rows_do_not_acknowledge_displacement(self):
        for repeated, destination in ((True, 1), (False, 3)):
            with self.subTest(repeated=repeated, first=destination):
                detector = BagGridDetector()
                old = self.grid(detector, pixels(0, 114, same_rows=repeated), 10)
                target = self.grid(detector, pixels(destination, 288, same_rows=repeated), 11)
                evidence = ScrollEvidence(old)
                evidence.input_sent(10)
                self.assertIsNone(evidence.observe(target))
                self.assertIsNone(evidence.observe(replace(target, capture_token=12)))
                self.assertTrue(evidence.waiting)
                if not repeated:
                    # The previous pixel-motion heuristic accepted this jump.
                    self.assertTrue(
                        page_moved(
                            page_content(pixels(0, 114), old.tiles),
                            page_content(pixels(destination, 288), old.tiles),
                        )
                    )

    def test_stationary_receipt_allows_retry_without_advancing_page(self):
        detector = BagGridDetector()
        old = self.grid(detector, pixels(0, 114), 1)
        evidence = ScrollEvidence(old)
        evidence.input_sent(1)
        self.assertIsNone(evidence.observe(replace(old, capture_token=2)))
        stationary = evidence.observe(replace(old, capture_token=3))
        self.assertIsNotNone(stationary)
        self.assertFalse(stationary.has_new_positions)
        self.assertEqual(0, stationary.shift)

    def test_scroll_receipt_rejects_rollback_after_a_confirmed_partial_step(self):
        for case, rail, top in (('scrollbar', 186, 116), ('grid', 240, 124)):
            with self.subTest(case=case):
                detector = BagGridDetector()
                origin = self.grid(detector, pixels(0, 114), 1)
                partial = self.grid(detector, pixels(0, 240, top=116), 2)
                jitter = self.grid(detector, pixels(0, 239, top=118), 4)
                rollback = self.grid(detector, pixels(0, rail, top=top), 6)
                forward = self.grid(detector, pixels(0, 285, top=110), 8)
                evidence = ScrollEvidence(origin)
                evidence.input_sent(1)
                self.assertIsNone(evidence.observe(partial))
                first = evidence.observe(replace(partial, capture_token=3))
                self.assertIsNotNone(first)
                self.assertEqual(0, first.shift)
                self.assertFalse(first.has_new_positions)

                evidence.input_sent(3)
                self.assertIsNone(evidence.observe(jitter))
                self.assertIsNotNone(evidence.observe(replace(jitter, capture_token=5)))
                evidence.input_sent(5)
                evidence.require_observation()  # A pause must not reset direction.
                self.assertIsNone(evidence.observe(rollback))
                self.assertIsNone(
                    evidence.observe(replace(rollback, capture_token=7)),
                    'a rollback must not authorize another wheel step',
                )
                self.assertTrue(evidence.waiting)
                self.assertIsNone(evidence.observe(forward))
                resumed = evidence.observe(replace(forward, capture_token=9))
                self.assertIsNotNone(resumed)
                self.assertEqual(0, resumed.shift)

    def test_running_short_page_remeasures_replaced_or_added_cards(self):
        original = short_page(65)
        replaced = short_page(175)
        added = short_page(65, 140)
        for case, baseline, first, second in (
            ('replaced', original, replaced, replaced),
            ('added', original, added, added),
            ('removed', added, original, original),
            ('transient', original, replaced, original),
        ):
            with self.subTest(case=case):
                detector = BagGridDetector()
                initial = self.grid(detector, baseline, 5)
                self.assertEqual(2 if case == 'removed' else 1, len(initial.tiles))
                self.assertTrue(initial.single_page)
                scan = ScanTraversal()
                generation = scan.begin('synthetic', initial, descending=True)
                task = ScanObservation()
                task._scan = scan
                task._generation = generation
                task._active_runtime = SimpleNamespace(capture=SimpleNamespace(clock=lambda: 1.0))
                task._guard = lambda operation='observe': scan.status.state == 'running'
                task._measure = lambda _: nullcontext()
                task._capture_identity = lambda: 'synthetic'
                task._page_ready = True
                task._fixed_grid, task._fixed_frame_shape = initial, baseline.shape[:2]
                task._detector, task._last_token = detector, 5
                task._page_signature = page_content(baseline, initial.tiles)
                task._pending_scroll = None
                with (
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.read_task_sample',
                        side_effect=[FrameSample(first, 6, 1.0), FrameSample(second, 7, 1.2)],
                    ),
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.bag_sort_direction',
                        return_value='descending',
                    ),
                ):
                    self.assertIsNone(task._observe(), 'unconfirmed grid must not authorize input')
                    self.assertEqual('running', scan.status.state)
                    observed = task._observe()
                if case == 'transient':
                    self.assertIsNotNone(observed)
                    self.assertEqual('running', scan.status.state)
                else:
                    self.assertIsNone(observed)
                    self.assertEqual('interrupted', scan.status.state)
                    self.assertIn('短列表内容或位置已变化', scan.status.message)

    def test_running_full_page_checks_scrollbar_even_when_thumbnails_match(self):
        baseline = pixels(0, 114, same_rows=True)
        moved = pixels(1, 290, same_rows=True)
        jitter = pixels(0, 115, same_rows=True)
        missing = baseline.copy()
        cv2.rectangle(missing, (1024, 114), (1030, 130), (6, 6, 6), -1)
        for case, candidate, stopped in (
            ('moved', moved, True),
            ('missing_thumb', missing, False),
            ('one_pixel_jitter', jitter, False),
        ):
            with self.subTest(case=case):
                detector = BagGridDetector()
                initial = self.grid(detector, baseline, 5)
                scan = ScanTraversal()
                generation = scan.begin('synthetic', initial, descending=True)
                task = ScanObservation()
                task._scan, task._generation = scan, generation
                task._active_runtime = SimpleNamespace(capture=SimpleNamespace(clock=lambda: 1.0))
                task._guard = lambda operation='observe': scan.status.state == 'running'
                task._measure = lambda _: nullcontext()
                task._capture_identity = lambda: 'synthetic'
                task._page_ready = True
                task._fixed_grid, task._fixed_frame_shape = initial, baseline.shape[:2]
                task._detector, task._last_token = detector, 5
                task._page_signature = page_content(baseline, initial.tiles)
                task._pending_scroll = None
                self.assertFalse(
                    page_moved(task._page_signature, page_content(candidate, initial.tiles))
                )
                with (
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.read_task_sample',
                        side_effect=[FrameSample(candidate, 6, 1.0), FrameSample(baseline, 7, 1.2)],
                    ),
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.bag_sort_direction',
                        return_value='descending',
                    ),
                ):
                    observed = task._observe()
                    if case == 'one_pixel_jitter':
                        self.assertIsNotNone(observed)
                    else:
                        self.assertTrue(
                            observed is None, 'unverified scroll position authorized input'
                        )
                    self.assertEqual('interrupted' if stopped else 'running', scan.status.state)
                    if case == 'missing_thumb':
                        self.assertIsNotNone(task._observe())

    def test_locked_grid_rejects_new_content_bounds_at_same_capture_size(self):
        from ww_echo_workshop.capture.viewport import detect_echo_viewport

        baseline = pixels(0, 114)
        letterboxed = baseline.copy()
        letterboxed[:, :8] = 0
        self.assertEqual(baseline.shape, letterboxed.shape)
        self.assertEqual((8, 0, 1592, 900), detect_echo_viewport(letterboxed))
        for phase in ('running', 'scrolling', 'resuming', 'blank_then_original'):
            with self.subTest(phase=phase):
                detector = BagGridDetector()
                initial = self.grid(detector, baseline, 5)
                scan = ScanTraversal()
                generation = scan.begin('synthetic', initial, descending=True)
                if phase == 'resuming':
                    scan.pause()
                task = ScanObservation()
                task._scan, task._generation = scan, generation
                task._active_runtime = SimpleNamespace(capture=SimpleNamespace(clock=lambda: 1.0))
                task._guard = lambda operation='observe': scan.status.state in ('running', 'paused')
                task._measure = lambda _: nullcontext()
                task._capture_identity = lambda: 'synthetic'
                task._page_ready = True
                task._fixed_grid, task._fixed_frame_shape = initial, baseline.shape[:2]
                task._detector, task._last_token = detector, 5
                task._page_signature = page_content(baseline, initial.tiles)
                task._pending_scroll = ScrollEvidence(initial) if phase == 'scrolling' else None
                first = np.zeros_like(baseline) if phase == 'blank_then_original' else letterboxed
                with (
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.read_task_sample',
                        side_effect=[FrameSample(first, 6, 1.0), FrameSample(baseline, 7, 1.2)],
                    ),
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.bag_sort_direction',
                        return_value='descending',
                    ),
                ):
                    operation = 'resume' if phase == 'resuming' else 'observe'
                    observed = task._observe(operation=operation)
                    self.assertTrue(observed is None, 'changed viewport reused cached coordinates')
                    if phase == 'blank_then_original':
                        self.assertEqual('running', scan.status.state)
                        self.assertIsNotNone(task._observe())
                    else:
                        self.assertEqual('interrupted', scan.status.state)
                        self.assertIn('画面边界已变化', scan.status.message)

    def test_stop_cancels_preflight_between_ocr_regions_and_inside_compute(self):
        from threading import Event

        from ww_echo_workshop.capture.panel_reader import PanelReader
        from ww_echo_workshop.core.cancellation import OperationCancelled, checkpoint

        frame = pixels(0, 114)
        for phase in ('ocr', 'compute'):
            for replace_run in (False, True):
                with self.subTest(phase=phase, replace_run=replace_run):
                    scan = ScanTraversal()
                    weights = SimpleNamespace(version='synthetic')
                    old = scan.queue('synthetic', weights=weights)
                    runtime = SimpleNamespace(
                        capture=SimpleNamespace(clock=lambda: 1.0),
                        observations=object(),
                        stop_event=Event(),
                    )
                    task = ScanObservation()
                    task._active_runtime, task._scan, task._generation = runtime, scan, old
                    task._settings = SimpleNamespace()
                    task._guard = lambda operation='observe': scan.status_for(old) is not None
                    task._measure = lambda _: nullcontext()
                    task._capture_identity = lambda: 'synthetic'
                    task._frozen_settings, task._last_token = {}, None
                    regions = []

                    def stop_old():
                        scan.stop(generation=old)
                        if replace_run:
                            scan.queue('next', weights=weights)

                    def read(_frame, region, _name):
                        regions.append(region)
                        if phase == 'ocr':
                            if len(regions) > 1:
                                self.fail('cancelled preflight entered another OCR region')
                            stop_old()
                        return []

                    def analyze(*_args, cancel_event, **_kwargs):
                        self.assertEqual('compute', phase)
                        self.assertFalse(cancel_event.is_set())
                        stop_old()
                        checkpoint(cancel_event)
                        self.fail('cancelled preflight still permitted native compute')

                    task._scan_parser = PanelReader(read)
                    with (
                        patch(
                            'ww_echo_workshop.adapters.okww.scan_observation.read_task_sample',
                            return_value=FrameSample(frame, 1, 1.0),
                        ),
                        patch(
                            'ww_echo_workshop.capture.panel_reader.analyze_echo_stats',
                            side_effect=analyze,
                        ),
                    ):
                        with self.assertRaises(OperationCancelled):
                            task._observe()
                    self.assertFalse(runtime.stop_event.is_set())
                    self.assertEqual(
                        'preparing' if replace_run else 'interrupted', scan.status.state
                    )
                    self.assertEqual(1 if phase == 'ocr' else 2, len(regions))

    def test_sort_preflight_waits_for_new_grid_before_any_item_click(self):
        from ww_echo_workshop.capture.scan.models import BagGrid, ScanTile

        class Adapter(ScanInput):
            def _observe(self):
                return next(self.frames, None)

        initial_tile = ScanTile(0, 0, (146, 122, 124, 151), ())
        updated_tile = ScanTile(0, 0, (189, 152, 124, 151), ())
        old_grid = BagGrid((initial_tile,), viewport=VIEW, single_page=True)
        new_grid = replace(old_grid, tiles=(updated_tile,), selected=(0, 0))
        scrolled = replace(old_grid, single_page=False, scrollbar=0.4)
        frame = short_page(65)
        controls = ((280, 824), (290, 612))

        def view(token, label, grid, *, open_menu=False):
            boxes = [SimpleNamespace(name=label, x=230, y=812, width=100, height=24)]
            if open_menu:
                boxes.append(SimpleNamespace(name='等级顺序', x=240, y=600, width=100, height=24))
            return frame, token, float(token), VIEW, SimpleNamespace(original_boxes=boxes), grid

        quality = view(1, '品质顺序', old_grid)
        menu = view(2, '品质顺序', None, open_menu=True)
        sorted_open = view(3, '等级顺序', new_grid, open_menu=True)
        label_lags = view(3, '品质顺序', new_grid, open_menu=True)
        sorted_closed = view(4, '等级顺序', new_grid)
        for label, frames, clicks, expected in (
            (
                'menu_stays_open',
                (quality, menu, sorted_open, sorted_closed),
                (*controls, updated_tile.center),
                sorted_closed,
            ),
            ('menu_closes', (quality, menu, sorted_closed), controls, sorted_closed),
            (
                'label_lags',
                (quality, menu, label_lags, sorted_closed),
                (*controls, updated_tile.center),
                sorted_closed,
            ),
            ('no_receipt', (quality, menu), controls, None),
            ('already_scrolled', (view(1, '品质顺序', scrolled), menu), (), None),
            ('moved_after_sort', (quality, menu, view(3, '等级顺序', scrolled)), controls, None),
        ):
            with self.subTest(label=label):
                task = Adapter()
                task.frames = iter(frames)
                task.log_info = Mock()
                task.sleep = Mock()
                task._scan = SimpleNamespace(
                    status=SimpleNamespace(state='preparing'),
                    profile_id='synthetic',
                    weights=None,
                    sonata=None,
                )
                task._scan_parser = SimpleNamespace(read_record=Mock(return_value=object()))
                task._click_checked = Mock(return_value=True)
                with patch(
                    'ww_echo_workshop.adapters.okww.scan_input.bag_sort_direction',
                    return_value='descending',
                ):
                    actual = task._prepare_sort()
                self.assertEqual(
                    clicks, tuple(call.args[0] for call in task._click_checked.call_args_list)
                )
                self.assertIs(expected, actual)

    def test_resume_waits_for_new_frame_and_rejects_changed_list(self):
        class Adapter(ScanInput, ScanObservation):
            pass

        for case in ('missing', 'stale', 'same', 'scrolled', 'replaced', 'pending_scroll'):
            with self.subTest(case=case):
                detector = BagGridDetector()
                frame = pixels(0, 114)
                old = self.grid(detector, frame, 5)
                scan = ScanTraversal()
                generation = scan.begin('synthetic', old, descending=True)
                scan.pause()
                scan.resume_requested = True
                capture = SimpleNamespace(clock=lambda: 1.0, resume=Mock())
                task = Adapter()
                task._scan, task._generation = scan, generation
                task._active_runtime = SimpleNamespace(capture=capture)
                task._guard = lambda operation='observe': (
                    operation == 'resume' and scan.status.state == 'paused'
                )
                task._measure = lambda _: nullcontext()
                task._capture_identity = lambda: 'synthetic'
                task._page_ready = True
                task._fixed_grid, task._fixed_frame_shape = old, frame.shape[:2]
                task._detector, task._last_token = detector, 5
                task._page_signature = page_content(frame, old.tiles)
                task._pending_scroll = ScrollEvidence(old) if case == 'pending_scroll' else None
                if task._pending_scroll is not None:
                    task._pending_scroll.input_sent(5)
                next_frame = {
                    'stale': frame,
                    'same': frame,
                    'scrolled': pixels(0, 300),
                    'replaced': pixels(3, 114),
                    'pending_scroll': pixels(1, 340),
                }.get(case)
                token = 5 if case == 'stale' else 6
                sample = None if next_frame is None else FrameSample(next_frame, token, 1.0)
                with (
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.read_task_sample',
                        return_value=sample,
                    ) as acquire,
                    patch(
                        'ww_echo_workshop.adapters.okww.scan_observation.bag_sort_direction',
                        return_value='descending',
                    ),
                ):
                    task._resume_page_if_requested()
                acquire.assert_called_once()
                if case in ('same', 'pending_scroll'):
                    self.assertEqual('running', scan.status.state)
                    capture.resume.assert_called_once()
                    self.assertIs(old, task._fixed_grid)
                    if case == 'pending_scroll':
                        self.assertTrue(task._pending_scroll.waiting)
                        self.assertEqual(1, task._pending_scroll.steps)
                else:
                    capture.resume.assert_not_called()
                    self.assertEqual(
                        'paused' if case in ('missing', 'stale') else 'interrupted',
                        scan.status.state,
                    )

    def _scroll_adapter(self, initial):
        class Adapter(ScanInput):
            def _observe(self):
                return self.latest

        task = Adapter()
        task._fixed_grid = initial
        task._scroll_batches = task._wheel_steps = 0
        task._pending_scroll = None
        task._generation = 7
        task._page = 0
        task._submit_page_levels = Mock()
        task.log_info = Mock()
        task._scan_job = Mock()
        task._scan = SimpleNamespace(
            status=SimpleNamespace(state='running'), pause=Mock(), accept_scroll=Mock()
        )
        task._active_runtime = SimpleNamespace(capture=SimpleNamespace(pause=Mock()))
        task._guard = lambda: True
        task._measure = lambda _: nullcontext()
        task._send_scan_wheel = Mock(return_value=True)
        task.sleep = Mock()
        return task

    def test_scroll_dispatch_recomputes_hover_after_partial_step(self):
        detector = BagGridDetector()
        initial_frame = pixels(0, 114)
        partial_frame = pixels(0, 240, top=112)
        next_frame = pixels(1, 334, top=130)
        initial = self.grid(detector, initial_frame, 1)
        partial = self.grid(detector, partial_frame, 2)
        following = self.grid(detector, next_frame, 4)
        task = self._scroll_adapter(initial)
        task._observe = Mock(
            side_effect=[
                (partial_frame, 2, 1.2, VIEW, None, partial),
                (partial_frame, 3, 1.4, VIEW, None, replace(partial, capture_token=3)),
                (next_frame, 4, 1.6, VIEW, None, following),
                (next_frame, 5, 1.8, VIEW, None, replace(following, capture_token=5)),
            ]
        )
        task._scroll_batch((initial_frame, 1, 1.0, VIEW, None, initial))
        calls = [call.args for call in task._send_scan_wheel.call_args_list]
        self.assertEqual(2, len(calls))
        self.assertNotEqual(calls[0][:2], calls[1][:2], 'second wheel used the stale hover point')
        self.assertEqual([-1, -1], [args[-1] for args in calls])
        for (x, y, _), grid in zip(calls, (initial, partial)):
            left, right = grid.rows()[0][:2]
            self.assertGreater(x, left.bounds[0] + left.bounds[2])
            self.assertLess(x, right.bounds[0])
            self.assertTrue(left.bounds[1] < y < left.bounds[1] + left.bounds[3])
        self.assertIsNone(task._pending_scroll)

    def test_scroll_dispatch_pauses_when_no_measured_gap(self):
        frame = short_page(65)
        grid = self.grid(BagGridDetector(), frame, 1)
        grid = replace(grid, scrollbar=0.4, single_page=False)
        task = self._scroll_adapter(grid)
        task._observe = Mock(return_value=None)
        task._scroll_batch((frame, 1, 1.0, VIEW, None, grid))
        task._send_scan_wheel.assert_not_called()
        task._scan.pause.assert_called_once()
        self.assertEqual(0, task._pending_scroll.steps)

    def test_wheel_hover_can_use_a_measured_vertical_gap(self):
        from ww_echo_workshop.capture.scan.models import BagGrid, ScanTile

        upper = ScanTile(0, 0, (145, 122, 124, 151), ())
        lower = ScanTile(1, 0, (147, 298, 124, 151), ())
        grid = BagGrid((upper, lower), viewport=VIEW)
        self.assertEqual((208, 285), wheel_hover_point(grid))
        crowded = replace(grid, tiles=(upper, replace(lower, bounds=(147, 279, 124, 151))))
        self.assertIsNone(wheel_hover_point(crowded))

    def test_production_scroll_dispatch_holds_after_ambiguous_frame(self):
        detector = BagGridDetector()
        initial_frame, jumped_frame = pixels(0, 114), pixels(3, 400)
        initial = self.grid(detector, initial_frame, 10)
        jumped = self.grid(detector, jumped_frame, 11)
        current = (initial_frame, 10, 0.0, VIEW, None, initial)
        task = self._scroll_adapter(initial)
        task.latest = (jumped_frame, 11, 1.0, VIEW, None, jumped)
        task._scroll_batch(current)
        task._scan.pause.assert_called_once()
        task._scan.accept_scroll.assert_not_called()
        task._send_scan_wheel.assert_called_once()
        self.assertEqual(1, task._pending_scroll.steps)
        # Retrying observation after resume must not send another notch.
        task._scroll_batch()
        task._send_scan_wheel.assert_called_once()
        task._scan.accept_scroll.assert_not_called()
        # The same pending notch can settle; only now publish new coordinates
        # and queue the next page's grades.
        settled_frame = pixels(1, 330)
        settled = self.grid(detector, settled_frame, 12)
        task._observe = Mock(
            side_effect=[
                (settled_frame, 12, 1.2, VIEW, None, settled),
                (settled_frame, 13, 1.4, VIEW, None, replace(settled, capture_token=13)),
            ]
        )
        task._scan.accept_scroll.return_value = True
        task._scroll_batch()
        task._send_scan_wheel.assert_called_once()
        receipt = task._scan.accept_scroll.call_args.args[0]
        self.assertEqual((1, 13), (receipt.shift, receipt.destination.capture_token))
        self.assertEqual(1, task._page)
        self.assertIsNone(task._pending_scroll)
        task._submit_page_levels.assert_called_once()
