# 验证角色要求批量校验的协议、错误定位和取消。
import threading
import unittest
from pathlib import Path

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.requirements_check import RequirementsCheckService
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.requirements import RoleRequirements

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def requirements(costs=(4, 3, 3, 1, 1), main=None, allowed=('套装甲',), sets=()):
    return RoleRequirements(costs, main or ((),) * 5, allowed, sets)


def broken(spec, field, value):
    object.__setattr__(spec, field, value)
    return spec


@unittest.skipUnless(ENGINE.is_file(), '需要先运行 tools/compute.py build 生成原生计算程序')
class RequirementChecks(unittest.TestCase):
    def setUp(self):
        self.jobs = ComputeJobs(ENGINE)
        self.addCleanup(self.jobs.wait_closed)
        self.addCleanup(self.jobs.close)
        self.service = RequirementsCheckService(compute=self.jobs)

    def test_valid_models_return_ordered_empty_errors(self):
        first = requirements()
        second = requirements(costs=(4, 4, 1, 1, 1))
        errors = self.service.check({('1304', 'default'): first, ('1102', 'default'): second})
        self.assertEqual(
            ((('1304', 'default'), None), (('1102', 'default'), None)),
            errors,
        )

    def test_invalid_layout_main_and_set_are_located_per_model(self):
        bad_cost = broken(requirements(), 'costs', (4, 4, 4, 4, 4))
        bad_main = broken(requirements(), 'main_names', ((), (), (), (), ('暴击',)))
        bad_sets = broken(
            requirements(allowed=('套装甲', '套装乙')),
            'set_requirements',
            (('套装甲', 3), ('套装乙', 3)),
        )
        request = self.service.start(
            {
                ('1304', 'default'): requirements(),
                ('1102', 'default'): bad_cost,
                ('1103', 'default'): bad_main,
                ('1104', 'default'): bad_sets,
            }
        )
        request.future.result(timeout=12)
        errors = dict(self.service.response(request))
        self.assertIsNone(errors[('1304', 'default')])
        self.assertIn('不得超过 12', errors[('1102', 'default')])
        self.assertIn('主词条', errors[('1103', 'default')])
        self.assertIn('套装', errors[('1104', 'default')])

    def test_response_rejects_wrong_identity_without_waiting(self):
        request = self.service.start({('1304', 'default'): requirements()})
        request.future.result(timeout=12)
        wrong = type(request)(request.future, 'other', request.keys)
        with self.assertRaises(ComputeFailure) as error:
            self.service.response(wrong)
        self.assertEqual('request_identity', error.exception.kind)

    def test_cancelled_worker_does_not_publish_verdicts(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            self.service.check({('1304', 'default'): requirements()}, cancel_event=cancel)

    def test_batch_and_identity_limits_are_typed_errors(self):
        with self.assertRaises(ValueError):
            self.service.start({(str(index), 'default'): requirements() for index in range(513)})
        with self.assertRaises(ValueError):
            self.service.start({('1304',): requirements()})
        with self.assertRaises(ValueError):
            self.service.start({('1304', 'default'): object()})
