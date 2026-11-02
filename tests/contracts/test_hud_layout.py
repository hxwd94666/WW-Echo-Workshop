# 验证四种 HUD 场景在 OKWW 支持分辨率下的锚点和词条区边界。
import unittest

from ww_echo_workshop.ui.workspace.hud import (
    COMPARISON_LEFT,
    RIGHT_DETAIL_LEFT,
    layout_position,
)


class HudLayoutContract(unittest.TestCase):
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
