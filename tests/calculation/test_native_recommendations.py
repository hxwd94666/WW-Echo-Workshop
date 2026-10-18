# 验证原生当前件比较保留冻结观测语义、一次批量、取消和进程故障边界。
from contextlib import ExitStack
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.recommendations import RecommendationService
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import EchoRecord, StatValue, canonical_json
from echo_workshop.resources.weights import WeightSnapshot
from tests.native_runtime import _jobs

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name('fixtures') / 'native_recommendations.json'
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def run_case(service, case, data, snapshot):
    record = EchoRecord.from_dict(case['record'])
    if case['mode'] == 'record':
        return asdict(service.compare(record, case['preferences'], snapshot))
    source = case['analysis']
    analysis = SimpleNamespace(
        **{k: v for k, v in source.items() if k not in ('main_rows', 'sub_rows')},
        main_rows=tuple(StatValue(**s) for s in source['main_rows']),
        sub_rows=tuple(StatValue(**s) for s in source['sub_rows']),
    )
    return [
        asdict(r)
        for r in service.compare_live(
            analysis,
            case['preferences'],
            data['profile_id'],
            snapshot,
            record,
        )
    ]


class NativeRecommendations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        cls.snapshot = WeightSnapshot.builtin()

    def test_frozen_observations_exact_without_python_business_evaluators(self):
        self.assertEqual(self.data['weight_version'], self.snapshot.version)
        with ExitStack() as stack:
            for name in (
                'echo_workshop.core.scoring.calculate_echo_score',
                'echo_workshop.core.scoring.score_prepared',
                'echo_workshop.core.compatibility.compatibility',
                'echo_workshop.core.rating.single_rating',
            ):
                stack.enter_context(patch(name, side_effect=AssertionError(name), create=True))
            stack.enter_context(
                patch.object(
                    EchoRecord,
                    'validation_issues',
                    new=property(lambda _: self.fail('Python validation')),
                )
            )
            for case in self.data['cases']:
                with self.subTest(case=case['name']):
                    value = run_case(
                        RecommendationService(compute=_jobs), case, self.data, self.snapshot
                    )
                    self.assertEqual(canonical_json(case['expected']), canonical_json(value))

    def test_one_native_batch_and_no_private_ocr_or_owner_cache_dependency(self):
        case = self.data['cases'][0]
        record = EchoRecord.from_dict(case['record'])
        service = RecommendationService(compute=_jobs)
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            result = service.compare(record, case['preferences'], self.snapshot)
            self.assertEqual(1, submit.call_count)
            args, options = submit.call_args
            self.assertEqual('current_comparison', args[0])
            payload = args[1]()
            self.assertEqual(4, len(payload['selections']))
            self.assertEqual({'timeout', 'cancel_event'}, options.keys())
            self.assertNotIn('raw_text', payload['record'])
            self.assertNotIn('screenshot', payload['record'])
            self.assertIs(
                result,
                service.compare(
                    replace(
                        record,
                        item_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                        locked=not record.locked,
                        owner='新归属',
                        raw_text='private',
                    ),
                    case['preferences'],
                    self.snapshot,
                ),
            )
            self.assertEqual(1, submit.call_count)
        event = threading.Event()
        event.set()
        with self.assertRaises(OperationCancelled), patch.object(_jobs, 'submit') as submit:
            service.compare(record, case['preferences'], self.snapshot, cancel_event=event)
        submit.assert_not_called()

    def test_cancellation_during_preparation_and_gui_guard_publish_nothing(self):
        case = self.data['cases'][0]
        record = EchoRecord.from_dict(case['record'])
        event = threading.Event()
        service = RecommendationService(compute=_jobs)
        original = self.snapshot.scoring_template

        def preparing(*key):
            event.set()
            return original(*key)

        with patch.object(type(self.snapshot), 'scoring_template', side_effect=preparing):
            with self.assertRaises(OperationCancelled):
                service.compare(record, case['preferences'], self.snapshot, cancel_event=event)
        self.assertIsNone(service._result)
        app = SimpleNamespace(thread=lambda: 'gui')
        qt = SimpleNamespace(
            QCoreApplication=SimpleNamespace(instance=lambda: app),
            QThread=SimpleNamespace(currentThread=lambda: 'gui'),
        )
        with (
            patch.dict(sys.modules, {'PySide6.QtCore': qt}),
            patch.object(_jobs, 'submit') as submit,
        ):
            with self.assertRaises(ComputeFailure) as failure:
                service.compare(record, case['preferences'], self.snapshot)
            self.assertEqual('gui_thread', failure.exception.kind)
        submit.assert_not_called()

    def test_live_cache_tracks_observation_metadata_profile_and_cost_certainty(self):
        case = next(c for c in self.data['cases'] if c['name'] == '1_normal')
        record = EchoRecord.from_dict(case['record'])
        source = case['analysis']
        analysis = SimpleNamespace(
            current_score=1,
            cost=1,
            observed_cost=1,
            main_rows=record.main_stats,
            sub_rows=record.sub_stats,
        )
        service = RecommendationService(compute=_jobs)

        def compare(**kwargs):
            return service.compare_live(
                analysis,
                case['preferences'],
                self.data['profile_id'],
                self.snapshot,
                kwargs.get('record', record),
            )

        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            original = compare()
            self.assertIs(original, compare(record=replace(record, owner='changed')))
            self.assertEqual(1, submit.call_count)
            analysis.observed_cost = None
            uncertain = compare()
            self.assertTrue(all(r.rating is None for r in uncertain))
            self.assertEqual(2, submit.call_count)
            compare(record=replace(record, issues=('ocr:sonata_needs_review',)))
            self.assertEqual(3, submit.call_count)
            analysis.current_score = None
            self.assertEqual((), compare())
            self.assertEqual(3, submit.call_count)
        self.assertEqual(source['cost'], analysis.cost)

    def test_live_first_three_selection_projection_skips_deleted_ids_without_ranking(self):
        case = next(c for c in self.data['cases'] if c['name'] == '3_normal')
        record = EchoRecord.from_dict(case['record'])
        raw = json.loads(case['preferences'])
        selected = raw['profiles'][self.data['profile_id']]['selections']
        raw['profiles'][self.data['profile_id']]['selections'] = [['deleted', 'default']] + selected
        analysis = SimpleNamespace(
            current_score=1,
            cost=3,
            observed_cost=3,
            main_rows=record.main_stats,
            sub_rows=record.sub_stats,
        )
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            rows = RecommendationService(compute=_jobs).compare_live(
                analysis,
                json.dumps(raw),
                self.data['profile_id'],
                self.snapshot,
                record,
            )
        self.assertEqual(selected[:3], [[r.character_id, r.modal_id] for r in rows])
        self.assertEqual(1, submit.call_count)
        payload = submit.call_args.args[1]()
        self.assertEqual(
            selected[:3], [[r['character_id'], r['modal_id']] for r in payload['selections']]
        )

    def test_native_panic_reaps_child_then_fresh_request_succeeds_without_cached_result(self):
        case = self.data['cases'][0]
        record = EchoRecord.from_dict(case['record'])
        jobs = ComputeJobs(ENGINE, fault_tests=True)

        class FaultOnce:
            failed = False

            def submit(self, operation, build, **options):
                if not self.failed:
                    self.failed = True
                    operation = 'fault_panic'
                return jobs.submit(operation, build, **options)

        service = RecommendationService(compute=FaultOnce())
        try:
            with self.assertRaises(ComputeFailure):
                service.compare(record, case['preferences'], self.snapshot)
            self.assertIsNone(service._result)
            self.assertEqual(0, service.calculations)
            self.assertEqual(
                canonical_json(case['expected']),
                canonical_json(
                    asdict(
                        service.compare(record, case['preferences'], self.snapshot),
                    )
                ),
            )
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    def test_missing_engine_and_bad_identity_do_not_cache_or_fall_back(self):
        case = self.data['cases'][0]
        record = EchoRecord.from_dict(case['record'])
        jobs = ComputeJobs(ROOT / 'build/missing-p10-engine.exe')
        service = RecommendationService(compute=jobs)
        try:
            with self.assertRaises(ComputeFailure):
                service.compare(record, case['preferences'], self.snapshot)
            self.assertIsNone(service._result)
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())

        class WrongIdentity:
            def submit(self, _, build, **options):
                from concurrent.futures import Future

                value = build()
                future = Future()
                future.set_result(
                    dict(
                        mode=value['mode'],
                        profile_id='wrong',
                        weight_version=value['weight_version'],
                        rows=[],
                        message='',
                    )
                )
                return future

        service = RecommendationService(compute=WrongIdentity())
        with self.assertRaises(ComputeFailure) as failure:
            service.compare(record, case['preferences'], self.snapshot)
        self.assertEqual('request_identity', failure.exception.kind)
        self.assertIsNone(service._result)

        class WrongRows:
            def submit(self, _, build, **options):
                from concurrent.futures import Future

                value = build()
                future = Future()
                future.set_result(
                    dict(
                        mode=value['mode'],
                        profile_id=value['profile_id'],
                        weight_version=value['weight_version'],
                        rows=[{}],
                        message='',
                    )
                )
                return future

        service = RecommendationService(compute=WrongRows())
        with self.assertRaises(ComputeFailure) as failure:
            service.compare(record, case['preferences'], self.snapshot)
        self.assertEqual('protocol', failure.exception.kind)
        self.assertIsNone(service._result)


if __name__ == '__main__':
    unittest.main()
