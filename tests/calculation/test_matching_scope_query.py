# 验证原生库存查询同时返回选择范围和空候选原因，保留旧反馈优先级。
import copy
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ww_echo_workshop.application.compute_protocol import ComputeFailure
from ww_echo_workshop.application.inventory_query import InventoryQueryService
from ww_echo_workshop.application.matching.feedback import (
    feedback_from_code,
)
from ww_echo_workshop.application.operations import OperationCancelled
from ww_echo_workshop.application.match_projection import MatchProjectionService
from ww_echo_workshop.application.matching.jobs import MatchingRequest, MatchingService
from ww_echo_workshop.core.compatibility import CompatibilityRule
from ww_echo_workshop.core.models import EchoRecord, StatValue
from ww_echo_workshop.resources.weights import WeightSnapshot
from tests.calculation.test_quick_quality import record_fixture
from tests.native_runtime import ScoreService, _jobs, pin_checks

FIXTURE = Path(__file__).with_name('fixtures') / 'matching_scope.json'


def records_for(case):
    result = []
    for raw in case['records']:
        row = copy.deepcopy(raw)
        for field in ('main_stats', 'sub_stats'):
            row[field] = tuple(StatValue(**value) for value in row[field])
        row['issues'] = tuple(row['issues'])
        result.append(EchoRecord(**row))
    return tuple(result)


class TestMatchingScopeQuery(unittest.TestCase):
    def test_frozen_feedback_order_explicit_ids_and_filter_groups(self):
        cases = json.loads(FIXTURE.read_text('utf-8'))['cases']
        queries = InventoryQueryService(compute=_jobs)

        def python_quality(_):
            raise AssertionError('Python quality predicate was called')

        with (
            patch.object(EchoRecord, 'usable', property(python_quality)),
            patch.object(EchoRecord, 'validation_issues', property(python_quality)),
        ):
            for case in cases:
                with self.subTest(case=case['name']):
                    result = queries.matching_scope(
                        records_for(case), selected_ids=case['selected_ids'], **case['filters']
                    )
                    self.assertEqual(case['catalog_ids'], [row.item_id for row in result.catalog])
                    self.assertEqual(case['record_ids'], [row.item_id for row in result.records])
                    feedback = feedback_from_code(result.feedback_code)
                    actual = (
                        None
                        if feedback is None
                        else dict(
                            text=feedback.text, action=feedback.action, role_key=feedback.role_key
                        )
                    )
                    self.assertEqual(case['feedback'], actual)
        print('NATIVE_MATCH_SCOPE_FROZEN_OK:', len(cases))

    def test_scope_feedback_uses_one_existing_inventory_query(self):
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][3]
        records = records_for(case)
        queries = InventoryQueryService(compute=_jobs)
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            result = queries.matching_scope(records, selected_ids=(records[-1].item_id,))
        submit.assert_called_once()
        self.assertEqual('inventory_query', submit.call_args.args[0])
        self.assertEqual((records[-1],), result.records)

    def test_scope_result_rejects_missing_broadened_swapped_ids_and_unknown_codes(self):
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][3]
        rows = tuple((record, 'item', None) for record in records_for(case))
        scope = dict(selected_ids=(rows[1][0].item_id,), text='')
        valid = dict(indices=[1], item_ids=[rows[1][0].item_id], feedback='')
        for bad in (
            dict(valid, indices=[], item_ids=[]),
            dict(valid, indices=[0, 1], item_ids=[row[0].item_id for row in rows]),
            dict(valid, item_ids=[rows[0][0].item_id]),
            dict(valid, feedback='invented'),
        ):
            with self.subTest(bad=bad), self.assertRaises(ComputeFailure):
                InventoryQueryService._scope_details(bad, rows, [0, 1], scope, None)
        with self.assertRaises(ComputeFailure):
            feedback_from_code('invented')

    def test_cancelled_scope_never_publishes_and_next_query_is_healthy(self):
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][3]
        queries = InventoryQueryService(compute=_jobs)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            queries.matching_scope(records_for(case), cancel_event=cancel)
        self.assertEqual('', queries.matching_scope(records_for(case)).feedback_code)
        with self.assertRaises(ValueError):
            queries.matching_scope(records_for(case), selected_ids='not-an-id-set')

    def _matching_job(self, records, *, scores=None, stop=None):
        inventory = SimpleNamespace(inventory_snapshot=Mock(return_value=(7, records, {})))
        queries = InventoryQueryService(compute=_jobs)
        snapshot = WeightSnapshot.builtin()
        service = MatchingService(
            inventory,
            queries,
            scores if scores is not None else ScoreService(SimpleNamespace(active=snapshot)),
            MatchProjectionService(compute=_jobs),
            pin_checks,
            stop if stop is not None else threading.Event(),
        )
        return service, inventory, snapshot

    def test_actual_candidate_job_returns_native_feedback_without_scoring(self):
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][5]
        records = records_for(case)
        scores = Mock()
        service, inventory, snapshot = self._matching_job(records, scores=scores)
        request = MatchingRequest(
            records[0].profile_id,
            snapshot,
            'frozen',
            (('1304', 'default'),),
            case['selected_ids'],
            {},
            {},
            {},
            catalogue_only=True,
        )
        result = service.run(request)
        inventory.inventory_snapshot.assert_called_once()
        self.assertIs(request, result.request)
        self.assertEqual(
            (7, (), (), None), (result.revision, result.records, result.scores, result.projections)
        )
        self.assertEqual(case['feedback']['text'], result.feedback.text)
        scores.compare.assert_not_called()

    def test_explicit_job_freezes_ids_and_preserves_native_order_and_score(self):
        original = record_fixture()
        prefix = '22222222-2222-4222-8222-'
        late, early, excluded = (
            replace(original, item_id=prefix + f'{index:012}') for index in (3, 1, 2)
        )
        service, _, snapshot = self._matching_job((late, early, excluded))
        key = ('1304', 'default')
        ids = [late.item_id, early.item_id]
        filters = {'text': ''}
        request = MatchingRequest(
            original.profile_id,
            snapshot,
            'frozen',
            (key,),
            ids,
            filters,
            {key: CompatibilityRule()},
            {},
        )
        ids[:] = [excluded.item_id]
        filters['text'] = '不再冻结输入内'
        result = service.run(request)
        self.assertEqual((late, early), result.records)
        self.assertEqual((late, early, excluded), result.catalog)
        self.assertEqual([29.96, 29.96], [cell.score.current_score for cell in result.scores])
        self.assertEqual(['B', 'B'], [cell.score.rating.label for cell in result.scores])
        self.assertTrue(result.projections.matches(result.records, (key,), result.scores))
        self.assertEqual(
            [early.item_id, late.item_id],
            [result.records[entry[0]].item_id for entry in result.projections.views[0].entries],
        )
        print('MATCH_JOB_FROZEN_OK: 29.96/B, source rows preserved, ties ordered by UUID')

    def test_cancelled_job_does_not_read_or_publish(self):
        original = record_fixture()
        stop = threading.Event()
        service, inventory, snapshot = self._matching_job((original,), stop=stop)
        request = MatchingRequest(original.profile_id, snapshot, '', (), None, {}, {}, {}, True)
        stop.set()
        with self.assertRaises(OperationCancelled):
            service.run(request)
        inventory.inventory_snapshot.assert_not_called()
        stop.clear()
        cancel = threading.Event()
        inventory.inventory_snapshot.side_effect = lambda *args, **kwargs: (
            cancel.set() or (7, (original,), {})
        )
        with self.assertRaises(OperationCancelled):
            service.run(request, cancel_event=cancel)
        inventory.inventory_snapshot.assert_called_once()
