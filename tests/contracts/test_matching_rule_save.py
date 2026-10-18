# 验证匹配要求校验不等待界面、合并最新草稿且只有有效原生结论才提交。
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest

from echo_workshop.application.pins import PinCheckService
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.requirements import RoleRequirements
from echo_workshop.resources.weights import WeightSnapshot
from echo_workshop.ui.matching.settings import MatchingRoleSettingsController
from tests.calculation.helpers import tuned
from tests.native_runtime import pin_checks


class _NoWaitFuture(Future):
    def result(self, *args, **kwargs):
        if not self.done():
            raise AssertionError('GUI waited for unfinished validation')
        return super().result(*args, **kwargs)


class _Compute:
    def __init__(self):
        self.requests = []

    def submit(self, op, build, **kwargs):
        future = _NoWaitFuture()
        self.requests.append((future, build))
        return future

    def complete(self, index, *, code=''):
        future, build = self.requests[index]
        with ThreadPoolExecutor(max_workers=1) as pool:
            value = pool.submit(build).result(timeout=2)
        future.set_result(
            dict(
                identity=value['identity'],
                verdicts=[
                    dict(
                        key=list(spec['key']),
                        slot=1 if code else None,
                        code=code,
                    )
                    for spec in value['requirements']
                ],
            )
        )


class _HostlessWidget(QWidget):
    def stop_worker(self):
        self._closing = True


class _Editor(MatchingRoleSettingsController, _HostlessWidget):
    def __init__(self, service):
        QWidget.__init__(self)
        self.key = ('1304', 'default')
        self.profile = '00000000-0000-4000-8000-000000000001'
        self.record = tuned(self.profile, 4)
        self.inputs = dict(costs=(4, 3, 3, 1, 1), names='测试套装', profile=self.profile)
        spec = RoleRequirements(
            self.inputs['costs'],
            ((),) * 5,
            ('测试套装',),
            fixed_item_ids=(self.record.item_id, '', '', '', ''),
        )
        self._role_requirements = {self.key: spec}
        self._cost_layouts = {self.key: spec.costs}
        self._rules = {self.key: CompatibilityRule('衍射', ('测试套装',))}
        self.runtime = SimpleNamespace(
            pin_checks=service, weights=SimpleNamespace(active=WeightSnapshot.builtin())
        )
        self.profile_box = SimpleNamespace(currentData=lambda: self.inputs['profile'])
        self.participants = SimpleNamespace(currentData=lambda: self.key)
        self.element_rule = SimpleNamespace(currentData=lambda: '衍射')
        self.sonata_rule = SimpleNamespace(text=lambda: self.inputs['names'])
        self.cost_layout_picker = SimpleNamespace(costs=lambda: self.inputs['costs'])
        self.main_requirements_picker = SimpleNamespace(main_names=lambda: ((),) * 5)
        self.fixed_items_picker = SimpleNamespace(item_ids=lambda: spec.fixed_item_ids)
        self.item_picker = SimpleNamespace(catalog=(self.record,))
        self.task = SimpleNamespace(_panel=SimpleNamespace(refresh_profile_guard=Mock()))
        self.status = SimpleNamespace(setText=Mock())
        self._calculation_error = ''
        self._rule_save_request = self._pending_rule_draft = self._active_rule_draft = None
        self._rule_save_timer = self._rule_save_job = None
        self._rule_save_epoch = 0
        self._closing = self._loading = self._saving_rule = self._preserve_edit_widgets = False
        self._rule_dirty = True
        self._rule_baseline = None
        self.save_preferences = Mock()
        self.changed = Mock()
        self._refresh_controls = Mock()

    def selections(self):
        return (self.key,)


class MatchingRuleSaveContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.compute = _Compute()
        self.editor = _Editor(PinCheckService(compute=self.compute))
        self.addCleanup(self.editor.deleteLater)
        self.addCleanup(self.editor.stop_worker)

    def test_pending_validation_never_saves_or_waits_then_commits_once(self):
        self.editor.save_rule()
        self.editor._poll_rule_validation()
        self.editor.save_preferences.assert_not_called()
        self.assertTrue(self.editor._rule_dirty)
        self.compute.complete(0)
        self.editor._poll_rule_validation()
        self.editor.save_preferences.assert_called_once()
        self.assertFalse(self.editor._rule_dirty)

    def test_edits_coalesce_to_one_latest_pending_request(self):
        self.editor.save_rule()
        self.editor.inputs['names'] = '中间套装'
        self.editor.save_rule()
        self.editor.inputs['names'] = '最后套装'
        self.editor.save_rule()
        self.assertEqual(len(self.compute.requests), 1)
        # Simulate a completed older response despite its cancellation marker.
        self.editor._rule_save_request.future.set_result({})
        self.editor._poll_rule_validation()
        self.editor.save_preferences.assert_not_called()
        self.assertEqual(len(self.compute.requests), 2)
        self.compute.complete(1)
        self.editor._poll_rule_validation()
        self.editor.save_preferences.assert_called_once()
        self.assertEqual(
            self.editor._role_requirements[self.editor.key].allowed_sonatas, ('最后套装',)
        )

    def test_missing_pin_and_save_failure_do_not_half_commit(self):
        original = self.editor._role_requirements, self.editor._cost_layouts, self.editor._rules
        self.editor.save_rule()
        self.compute.complete(0, code='missing')
        self.editor._poll_rule_validation()
        self.editor.save_preferences.assert_not_called()
        self.assertIn('缺失', self.editor._rule_save_error)
        self.editor.save_rule()
        self.editor.save_preferences.side_effect = OSError('synthetic persistence error')
        self.compute.complete(1)
        self.editor._poll_rule_validation()
        for before, after in zip(
            original,
            (self.editor._role_requirements, self.editor._cost_layouts, self.editor._rules),
        ):
            self.assertIs(before, after)
        self.assertTrue(self.editor._rule_dirty)

    def test_invalid_latest_edit_revokes_queued_valid_draft(self):
        self.editor.save_rule()
        self.editor.inputs['names'] = '中间套装'
        self.editor.save_rule()
        self.editor.inputs['costs'] = (4, 4, 4, 1, 1)
        self.editor._autosave_rule()
        self.assertIsNone(self.editor._pending_rule_draft)
        self.editor._rule_save_request.future.set_result({})
        self.editor._poll_rule_validation()
        self.assertEqual(len(self.compute.requests), 1)
        self.editor.save_preferences.assert_not_called()
        self.assertTrue(self.editor._rule_dirty)

    def test_changed_profile_or_closed_owner_discards_result(self):
        self.editor.save_rule()
        self.editor.inputs['profile'] = '00000000-0000-4000-8000-000000000002'
        self.compute.complete(0)
        self.editor._poll_rule_validation()
        self.editor.save_preferences.assert_not_called()
        self.editor.save_rule()
        self.editor.stop_worker()
        self.editor._rule_save_request.future.set_result({})
        self.editor._poll_rule_validation()
        self.editor.save_preferences.assert_not_called()

    def test_real_engine_validation_is_asynchronous_on_gui(self):
        self.editor.runtime.pin_checks = pin_checks
        with patch.object(pin_checks, 'check', side_effect=AssertionError('blocking GUI check')):
            self.editor.save_rule()
            self.editor.save_preferences.assert_not_called()
            end = time.monotonic() + 4
            while not self.editor.save_preferences.called and time.monotonic() < end:
                QTest.qWait(10)
            self.editor.save_preferences.assert_called_once()
