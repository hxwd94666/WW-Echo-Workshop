# 验证自动战斗期间实时评分会主动让出采集和 OCR 资源。
import unittest
from types import SimpleNamespace

from ww_echo_workshop.adapters.okww.live_guards import combat_active, combat_task as _combat_task


class CombatGuardContract(unittest.TestCase):
    def test_auto_combat_is_suspended(self):
        self.assertTrue(_combat_task(type('AutoCombatTask', (), {})()))
        self.assertTrue(_combat_task(type('BaseCombatTask', (), {})()))

    def test_paused_or_normal_task_is_not_combat(self):
        paused = type('AutoCombatTask', (), {'paused': True})()
        self.assertFalse(_combat_task(paused))
        self.assertFalse(_combat_task(SimpleNamespace(paused=False)))
        self.assertFalse(_combat_task(None))

    def test_combat_task_state_is_detected_when_trigger_task_is_current(self):
        auto = type('AutoCombatTask', (), {'_in_combat': True})()
        executor = SimpleNamespace(trigger_tasks=(auto,))
        active, hold = combat_active(executor, None, 0.0)
        self.assertTrue(active)
        self.assertGreater(hold, 0.0)
        auto._in_combat = False
        active, hold = combat_active(executor, None, hold)
        self.assertTrue(active)
        active, _ = combat_active(executor, None, 0.0)
        self.assertFalse(active)
