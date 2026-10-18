# 用迁移前冻结投影验证原生全局排序、代表项、统计与身份隔离。
from concurrent.futures import Future
from dataclasses import replace
import copy
import json
from pathlib import Path
import threading
import unittest

from echo_workshop.application.compute_protocol import ComputeWorker, ComputeFailure
from echo_workshop.application.match_projection import MatchProjectionService
from echo_workshop.application.scoring import ComparisonScore
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.scoring import EchoScoreResult, ALGORITHM_VERSION
from tests.calculation.helpers import tuned
from tests.native_runtime import ENGINE, _jobs

FIXTURE = Path(__file__).with_name('fixtures') / 'match_projection.json'


def case_inputs(case):
    records = tuple(
        tuned('00000000-0000-4000-8000-000000000001', item_id=identity, name=f'Echo-{row}')
        for row, identity in enumerate(case['ids'])
    )
    keys = tuple(tuple(key) for key in case['roles'])
    scores = []
    for offset, (current, reason, recommendation) in enumerate(case['cells']):
        row, column = divmod(offset, len(keys))
        value = EchoScoreResult(1, '1', (), current, 50, None) if current is not None else None
        scores.append(
            ComparisonScore(
                records[row].item_id,
                *keys[column],
                'frozen',
                ALGORITHM_VERSION,
                7,
                value,
                'unknown',
                (),
                recommendation=recommendation,
                exclusion=reason,
            )
        )
    return records, keys, tuple(scores)


class TestMatchProjection(unittest.TestCase):
    def test_frozen_prior_projection_including_global_uuid_ties_and_unknowns(self):
        cases = json.loads(FIXTURE.read_text('utf-8'))['cases']
        service = MatchProjectionService(compute=_jobs)
        for case in cases:
            with self.subTest(case=case['name']):
                records, keys, scores = case_inputs(case)
                result = service.project(records, keys, scores)
                self.assertTrue(result.matches(records, keys, scores))
                actual = [
                    dict(
                        role=None if index == 0 else index - 1,
                        entries=[list(entry) for entry in view.entries],
                        excluded=view.excluded,
                        unknown=view.unknown,
                    )
                    for index, view in enumerate(result.views)
                ]
                self.assertEqual(case['expected_views'], actual)
        print('MATCH_PROJECTION_FROZEN_OK:', len(cases))

    def test_bad_shape_duplicate_identity_and_unknown_reason_leave_worker_healthy(self):
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][0]
        payload = dict(
            identity='snapshot', ids=case['ids'], roles=case['roles'], cells=case['cells']
        )
        with ComputeWorker(ENGINE) as worker:
            for field in ('duplicate-id', 'duplicate-role', 'bad-reason', 'wrong-size'):
                bad = copy.deepcopy(payload)
                if field == 'duplicate-id':
                    bad['ids'][1] = bad['ids'][0]
                elif field == 'duplicate-role':
                    bad['roles'][1] = bad['roles'][0]
                elif field == 'bad-reason':
                    bad['cells'][0][1] = 'invented'
                else:
                    bad['cells'].pop()
                with self.subTest(field=field), self.assertRaises(ComputeFailure):
                    worker.call('match_projection', bad)
            self.assertEqual({'state': 'healthy'}, worker.call('ping', {'state': 'healthy'}))

    def test_input_and_response_identity_are_checked_without_fallback(self):
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][0]
        records, keys, scores = case_inputs(case)

        class WrongResponse:
            def submit(self, op, builder, **kwargs):
                builder()
                future = Future()
                future.set_result(dict(identity='stale', flags=[], views=[]))
                return future

        service = MatchProjectionService(compute=WrongResponse())
        with self.assertRaises(ComputeFailure) as error:
            service.project(records, keys, scores)
        self.assertEqual('request_identity', error.exception.kind)
        mismatched = (replace(scores[0], item_id=records[1].item_id), *scores[1:])
        with self.assertRaises(ComputeFailure) as error:
            service.project(records, keys, mismatched)
        self.assertEqual('request_identity', error.exception.kind)

    def test_cancelled_projection_publishes_nothing_and_next_request_succeeds(self):
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][0]
        args = case_inputs(case)
        service = MatchProjectionService(compute=_jobs)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            service.project(*args, cancel_event=cancel)
        self.assertTrue(service.project(*args).matches(*args))
