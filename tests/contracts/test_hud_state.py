# 校验 HUD 文本快照只读更新与隐藏。
import unittest

from ww_echo_workshop.application.hud_state import HudState


class HudStateContract(unittest.TestCase):
    def test_update_and_hide(self):
        state = HudState()
        state.update('策略：继续强化', ((125, 211, 252),))
        snapshot = state.snapshot()
        self.assertTrue(snapshot['visible'])
        self.assertEqual(snapshot['text'], '策略：继续强化')
        self.assertEqual(snapshot['colours'], ((125, 211, 252),))
        self.assertEqual(snapshot['rows'], (('策略：继续强化', (125, 211, 252)),))
        state.hide()
        self.assertFalse(state.snapshot()['visible'])
        state.update('', ())
        self.assertFalse(state.snapshot()['visible'])


if __name__ == '__main__':
    unittest.main()
