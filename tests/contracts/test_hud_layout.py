# 验证四种 HUD 场景在 OKWW 支持分辨率下的锚点和词条区边界。
import unittest
from types import SimpleNamespace

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication
from ww_echo_workshop.adapters.okww.live_presentation import live_summary
from ww_echo_workshop.application.live_preferences import LiveOption, LivePreferences
from ww_echo_workshop.application.hud_state import HudState

from ww_echo_workshop.ui.workspace.hud import (
    COMPARISON_LEFT,
    RIGHT_DETAIL_LEFT,
    HudWindow,
    layout_position,
)


class HudLayoutContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_comparison_slash_is_light_gray_without_color_leaking_across_sides(self):
        left = (182, 186, 194)
        right = (113, 187, 255)
        for colours, expected in (
            ((left, right), ['#b6bac2', '#c4c7cc', '#71bbff']),
            ((None, right), ['#f4f4f4', '#c4c7cc', '#71bbff']),
            ((left, None), ['#b6bac2', '#c4c7cc', '#f4f4f4']),
            ((None, None), ['#f4f4f4', '#c4c7cc', '#f4f4f4']),
        ):
            html, _, rich = HudWindow._format_row('13.61 / 30.00', colours)
            self.assertTrue(rich)
            doc = QTextDocument()
            doc.setHtml(html)
            spans = []
            cursor = doc.begin().begin()
            while not cursor.atEnd():
                fragment = cursor.fragment()
                spans.append((fragment.text(), fragment.charFormat().foreground().color().name()))
                cursor += 1
            self.assertEqual(list(zip(['13.61', ' / ', '30.00'], expected)), spans)

    def test_qt_hud_strategy_reserves_height_but_keeps_original_width_and_anchor(self):
        app = self.app
        prefs = LivePreferences({LiveOption.HUD_AUTO_POSITION.value: True})
        window = HudWindow(prefs, lambda: None)
        self.addCleanup(window.close)
        score = SimpleNamespace(
            current_score=21.0,
            potential_score=30.0,
            selected_template='角色模版',
            summary='',
            score_text='',
            observed_cost=4,
            rating_color=None,
            layout='display',
        )
        state = HudState()
        viewport = (0, 0, 800, 600)

        def render(advice='', *, enabled):
            text, colors = live_summary(score, policy_text=advice, policy_enabled=enabled)
            state.update(text, colors)
            window.set_rows(state.snapshot()['rows'])
            window.move_for_layout('display', viewport)
            window.show()
            app.processEvents()
            return window.width(), window.height(), window.pos().toTuple()

        baseline = render(enabled=False)
        waiting = render(enabled=True)
        self.assertEqual(window._line_labels[3].text(), '强化策略')
        self.assertEqual(window._line_labels[4].text(), '策略：计算中…')
        self.assertEqual(window._line_labels[5].text(), '期望：计算中…')
        self.assertEqual(waiting[0], baseline[0])
        self.assertEqual(waiting[2], baseline[2])
        self.assertGreater(waiting[1], baseline[1])
        ready = render('策略：继续强化 · 80%\n期望：100调 · 2筒', enabled=True)
        self.assertEqual(window._line_labels[5].text(), '期望：100调 · 2筒')
        self.assertEqual(ready, waiting)
        long_advice = render('策略：' + '较长提示' * 100, enabled=True)
        self.assertEqual(long_advice, waiting)
        self.assertTrue(window._line_labels[4].text().endswith('…'))
        self.assertEqual(prefs.position(), (80, 120))

    def test_supported_resolutions_keep_hud_inside_safe_columns(self):
        width = 300
        ratio = 1.25
        for physical_width, physical_height in (
            (2560, 1440),
            (1920, 1080),
            (1600, 900),
            (1280, 720),
        ):
            logical_width = physical_width / ratio
            logical_height = physical_height / ratio
            viewport = (0, 0, physical_width, physical_height)
            for layout in ('display', 'bag', 'comparison', 'enhancement'):
                x, y = layout_position(layout, viewport, width, ratio)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + width, logical_width)
                self.assertLessEqual(y, logical_height)
            display_x, _ = layout_position('display', viewport, width, ratio)
            self.assertLessEqual(display_x + width, round(logical_width * RIGHT_DETAIL_LEFT))
            bag_x, _ = layout_position('bag', viewport, width, ratio)
            self.assertLessEqual(bag_x + width, round(logical_width * RIGHT_DETAIL_LEFT))
            comparison_x, _ = layout_position('comparison', viewport, width, ratio)
            self.assertLessEqual(comparison_x + width, round(logical_width * COMPARISON_LEFT))

    def test_anchor_scales_with_dpi(self):
        viewport = (0, 0, 2560, 1440)
        x100, _ = layout_position('enhancement', viewport, 300, 1.0)
        x125, _ = layout_position('enhancement', viewport, 300, 1.25)
        self.assertGreater(x100, x125)
