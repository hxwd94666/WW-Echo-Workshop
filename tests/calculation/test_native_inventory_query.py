# 验证库存原生筛选统计、时间精度、行映射及后台取消边界。
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.inventory import InventoryReader
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.plan_jobs import PlanCommands
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import EchoRecord, canonical_json
from tests.native_runtime import _jobs

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name('fixtures') / 'native_inventory_query.json'
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


class ShiftTimezone(tzinfo):
    def utcoffset(self, value):
        return timedelta(hours=2 if value.month >= 3 else 1)

    def dst(self, value):
        return timedelta(0)


class NativeInventoryQuery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def setUp(self):
        self.service = InventoryQueryService(compute=_jobs)
        self.records = tuple(EchoRecord.from_dict(row) for row in self.data['records'])
        self.now = datetime.fromisoformat(self.data['now'])

    def assert_selected(self, source, expected, actual):
        self.assertEqual(len(expected), len(actual))
        for index, value in zip(expected, actual):
            self.assertIs(source[index], value)

    def test_all_frozen_matching_and_review_cases_without_python_selection_or_validation(self):
        before = canonical_json(self.data)
        rows = tuple(
            (record, kind, object()) for record, kind in zip(self.records, self.data['row_kinds'])
        )
        with ExitStack() as stack:
            for name in ('validation_issues', 'usable', 'fully_tuned'):
                stack.enter_context(
                    patch.object(
                        EchoRecord,
                        name,
                        new_callable=PropertyMock,
                        side_effect=AssertionError(name),
                    )
                )
            stack.enter_context(
                patch(
                    'echo_workshop.resources.search.search_matches',
                    side_effect=AssertionError('Python selected records'),
                )
            )
            for case in self.data['matching_cases']:
                with self.subTest(options=case['options']):
                    options = dict(case['options'])
                    options['protected_ids'] = self.data['protected_ids']
                    actual = self.service.filter_records(self.records, now=self.now, **options)
                    self.assert_selected(self.records, case['indices'], actual)
            for case in self.data['review_cases']:
                with self.subTest(review=case):
                    actual, counts, quality = self.service.review(
                        rows, mode=case['mode'], fully_tuned=case['fully_tuned']
                    )
                    self.assert_selected(rows, case['indices'], actual)
                    self.assertEqual(canonical_json(case['counts']), canonical_json(counts))
                    self.assertEqual(len(actual), len(quality))
                    for row, fact in zip(actual, quality):
                        self.assertIs(row[0], fact.record)
        self.assertEqual(before, canonical_json(self.data))

    def test_fractional_microseconds_and_dynamic_timezone_wall_boundary_are_frozen(self):
        records = tuple(EchoRecord.from_dict(row) for row in self.data['precision_records'])
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            for case in self.data['precision_cases']:
                actual = self.service.filter_records(records, now=self.now, **case['options'])
                self.assert_selected(records, case['indices'], actual)
            self.assertEqual(['inventory_query'] * 6, [c.args[0] for c in submit.call_args_list])
        case = self.data['dynamic_timezone_case']
        records = tuple(EchoRecord.from_dict(row) for row in case['records'])
        now = datetime.fromisoformat(case['now_wall']).replace(tzinfo=ShiftTimezone())
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            actual = self.service.filter_records(records, now=now, **case['options'])
        self.assert_selected(records, case['indices'], actual)
        self.assertEqual(
            ['inventory_clock', 'inventory_query'], [c.args[0] for c in submit.call_args_list]
        )

    def test_typed_errors_keep_healthy_process_and_unknown_level_is_retained_for_review(self):
        self.service.filter_records((), now=self.now)
        child = _jobs._workers['interactive']._process
        unknown = replace(self.records[0], level=None)
        with self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'):
            with self.assertRaises(TypeError):
                self.service.filter_records((unknown,), now=self.now, cost=4)
            for now, age in ((self.now, 1e9), (datetime.min.replace(tzinfo=timezone.utc), 1)):
                with self.assertRaises(OverflowError):
                    self.service.filter_records((), now=now, max_age_days=age)
        rows = ((unknown, 'item', None),)
        actual, counts, quality = self.service.review(rows, mode=1)
        self.assertEqual(
            ('missing:level',),
            tuple(issue for issue in quality[0].issues if issue == 'missing:level'),
        )
        self.assert_selected(rows, [0], actual)
        self.assertEqual(dict(saved=1, pending=1, confirmed=0, eligible=0), counts)
        self.assertIs(child, _jobs._workers['interactive']._process)
        self.assertIsNone(child.poll())
        self.assertEqual(0, _jobs._failures['interactive'])

    def test_duplicate_positions_and_explicit_input_limit_do_not_truncate_or_deduplicate(self):
        record = self.records[0]
        records = (record,) * 26
        self.assert_selected(
            records, list(range(26)), self.service.filter_records(records, now=self.now)
        )
        with patch.object(_jobs, 'submit') as submit:
            for method, args in (
                (self.service.filter_records, ((record,) * 20001,)),
                (self.service.review, (((record, 'item', None),) * 20001,)),
            ):
                with self.assertRaises(ComputeFailure) as caught:
                    method(*args)
                self.assertEqual('input_limit', caught.exception.kind)
        submit.assert_not_called()

    def test_invalid_reply_identity_order_uuid_and_statistics_publish_nothing(self):
        class Replies:
            def __init__(self, damage):
                self.damage = damage

            def submit(self, operation, build, **kwargs):
                payload = build()
                value = dict(
                    query_id=payload['query_id'],
                    mode='matching',
                    snapshot_id=payload['snapshot_id'],
                    snapshot_cached=False,
                    indices=[0],
                    item_ids=[payload['rows'][0]['record']['item_id']],
                    counts=dict(saved=1, pending=0, confirmed=1, eligible=1),
                )
                self.damage(value)
                future = Future()
                future.set_result(value)
                return future

        damages = (
            lambda value: value.update(query_id='stale'),
            lambda value: value.update(snapshot_id='stale'),
            lambda value: value.update(snapshot_cached=1),
            lambda value: value.update(mode='review'),
            lambda value: value.update(indices=[True]),
            lambda value: value.update(indices=[1]),
            lambda value: value.update(item_ids=['wrong']),
            lambda value: value.update(indices=[0, 0], item_ids=value['item_ids'] * 2),
            lambda value: value.update(counts=dict(saved=1, pending=True, confirmed=1, eligible=1)),
            lambda value: value.update(counts=dict(saved=1, pending=1, confirmed=1, eligible=1)),
            lambda value: value.update(counts=dict(saved=1, pending=1, confirmed=0, eligible=1)),
        )
        with self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'):
            for damage in damages:
                with self.subTest(damage=damage), self.assertRaises(ComputeFailure):
                    InventoryQueryService(compute=Replies(damage)).filter_records(
                        self.records[:1], now=self.now
                    )

    def test_reader_maps_snapshot_identity_counts_and_latest_request_only(self):
        rows = tuple(
            (record, kind, None) for record, kind in zip(self.records, self.data['row_kinds'])
        )
        repository = Mock()
        entered, release = threading.Event(), threading.Event()

        def snapshot(profile, **kwargs):
            if profile == 'old':
                entered.set()
                self.assertTrue(release.wait(2))
            return 27, rows, {'claim': 'owner'}

        repository.review_snapshot.side_effect = snapshot
        reader = InventoryReader(repository, queries=self.service)
        try:
            reader.request('old', 0)
            self.assertTrue(entered.wait(2))
            generation = reader.request('current', 1)
            release.set()
            deadline = time.monotonic() + 3
            while reader.status.state == 'running' and time.monotonic() < deadline:
                time.sleep(0.005)
            status = reader.status
            self.assertEqual('ready', status.state, status.message)
            self.assertEqual(generation, status.generation)
            result = status.result
            self.assertEqual(
                ('current', 27, (('claim', 'owner'),)),
                (result.profile_id, result.revision, result.claims),
            )
            case = next(c for c in self.data['review_cases'] if c['mode'] == 1 and c['fully_tuned'])
            self.assert_selected(rows, case['indices'], result.rows)
            self.assertEqual(case['counts']['pending'], result.pending_count)
            self.assertEqual(len(result.rows), len(result.quality))
            for row, fact in zip(result.rows, result.quality):
                self.assertIs(row[0], fact.record)
        finally:
            release.set()
            reader.close()

    def test_reader_cancel_reaches_live_hung_child_and_returns_before_reaping(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)
        jobs.submit('ping', lambda: {'warm': True}).result(timeout=5)
        child = jobs._workers['interactive']._process
        entered = threading.Event()

        class Hang:
            def submit(self, operation, build, **kwargs):
                future = jobs.submit('fault_hang', build, **kwargs)
                entered.set()
                return future

        repository = Mock()
        repository.review_snapshot.return_value = (7, ((self.records[0], 'item', None),), {})
        reader = InventoryReader(repository, queries=InventoryQueryService(compute=Hang()))
        try:
            reader.request('profile', 0)
            self.assertTrue(entered.wait(2))
            time.sleep(0.05)
            self.assertIsNone(child.poll())
            at = time.monotonic()
            reader.close()
            self.assertLess(time.monotonic() - at, 0.5)
            self.assertEqual('closed', reader.status.state)
            self.assertIsNone(reader.status.result)
        finally:
            reader.close()
            jobs.close()
            self.assertTrue(jobs.wait_closed())
        self.assertIsNotNone(child.poll())

    def test_cancel_before_submission_and_explicit_plan_precheck_query_dependency(self):
        event = threading.Event()
        event.set()
        with patch.object(_jobs, 'submit') as submit, self.assertRaises(OperationCancelled):
            self.service.filter_records(self.records, now=self.now, cancel_event=event)
        submit.assert_not_called()
        snapshot = SimpleNamespace(version='weights')
        request = SimpleNamespace(profile_id=self.records[0].profile_id, version='request')
        reservations = SimpleNamespace(records=self.records, inventory_revision=9)
        repository = Mock()
        repository.loadout_snapshot.return_value = reservations
        allocation = Mock()
        allocation.precheck.return_value = {}
        commands = PlanCommands(None, repository, allocation=allocation, queries=self.service)
        result = commands.execute(
            'precheck',
            dict(request=request, snapshot=snapshot),
            candidate_filters=dict(cost=1, now=self.now),
            scope_generation=17,
        )
        selected = allocation.precheck.call_args.args[0]
        self.assertEqual(
            [
                r.item_id
                for r in self.records
                if r.cost == 1 and r.review == 'confirmed' and not r.issues
            ],
            [r.item_id for r in selected],
        )
        self.assertEqual(17, result.payload[-1]['scope_generation'])
        with self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'), self.assertRaises(ValueError):
            PlanCommands(None, repository, allocation=allocation).execute(
                'precheck', dict(request=request, snapshot=snapshot), candidate_filters={}
            )

    def test_retired_python_backend_functions_are_deleted(self):
        from echo_workshop.application import inventory, view_state
        from echo_workshop.application.matching import filtering

        self.assertFalse(hasattr(filtering, 'filter_records'))
        self.assertFalse(hasattr(inventory, 'filter_review_rows'))
        self.assertFalse(hasattr(view_state, 'review_counts'))
