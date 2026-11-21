# 验证实时评分稳定键会忽略一帧 OCR 抖动但保留真实词条变化。
import unittest
from types import SimpleNamespace
from dataclasses import replace

from ww_echo_workshop.capture.live_observation import LiveObservationState, analysis_key
from ww_echo_workshop.capture.stat_observation import EchoStatAnalysis, RecognizedStatRow
from ww_echo_workshop.capture.viewport import EchoViewport


def analysis(score, *subs):
    return SimpleNamespace(
        layout='display',
        current_score=score,
        main_rows=(SimpleNamespace(stat_name='暴击', value=22.0),),
        sub_rows=tuple(SimpleNamespace(stat_name=name, value=value) for name, value in subs),
    )


class LiveAnalysisStabilityContract(unittest.TestCase):
    def _scene(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('需要宿主图像依赖来回放帧')

        state = LiveObservationState()
        frame = np.full((720, 1280, 3), 32, dtype=np.uint8)
        view = EchoViewport.checked(None, 1280, 720)
        main = RecognizedStatRow(980, 175, 246, 19, '暴击', 22.0, '22.0%', 1170, 175)
        sub = replace(main, y=300, tier_y=300, stat_name='暴击伤害', value=13.8, value_text='13.8%')
        settled = EchoStatAnalysis(
            (),
            (),
            '',
            main_rows=(main,),
            sub_rows=(sub,),
            layout='display',
            cost=4,
            observed_cost=4,
            current_score=26.83,
        )

        def observe(pixels, now, value, context='live'):
            return state.observe(
                pixels,
                view,
                context=context,
                now=now,
                read_regions=lambda regions: (),
                analyze=lambda *args: value,
            )

        return frame, settled, observe

    def test_same_rows_produce_same_key(self):
        left = analysis(26.83, ('暴击伤害', 13.8), ('暴击', 9.3))
        right = analysis(26.83, ('暴击伤害', 13.8), ('暴击', 9.3))
        self.assertEqual(analysis_key(left), analysis_key(right))

    def test_score_flicker_does_not_change_key(self):
        left = analysis(26.83, ('暴击伤害', 13.8))
        right = analysis(21.70, ('暴击伤害', 13.8))
        self.assertEqual(analysis_key(left), analysis_key(right))

    def test_real_row_change_changes_key(self):
        left = analysis(26.83, ('暴击伤害', 13.8))
        right = analysis(26.83, ('暴击伤害', 14.0))
        self.assertNotEqual(analysis_key(left), analysis_key(right))

    def test_constant_glyphs_never_publish_conflicting_reads_from_full_refresh(self):
        pixels, stable, observe = self._scene()
        pixels[300:307, 1100:1104] = 240
        misread = replace(
            stable, sub_rows=(replace(stable.sub_rows[0], value=18.8),), current_score=31
        )
        self.assertEqual(stable, observe(pixels, 0.0, stable).analysis)
        for instant in (2.1, 4.2, 6.3, 8.4):
            with self.subTest(instant=instant):
                self.assertEqual(stable, observe(pixels, instant, misread).analysis)

    def test_changed_stat_waits_for_consecutive_matching_reads_at_trigger_cadence(self):
        pixels, stable, observe = self._scene()
        pixels[300:307, 1100:1104] = 240
        other = pixels.copy()
        other[300:307, 1105:1108] = 240
        sub = stable.sub_rows[0]
        change = replace(stable, sub_rows=(replace(sub, value=15.0),), current_score=28.5)
        wrong = replace(stable, sub_rows=(replace(sub, value=20.0),), current_score=35.2)
        self.assertEqual(stable, observe(pixels, 0.0, stable).analysis)
        self.assertIsNone(observe(other, 0.2, wrong))
        self.assertIsNone(observe(other, 0.35, change))
        self.assertIsNone(observe(other, 0.40, change))  # Too soon to confirm.
        self.assertEqual(change, observe(other, 0.50, change).analysis)
        self.assertEqual(wrong, observe(other, 0.55, wrong, context='new-source').analysis)

    def test_missing_reads_are_held_briefly_but_clear_when_the_page_is_gone(self):
        pixels, stable, observe = self._scene()
        pixels[300:307, 1100:1104] = 240
        blank = pixels.copy()
        blank[300:307, 1100:1104] = 0
        missing = EchoStatAnalysis((), (), '')
        self.assertEqual(stable, observe(pixels, 0.0, stable).analysis)
        self.assertIsNone(observe(blank, 0.20, missing))
        self.assertIsNone(observe(blank, 0.35, missing))
        self.assertEqual(missing, observe(blank, 0.70, missing).analysis)
        self.assertEqual(stable, observe(pixels, 0.75, stable).analysis)

    def test_settled_score_keeps_current_frame_coordinates_and_drops_expired_grade(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('需要宿主图像依赖来回放帧')
        state = LiveObservationState()
        frame = np.full((720, 1280, 3), 32, dtype=np.uint8)
        view = EchoViewport.checked(None, 1280, 720)
        row = RecognizedStatRow(980, 175, 246, 19, '暴击', 22.0, '22.0%', 1170, 175)
        first = EchoStatAnalysis(
            (row.rectangle((255, 0, 0)),),
            (26.83,),
            '旧数字',
            main_rows=(row,),
            layout='display',
            cost=4,
            observed_cost=4,
            current_score=26.83,
            score_text='26.83 · A',
        )
        moved = replace(row, y=224, tier_y=224)
        second = replace(
            first,
            main_rows=(moved,),
            rectangles=(moved.rectangle((255, 0, 0)),),
            current_score=26.75,
            score_text='26.75 · A',
        )
        jitter = replace(moved, y=226, tier_y=226)
        third = replace(
            second,
            main_rows=(jitter,),
            rectangles=(jitter.rectangle((255, 0, 0)),),
            observed_cost=None,
            score_text='26.75',
            summary='参考评分',
        )
        for instant, analysis in ((0.0, first), (2.1, second), (4.2, third), (4.7, third)):
            result = state.observe(
                frame,
                view,
                context='same-source',
                now=instant,
                read_regions=lambda regions: (),
                analyze=lambda *args: analysis,
            ).analysis
            if instant == 2.1:
                self.assertEqual(26.83, result.current_score)
                self.assertEqual((224, 224), (result.main_rows[0].y, result.rectangles[0].tier_y))
            elif instant == 4.2:
                self.assertEqual(4, result.retained_cost)
                self.assertIsNone(result.observed_cost)
                self.assertEqual((226, 226), (result.main_rows[0].y, result.rectangles[0].tier_y))
                self.assertEqual('26.83 · A', result.score_text)
            elif instant == 4.7:
                self.assertIsNone(result.observed_cost)
                self.assertIsNone(result.retained_cost)
                self.assertEqual('26.75', result.score_text)

    def test_cost_reappearing_on_identical_frame_restores_current_score(self):
        from ww_echo_workshop.adapters.okww.live_presentation import live_summary

        pixels, settled, observe = self._scene()
        settled = replace(settled, score_text='26.83 · A', rating_color=(113, 187, 255))
        missing = replace(
            settled,
            observed_cost=None,
            cost=1,
            score_text='25.00',
            current_score=25.0,
            rating_color=None,
        )
        pixels[100:108, 1100:1104] = 235
        self.assertEqual(4, observe(pixels, 0.0, settled).analysis.observed_cost)
        retained = observe(pixels, 2.1, missing).analysis
        self.assertEqual(
            (4, 4, None), (retained.cost, retained.retained_cost, retained.observed_cost)
        )
        self.assertIn('当前评分 26.83 · A', live_summary(retained)[0])
        self.assertEqual(4, observe(pixels, 2.25, missing).analysis.retained_cost)
        expired = observe(pixels, 2.61, missing).analysis
        self.assertIn('参考评分 25.00', live_summary(expired)[0])
        restored = observe(pixels, 4.2, settled).analysis
        self.assertEqual((4, None), (restored.observed_cost, restored.retained_cost))
        self.assertIn('当前评分 26.83 · A', live_summary(restored)[0])

    def test_cost_is_not_inherited_if_only_the_title_pixels_changed(self):
        pixels, settled, observe = self._scene()
        new_title = pixels.copy()
        new_title[100:104, 900:904] = 95
        self.assertEqual(4, observe(pixels, 0.0, settled).analysis.observed_cost)
        current = observe(new_title, 2.1, replace(settled, observed_cost=None)).analysis
        self.assertIsNone(current.observed_cost)
        self.assertIsNone(current.retained_cost)

    def test_conflicting_cost_drops_grade_until_another_matching_read(self):
        pixels, stable, observe = self._scene()
        alternate = replace(
            stable,
            cost=3,
            observed_cost=3,
            score_text='18.75 · C',
            current_score=18.75,
            rating_color=(182, 186, 194),
        )
        self.assertEqual(4, observe(pixels, 0.0, stable).analysis.observed_cost)
        first = observe(pixels, 2.1, alternate).analysis
        self.assertIsNone(first.observed_cost)
        self.assertIsNone(first.retained_cost)
        self.assertEqual('18.75', first.score_text)
        self.assertIsNone(observe(pixels, 2.2, alternate).analysis.observed_cost)
        self.assertEqual(3, observe(pixels, 2.25, alternate).analysis.observed_cost)

    def test_live_cost_retry_reads_only_the_selected_header_before_scoring(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('需要宿主图像依赖来回放帧')
        from ww_echo_workshop.capture.panel_reader import OcrTextBox, PanelReader
        from ww_echo_workshop.resources.weights import WeightSnapshot

        frame = np.full((900, 1600, 3), 32, np.uint8)
        initial = [
            OcrTextBox(x, y, 55, 26, label, 0.99)
            for x, y, label in (
                (850, 220, '暴击'),
                (1100, 220, '22.0%'),
                (850, 258, '攻击'),
                (1100, 258, '150'),
                (850, 390, '声骸技能'),
                (1260, 220, '暴击'),
                (1480, 220, '22.0%'),
                (1260, 258, '攻击'),
                (1480, 258, '150'),
                (1260, 390, '声骸技能'),
            )
        ]
        costs = [
            OcrTextBox(850, 165, 60, 22, 'COST 3', 0.99),
            OcrTextBox(1260, 165, 60, 22, 'COST 4', 0.99),
        ]
        reads = []
        evaluations = []

        def read_region(pixels, rect, stage):
            self.assertIs(frame, pixels)
            reads.append((rect, stage))
            return costs  # Deliberately include the opposite column to check clipping.

        def evaluate(main, sub, **kwargs):
            inferred = kwargs['cost']
            evaluations.append(inferred)
            score = SimpleNamespace(
                current_score=17.73, potential_score=100, row_scores=(), rating=None
            )
            return SimpleNamespace(cost=inferred or 1, score_for=lambda _: score, tiers=())

        parser = PanelReader(read_region)
        kwargs = dict(
            viewport=(0, 0, 1600, 900),
            boxes=initial,
            fast=True,
            observation_service=SimpleNamespace(evaluate=evaluate),
        )
        settings = {'TemplateCharacterID': '1304', 'TemplateModalID': 'default'}
        snapshot = WeightSnapshot.builtin()
        for side, cost in (('right', 4), ('left', 3)):
            result = parser.read_metadata(
                frame, settings, snapshot, comparison_side=side, retry_live_cost=True, **kwargs
            )
            self.assertEqual(cost, result.analysis.observed_cost)
        self.assertEqual([4, 3], evaluations)
        self.assertEqual(
            # The parsed first-row box starts three pixels above the OCR text.
            [((1240, 72, 344, 145), 'echo-live-cost'), ((832, 72, 408, 145), 'echo-live-cost')],
            reads,
        )
        result = parser.read_metadata(frame, settings, snapshot, **kwargs)
        self.assertIsNone(result.analysis.observed_cost)
        self.assertEqual(2, len(reads))
        kwargs['boxes'] = [*initial, *costs]
        result = parser.read_metadata(frame, settings, snapshot, retry_live_cost=True, **kwargs)
        self.assertEqual(4, result.analysis.observed_cost)
        self.assertEqual(2, len(reads))  # A successful first read never triggers OCR again.

    def test_ultrawide_display_crop_is_right_anchored_and_keeps_lower_detail_rows(self):
        view = EchoViewport.checked((50, 12, 1920, 720), 2020, 744)
        ((x, y, w, h),) = view.live_regions('display')
        self.assertEqual(690, view.offset('right')[0])
        self.assertEqual(690 + round(0.74 * 1280), x)
        self.assertGreaterEqual(x + w, 690 + round(0.985 * 1280))
        self.assertGreaterEqual(y + h, 12 + round(0.90 * 720))

    def test_right_detail_fingerprint_ignores_model_motion_but_detects_small_glyph(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('需要宿主图像依赖来回放帧')
        state = LiveObservationState()
        view = EchoViewport.checked(None, 1280, 720)
        frame = np.full((720, 1280, 3), 32, dtype=np.uint8)
        model_moved = frame.copy()
        model_moved[310:335, 623:653] = 220
        small_glyph = model_moved.copy()
        small_glyph[173:181, 1100:1105] = 240
        self.assertFalse(np.array_equal(frame[::32, ::32], model_moved[::32, ::32]))
        np.testing.assert_array_equal(model_moved[::32, ::32], small_glyph[::32, ::32])
        row = RecognizedStatRow(980, 175, 246, 19, '暴击', 22.0, '22.0%', 1170, 175)
        result = EchoStatAnalysis((), (), '', main_rows=(row,), layout='display')
        reads = []

        def read_regions(regions):
            reads.append(regions)
            return ()

        def observe(pixels, now):
            return state.observe(
                pixels,
                view,
                context='synthetic',
                now=now,
                read_regions=read_regions,
                analyze=lambda *args: result,
            )

        self.assertIsNotNone(observe(frame, 0.0))
        self.assertIsNone(observe(model_moved, 0.1))
        self.assertIsNotNone(observe(small_glyph, 0.2))
        self.assertIsNone(observe(small_glyph, 0.3))
        self.assertIsNotNone(observe(small_glyph, 2.1))
        self.assertEqual(
            [view.ocr_regions(), view.live_regions('display'), view.ocr_regions()], reads
        )
