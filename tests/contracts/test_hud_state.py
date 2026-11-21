# 校验 HUD 文本快照只读更新与隐藏。
import unittest
from unittest.mock import patch

from ww_echo_workshop.application.hud_state import HudState


class HudStateContract(unittest.TestCase):
    def test_live_snapshot_expires_without_another_host_frame_and_recovers(self):
        state, now = HudState(), [35.0]
        with patch('ww_echo_workshop.application.hud_state.time.monotonic', lambda: now[0]):
            state.update('实时评分 17.02', ((125, 211, 252),), layout='bag')
            now[0] = 40.99
            self.assertTrue(state.snapshot()['visible'])
            now[0] = 41.0
            self.assertFalse(state.snapshot()['visible'])
            self.assertEqual((), state.snapshot()['rows'])
            self.assertEqual('', state.snapshot()['text'])
            state.update('实时评分 17.73', layout='display')
            self.assertEqual(
                (True, '实时评分 17.73'),
                tuple(state.snapshot()[key] for key in ('visible', 'text')),
            )

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
