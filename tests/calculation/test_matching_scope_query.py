# 验证原生库存查询同时返回选择范围和空候选原因，保留旧反馈优先级。
import ast
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.matching.feedback import (
    feedback_from_code,
    MatchRequirementsError,
    validate_match_requirements,
)
from echo_workshop.application.operations import OperationCancelled
from echo_workshop.application.pins import prepare_pin_catalog
from echo_workshop.core.models import EchoRecord, StatValue
from echo_workshop.resources.weights import WeightSnapshot
from tests.native_runtime import _jobs

ROOT = Path(__file__).resolve().parents[2]
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

    def test_actual_candidate_worker_publishes_native_feedback_without_rescoring(self):
        # Execute the actual catalogue-only worker body; this is not a real Qt/host gate.
        source = ROOT / 'src/echo_workshop/ui/matching/controller.py'
        tree = ast.parse(source.read_text('utf-8'))
        controller = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ComparisonContent'
        )
        start = next(
            n
            for n in controller.body
            if isinstance(n, ast.FunctionDef) and n.name == '_start_comparison'
        )
        calculate = next(
            n for n in start.body if isinstance(n, ast.FunctionDef) and n.name == 'calculate'
        )
        payload_type = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == '_ComparisonPayload'
        )
        case = json.loads(FIXTURE.read_text('utf-8'))['cases'][5]
        records = records_for(case)
        queries = InventoryQueryService(compute=_jobs)
        completed = Mock()
        inventory = SimpleNamespace(inventory_snapshot=Mock(return_value=(7, records, {})))
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                inventory=inventory,
                inventory_queries=queries,
                scores=Mock(),
            ),
            _signals=SimpleNamespace(completed=SimpleNamespace(emit=completed)),
        )
        env = dict(
            __name__=__name__,
            self=owner,
            profile=records[0].profile_id,
            requirements={},
            snapshot=WeightSnapshot.builtin(),
            stop=threading.Event(),
            selected_ids=case['selected_ids'],
            filters={},
            rules={},
            selections=(('1304', 'default'),),
            _catalogue_only=True,
            generation=3,
            defaults_version='frozen',
            prepare_pin_catalog=prepare_pin_catalog,
            feedback_from_code=feedback_from_code,
            validate_match_requirements=validate_match_requirements,
            ComparisonCancelled=OperationCancelled,
            OperationCancelled=OperationCancelled,
            MatchRequirementsError=MatchRequirementsError,
        )
        exec(
            compile(
                ast.Module(body=[payload_type, calculate], type_ignores=[]), str(source), 'exec'
            ),
            env,
        )
        env['calculate']()
        completed.assert_called_once()
        generation, payload, error = completed.call_args.args
        self.assertEqual((3, ''), (generation, error))
        self.assertEqual((), payload[3])
        self.assertEqual(case['feedback']['text'], payload.feedback.text)
        owner.runtime.scores.compare.assert_not_called()
