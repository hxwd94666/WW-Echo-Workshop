# 校验 HUD 窗口的锁定穿透与编辑模式切换。
import time
import unittest
from types import SimpleNamespace

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from ww_echo_workshop.ui.workspace.hud import HudController, HudWindow
except ImportError:
    Qt = None
    QApplication = None
    HudWindow = None

from ww_echo_workshop.application.hud_state import HudState


@unittest.skipIf(HudWindow is None, '当前解释器没有 PySide6')
class HudWindowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_lock_and_edit_modes(self):
        task = SimpleNamespace(
            config={
                '启用声骸评分': True,
                '强化策略目标分': 35.0,
                '策略浮层模式': 'qt',
                '策略悬浮窗位置': '',
            }
        )
        runtime = SimpleNamespace(hud_state=HudState(), _closed=False)
        window = HudWindow(task, runtime)
        self.assertTrue(window.isWindow())
        self.assertTrue(window.testAttribute(Qt.WA_TranslucentBackground))
        self.assertFalse(window.windowFlags() & Qt.WindowTransparentForInput)
        self.assertTrue(window.normal_buttons.isVisible())
        window.set_rows((('模版：测试', (255, 110, 220)), ('策略：继续强化', (125, 211, 252))))
        self.assertEqual(len(window._line_labels), 2)
        window.edit()
        self.assertFalse(window.windowFlags() & Qt.WindowTransparentForInput)
        self.assertTrue(window.edit_panel.isVisible())
        edit_height = window.height()
        window.lock()
        self.assertLess(window.height(), edit_height)
        self.assertTrue(window.windowFlags() & Qt.WindowTransparentForInput)
        window.close()

    def test_comparison_rows_use_paired_colors(self):
        task = SimpleNamespace(config={'启用声骸评分': True, '策略浮层模式': 'qt'})
        runtime = SimpleNamespace(hud_state=HudState(), _closed=False)
        window = HudWindow(task, runtime)
        rows = (
            ('丹瑾-通用', ((255, 110, 220),)),
            ('当前评分：', ()),
            ('31.77 / 31.77', ((255, 220, 80), (255, 220, 80))),
            ('理论最高：', ()),
            ('31.77 / 31.77', ()),
            ('左策略：继续强化', ((125, 211, 252),)),
            ('右策略：继续强化', ((125, 211, 252),)),
        )
        window.set_rows(rows)
        self.assertEqual(len(window._line_labels), len(rows))
        self.assertIn('<span', window._line_labels[2].text())
        window.close()

    def test_controller_hides_without_visible_state(self):
        task = SimpleNamespace(
            config={
                '启用声骸评分': True,
                '策略浮层模式': 'qt',
                '强化策略目标分': 35.0,
            }
        )
        state = HudState()
        runtime = SimpleNamespace(hud_state=state, _closed=False)
        controller = HudController(task, runtime)
        self.assertFalse(controller.window.isVisible())
        state.update('策略：继续强化', ((125, 211, 252),))
        controller._tick()
        self.assertTrue(controller.window.isVisible())
        state.hide()
        controller.window._last_interaction = time.monotonic() - 1.0
        controller._hidden_since = time.monotonic() - 1.0
        controller._tick()
        self.assertFalse(controller.window.isVisible())
        controller.close()


if __name__ == '__main__':
    unittest.main()
