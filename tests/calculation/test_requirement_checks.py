# 验证原生要求总量/主词条/套装校验及合并入口，不依赖 Python 构造器挡错。
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from echo_workshop.application.compute_bridge import plain, record_payload
from echo_workshop.application.compute_protocol import ComputeWorker, ComputeFailure
from echo_workshop.resources.weights import WeightSnapshot
from tests.native_runtime import ENGINE, ScoreService, _jobs
from tests.calculation.helpers import tuned

FIXTURE = Path(__file__).with_name('fixtures') / 'requirement_checks.json'


class TestRequirementChecks(unittest.TestCase):
    def test_frozen_constructor_contracts_match_native_business_guard(self):
        cases = json.loads(FIXTURE.read_text('utf-8'))['cases']
        with ComputeWorker(ENGINE) as worker:
            result = worker.call(
                'requirements_check',
                dict(
                    identity='frozen-models',
                    models=[case['requirements'] for case in cases],
                ),
            )
        self.assertEqual('frozen-models', result['identity'])
        self.assertEqual([case['error'] for case in cases], result['errors'])
        print('NATIVE_REQUIREMENTS_FROZEN_OK:', len(cases))

    def test_merged_pin_and_matching_backends_reject_bypassed_model_guards(self):
        cases = json.loads(FIXTURE.read_text('utf-8'))['cases']
        snapshot = WeightSnapshot.builtin()
        key = ('1304', 'default')
        template = plain(snapshot.scoring_template(*key))
        record = tuned('00000000-0000-4000-8000-000000000001', 4)
        checked = (
            'cost_total',
            'wrong_cost_main',
            'set_sum',
            'set_unknown',
            'duplicate_set_canonical',
        )
        with ComputeWorker(ENGINE) as worker:
            for case in cases:
                if case['name'] not in checked:
                    continue
                spec = dict(case['requirements'], fixed_item_ids=[''] * 5)
                requests = (
                    (
                        'pin_check',
                        dict(
                            identity=case['name'], requirements=[dict(key=key, **spec)], records=[]
                        ),
                    ),
                    (
                        'match_batch',
                        dict(
                            records=[record_payload(record)],
                            selections=[
                                dict(
                                    character_id=key[0],
                                    modal_id=key[1],
                                    template=template,
                                    rule=dict(element='衍射', sonatas=['测试套装']),
                                    slots=spec,
                                )
                            ],
                        ),
                    ),
                )
                for op, payload in requests:
                    with (
                        self.subTest(case=case['name'], op=op),
                        self.assertRaises(ComputeFailure) as error,
                    ):
                        worker.call(op, payload)
                    self.assertEqual('invalid_request', error.exception.kind)
                    self.assertEqual(case['error'], str(error.exception))
            self.assertEqual({'ok': True}, worker.call('ping', {'ok': True}))

    def test_changed_set_policy_invalidates_cached_scores_without_extra_validation_rpc(self):
        raw = copy.deepcopy(json.loads(FIXTURE.read_text('utf-8'))['cases'][0]['requirements'])
        raw['fixed_item_ids'] = ('',) * 5
        spec = SimpleNamespace(**raw)
        snapshot = WeightSnapshot.builtin()
        record = tuned('00000000-0000-4000-8000-000000000001', 4)
        key = ('1304', 'default')
        service = ScoreService(SimpleNamespace(active=snapshot))
        kwargs = dict(inventory_revision=7, requirements={key: spec})
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            good = service.compare((record,), (key,), **kwargs)
            raw_counts = copy.deepcopy(spec.set_requirements)
            spec.set_requirements[0][1] = 3
            with self.assertRaises(ComputeFailure) as error:
                service.compare((record,), (key,), **kwargs)
            self.assertEqual('invalid_request', error.exception.kind)
            spec.set_requirements = raw_counts
            self.assertEqual(good, service.compare((record,), (key,), **kwargs))
        self.assertEqual(['match_batch'] * 3, [call.args[0] for call in submit.call_args_list])
