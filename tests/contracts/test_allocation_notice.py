# 验证配装结果不会与顶部状态重复显示搜索上限提示。
import unittest
from types import SimpleNamespace

from ww_echo_workshop.ui.allocation.results import AllocationUX


class FakeLabel:
    def __init__(self):
        self.text = ''
        self.visible = False

    def setText(self, text):
        self.text = text

    def setToolTip(self, text):
        self.tooltip = text

    def setVisible(self, visible):
        self.visible = visible

    def clear(self):
        self.text = ''


class FakeStatus:
    def __init__(self, visible, text):
        self.visible = visible
        self._text = text

    def isVisible(self):
        return self.visible

    def text(self):
        return self._text

    def setVisible(self, visible):
        self.visible = visible

    def clear(self):
        self._text = ''
        self.visible = False


class FakeOutcome:
    def __init__(self):
        self.text = ''

    def setPlainText(self, text):
        self.text = text


class AllocationNoticeContract(unittest.TestCase):
    def panel(self, *, status_visible, status_text):
        status = FakeStatus(status_visible, status_text)
        return SimpleNamespace(
            status=status,
            outcome=FakeOutcome(),
            outcome_label=FakeLabel(),
            precheck_panel=SimpleNamespace(toggle=SimpleNamespace(setChecked=lambda _: None)),
            steps=SimpleNamespace(setCurrentIndex=lambda _: None),
        )

    def run_result(self, panel, exhaustive):
        AllocationUX.show_outcome(
            panel,
            {'exhaustive': exhaustive, 'roles': [], 'request': {}},
        )

    def test_limited_result_does_not_repeat_visible_status(self):
        panel = self.panel(
            status_visible=True,
            status_text='搜索已到上限，当前方案未保证最优。',
        )
        self.run_result(panel, False)
        self.assertTrue(panel.outcome_label.visible)
        self.assertFalse(panel.status.visible)
        self.assertEqual(panel.status.text(), '')

    def test_limited_result_keeps_summary_when_status_hidden(self):
        panel = self.panel(status_visible=False, status_text='')
        self.run_result(panel, False)
        self.assertTrue(panel.outcome_label.visible)

    def test_complete_result_keeps_summary(self):
        panel = self.panel(status_visible=True, status_text='搜索完成')
        self.run_result(panel, True)
        self.assertTrue(panel.outcome_label.visible)
