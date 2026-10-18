# 验证固定件原生校验与迁移前冻结结论、角色身份和取消合同。
from concurrent.futures import Future
import copy
import json
from pathlib import Path
import threading
import unittest

from echo_workshop.application.compute_protocol import ComputeFailure, ComputeWorker
from echo_workshop.application.pins import PinCheck, PinCheckService, pin_error_text
from echo_workshop.application.matching.feedback import (
    validate_match_requirements,
    MatchRequirementsError,
)
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.requirements import RoleRequirements
from echo_workshop.resources.weights import WeightSnapshot
from tests.native_runtime import ENGINE, pin_checks
from tests.calculation.helpers import tuned


class TestPinChecks(unittest.TestCase):
    def test_frozen_valid_missing_pending_cost_and_main_conclusions(self):
        cases = json.loads(
            (Path(__file__).with_name('fixtures') / 'pin_checks.json').read_text('utf-8')
        )['cases']
        with ComputeWorker(ENGINE) as worker:
            for case in cases:
                with self.subTest(case=case['name']):
                    value = worker.call(
                        'pin_check',
                        dict(
                            identity=case['name'],
                            requirements=case['requirements'],
                            records=case['records'],
                        ),
                    )
                    self.assertEqual(case['name'], value['identity'])
                    self.assertEqual(case['expected'], value['verdicts'])
                    verdict = value['verdicts'][0]
                    if verdict['code']:
                        self.assertEqual(
                            case['message'], pin_error_text(verdict['slot'], verdict['code'])
                        )
            bad = copy.deepcopy(cases[0])
            bad['requirements'][0]['fixed_item_ids'][1] = bad['requirements'][0]['fixed_item_ids'][
                0
            ]
            with self.assertRaises(ComputeFailure):
                worker.call(
                    'pin_check',
                    dict(identity='bad', requirements=bad['requirements'], records=bad['records']),
                )
            self.assertEqual({'ok': True}, worker.call('ping', {'ok': True}))
        print('NATIVE_PIN_CHECK_FROZEN_OK:', len(cases))

    def test_batch_identity_and_matching_role_feedback_keep_missing_uuid(self):
        profile = '00000000-0000-4000-8000-000000000001'
        record = tuned(profile, 4)
        spec = RoleRequirements(
            (4, 3, 3, 1, 1), ((),) * 5, (), fixed_item_ids=(record.item_id, '', '', '', '')
        )
        keys = [('1304', 'default'), ('1109', 'default')]
        self.assertEqual((), pin_checks.check(dict.fromkeys(keys, spec), (record,)))
        errors = pin_checks.check(dict.fromkeys(keys, spec), ())
        self.assertEqual(tuple(keys), tuple(row[0] for row in errors))
        with self.assertRaises(MatchRequirementsError) as error:
            validate_match_requirements(
                {keys[0]: spec}, (), WeightSnapshot.builtin(), pin_checks=pin_checks
            )
        self.assertEqual(keys[0], error.exception.feedback.role_key)
        self.assertIn('缺失', str(error.exception))
        self.assertEqual(record.item_id, spec.fixed_item_ids[0])

    def test_response_never_waits_and_cancelled_check_does_not_publish(self):
        future = Future()
        request = PinCheck(future, 'identity', (('1304', 'default'),))
        with self.assertRaises(ComputeFailure) as error:
            PinCheckService.response(request)
        self.assertEqual('pending', error.exception.kind)
        future.set_result(dict(identity='wrong', verdicts=[]))
        with self.assertRaises(ComputeFailure) as error:
            PinCheckService.response(request)
        self.assertEqual('request_identity', error.exception.kind)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            pin_checks.check({}, (), cancel_event=cancel)
        self.assertEqual((), pin_checks.check({}, ()))
