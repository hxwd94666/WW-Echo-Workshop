# 验证同次原生复核返回质量结论、冻结展示口径及失败不回退。
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import asdict, replace
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, PropertyMock, patch

from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.record_quality import quality_for
from echo_workshop.application.view_state import review_checked_reason
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import EchoRecord, canonical_json
from echo_workshop.ui.inventory.card_data import checked_protection_text, project_checked_card
from tests.native_runtime import _jobs

FIXTURE = Path(__file__).with_name('fixtures') / 'review_quality.json'


class ReviewQuality(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']
        self.rows = tuple(
            (EchoRecord.from_dict(case['record']), case['kind'], ()) for case in self.cases
        )
        self.service = InventoryQueryService(compute=_jobs)

    def test_native_quality_and_display_match_frozen_contract_in_one_existing_request(self):
        with ExitStack() as stack:
            for name in ('validation_issues', 'usable'):
                stack.enter_context(
                    patch.object(
                        EchoRecord,
                        name,
                        new_callable=PropertyMock,
                        side_effect=AssertionError('retired Python quality: ' + name),
                    )
                )
            submit = stack.enter_context(patch.object(_jobs, 'submit', wraps=_jobs.submit))
            rows, _, quality = self.service.review(self.rows)
            self.assertEqual(1, submit.call_count)
            self.assertEqual('inventory_query', submit.call_args.args[0])
            for row, fact, case in zip(rows, quality, self.cases):
                record, kind, _ = row
                self.assertIs(record, fact.record)
                self.assertEqual(tuple(case['issues']), fact.issues)
                self.assertIs(case['usable'], fact.usable)
                self.assertEqual(
                    canonical_json(case['card']),
                    canonical_json(asdict(project_checked_card(record, fact, kind))),
                )
                self.assertEqual(case['reason'], review_checked_reason(record, fact, kind))
                self.assertEqual(
                    case['protection'],
                    checked_protection_text(record, fact, '已预留：测试角色-模版（待复核）'),
                )
            selected, _, facts = self.service.review(self.rows, mode=1)
            expected = [
                (row, case)
                for row, case in zip(self.rows, self.cases)
                if row[1] != 'item' or not case['usable']
            ]
            self.assertEqual(len(expected), len(selected))
            for (row, case), actual, fact in zip(expected, selected, facts):
                self.assertIs(row, actual)
                self.assertIs(row[0], fact.record)
                self.assertEqual(tuple(case['issues']), fact.issues)
            self.assertEqual(2, submit.call_count)

    def test_record_identity_is_exact_and_bad_quality_reply_publishes_nothing(self):
        row = self.rows[0]
        rows, _, facts = self.service.review((row,))
        with self.assertRaises(ValueError):
            quality_for(replace(rows[0][0], note='changed'), facts[0])

        for mutation in ('missing', 'wrong_uuid', 'bad_flag', 'bad_issues'):

            def submit(operation, build, **options):
                payload = build()
                response = _jobs.submit(operation, lambda: payload, **options).result(timeout=12)
                if mutation == 'missing':
                    response['quality'] = []
                elif mutation == 'wrong_uuid':
                    response['quality'][0]['item_id'] = self.rows[1][0].item_id
                elif mutation == 'bad_flag':
                    response['quality'][0]['usable'] = 1
                else:
                    response['quality'][0]['issues'] = [None]
                future = Future()
                future.set_result(response)
                return future

            with self.subTest(mutation=mutation), self.assertRaises(ComputeFailure):
                InventoryQueryService(compute=Mock(submit=submit)).review((row,))

    def test_native_failure_and_pre_submission_cancel_never_use_python_quality(self):
        future = Future()
        future.set_exception(ComputeFailure('timeout', 'quality test failure'))
        compute = Mock()
        compute.submit.return_value = future
        service = InventoryQueryService(compute=compute)
        with patch.object(
            EchoRecord,
            'validation_issues',
            new_callable=PropertyMock,
            side_effect=AssertionError('Python fallback'),
        ):
            with (
                self.assertRaises(ComputeFailure),
                self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'),
            ):
                service.review(self.rows)
            cancel = threading.Event()
            cancel.set()
            compute.submit.reset_mock()
            with self.assertRaises(OperationCancelled):
                service.review(self.rows, cancel_event=cancel)
            compute.submit.assert_not_called()
