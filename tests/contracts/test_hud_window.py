# 校验 HUD 窗口的锁定穿透与编辑模式切换。
import time
import unittest

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    from ww_echo_workshop.ui.workspace.hud import HudController, HudWindow
except ImportError:
    Qt = None
    QApplication = None
    HudWindow = None

from ww_echo_workshop.application.hud_state import HudState
from ww_echo_workshop.application.live_preferences import LivePreferences


@unittest.skipIf(HudWindow is None, '当前解释器没有 PySide6')
class HudWindowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _controller(self, config, state):
        return HudController(
            LivePreferences(config), weight_snapshot=lambda: None, state=state, closed=lambda: False
        )

    def test_lock_and_edit_modes(self):
        config = {
            '启用声骸评分': True,
            '强化策略目标分': 35.0,
            '策略浮层模式': 'qt',
            '策略悬浮窗位置': '',
        }
        window = HudWindow(LivePreferences(config), lambda: None)
        self.assertTrue(window.isWindow())
        self.assertTrue(window.testAttribute(Qt.WA_TranslucentBackground))
        self.assertFalse(window.windowFlags() & Qt.WindowTransparentForInput)
        self.assertTrue(window.normal_buttons.isVisible())
        self.assertFalse(window.lock_button.icon().isNull())
        self.assertFalse(window.details_button.icon().isNull())
        self.assertEqual(window.lock_button.text(), '')
        self.assertEqual(window.details_button.text(), '')
        window.set_rows((('模版：测试', (255, 110, 220)), ('策略：继续强化', (125, 211, 252))))
        self.assertEqual(len(window._line_labels), 2)
        window.edit()
        self.assertFalse(window.windowFlags() & Qt.WindowTransparentForInput)
        self.assertTrue(window.edit_panel.isVisible())
        checks = window.findChild(QWidget, 'hudChecks')
        self.assertIsNotNone(checks)
        self.assertEqual(checks.layout().spacing(), 12)
        labels = [checks.layout().itemAt(index).widget().text() for index in range(4)]
        self.assertEqual(
            labels,
            ['实时评分', '悬浮窗位置变换（仅适配2K）', '跟随角色', '策略建议'],
        )
        self.assertIn('QCheckBox::indicator', window.edit_panel.styleSheet())
        self.assertIn('QCheckBox::indicator', window.live_check.styleSheet())
        self.assertIn('border: 2px solid #ffffff', window.live_check.styleSheet())
        self.assertIn('background-color: transparent', window.unlock_button.styleSheet())
        self.assertTrue(window.hud_follow_check.isVisible())
        window.hud_follow_check.setChecked(True)
        self.assertTrue(config['策略悬浮窗自动定位'])
        window.hud_follow_check.setChecked(False)
        self.assertFalse(config['策略悬浮窗自动定位'])
        edit_height = window.height()
        window.lock()
        self.assertLess(window.height(), edit_height)
        self.assertTrue(window.windowFlags() & Qt.WindowTransparentForInput)
        self.assertTrue(window.unlock_button.isVisible())
        self.assertFalse(window.unlock_button.icon().isNull())
        self.assertEqual(window.unlock_button.text(), '')
        window.close()

    def test_controller_unlock_rebuilds_window(self):
        config = {
            '启用声骸评分': True,
            '强化策略目标分': 35.0,
            '策略浮层模式': 'qt',
            '策略悬浮窗位置': '',
        }
        state = HudState()
        controller = self._controller(config, state)
        state.update('策略：继续强化', ((125, 211, 252),))
        controller.refresh()
        old = controller.window
        old.lock()
        old.unlock_button.click()
        QApplication.processEvents()
        self.assertIsNot(controller.window, old)
        self.assertFalse(controller.window._locked)
        self.assertTrue(controller.window.isVisible())
        self.assertEqual(controller.window._rows, old._rows)
        controller.close()

    def test_comparison_rows_use_paired_colors(self):
        config = {'启用声骸评分': True, '策略浮层模式': 'qt'}
        window = HudWindow(LivePreferences(config), lambda: None)
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
        config = {
            '启用声骸评分': True,
            '策略浮层模式': 'qt',
            '强化策略目标分': 35.0,
        }
        state = HudState()
        controller = self._controller(config, state)
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

    def test_expiry_keeps_hud_interaction_without_showing_old_score(self):
        from PySide6.QtCore import QPoint

        config = {'启用声骸评分': True, '策略浮层模式': 'qt'}
        for interaction in ('details', 'dragging', 'unlock'):
            with self.subTest(interaction=interaction):
                state = HudState()
                controller = self._controller(config, state)
                try:
                    state.update('实时评分 17.02')
                    controller._tick()
                    window = controller.window
                    if interaction == 'details':
                        window.edit()
                    elif interaction == 'dragging':
                        window._drag = QPoint(3, 5)
                    else:
                        window.lock()
                    state._updated_at = time.monotonic() - 7
                    if interaction == 'unlock':
                        controller._rebuild_window()
                    else:
                        controller._tick()
                    window = controller.window
                    self.assertTrue(window.isVisible())
                    self.assertEqual('等待新的评分画面…', window._line_labels[0].text())
                    if interaction == 'details':
                        self.assertTrue(window.edit_panel.isVisible())
                    elif interaction == 'dragging':
                        self.assertEqual(QPoint(3, 5), window._drag)
                    else:
                        self.assertFalse(window._locked)
                    state.update('实时评分 17.73')
                    controller._tick()
                    self.assertEqual('实时评分 17.73', window._line_labels[0].text())
                finally:
                    controller.close()


if __name__ == '__main__':
    unittest.main()
