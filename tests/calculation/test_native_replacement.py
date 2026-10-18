# 验证单槽替换冻结语义、原生批量求值和不信任候选缓存的预览边界。
from contextlib import ExitStack
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from echo_workshop.application.allocation import AllocationService
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.loadout_actions import clone_loadout_payload, source_payload
from echo_workshop.application.replacement import replacement_candidates, replacement_preview
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.allocation.models import RoleAllocation, PRIORITY_POLICY_VERSION
from echo_workshop.core.loadout import LoadoutSnapshot
from echo_workshop.core.models import EchoRecord, canonical_json
from echo_workshop.storage.plans import validate_plan

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def options_value(options):
    data = asdict(options)
    data.pop('source_json')
    data['old_record'] = options.old_record.item_id
    data['records'] = [r.item_id for r in options.records]
    return json.loads(json.dumps(data))


def preview_value(preview):
    data = preview.result.to_dict()
    data.pop('plan_id')
    return json.loads(
        json.dumps(
            dict(
                result=data,
                records=[r.item_id for r in preview.records],
                snapshot_version=preview.snapshot.version,
                source_plan_id=preview.source_plan_id,
                character_id=preview.character_id,
                slot_index=preview.slot_index,
                item_id=preview.item_id,
                inventory_revision=preview.inventory_revision,
                loadout_revision=preview.loadout_revision,
            )
        )
    )


class TestNativeReplacement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_replacement.json').read_text(
                encoding='utf-8'
            )
        )

    def setUp(self):
        self.jobs = ComputeJobs(ENGINE)
        self.addCleanup(self.jobs.wait_closed)
        self.addCleanup(self.jobs.close)
        self.service = AllocationService(None, compute=self.jobs)

    def case(self, index=0):
        case = self.fixture['cases'][index]
        payload = copy.deepcopy(case['source'])
        payload['weight_snapshot'] = self.fixture['weight_snapshot']
        live = LoadoutSnapshot(
            payload['request']['profile_id'],
            case['inventory_revision'],
            case['loadout_revision'],
            tuple(case['current']),
            tuple(tuple(row) for row in case['claims']),
            tuple(EchoRecord.from_dict(row) for row in case['records']),
        )
        return case, payload, live

    def candidates(self, payload, live):
        return replacement_candidates(payload, live, '1304', 0, plan_evaluator=self.service)

    def test_seven_frozen_option_and_preview_outputs_without_python_business_evaluation(self):
        forbidden = (
            'echo_workshop.core.scoring.calculate_echo_score',
            'echo_workshop.core.scoring.prepare_score',
            'echo_workshop.core.scoring.score_prepared',
            'echo_workshop.core.rating.single_rating',
            'echo_workshop.core.rating.loadout_rating',
            'echo_workshop.core.compatibility.compatibility',
        )
        with ExitStack() as stack:
            mocks = [
                stack.enter_context(patch(name, side_effect=AssertionError(name), create=True))
                for name in forbidden
            ]
            for index in range(len(self.fixture['cases'])):
                case, payload, live = self.case(index)
                before = copy.deepcopy(payload)
                with self.subTest(case=case['name']):
                    options = self.candidates(payload, live)
                    self.assertEqual(case['expected'], options_value(options))
                    self.assertEqual(
                        canonical_json(case['expected']), canonical_json(options_value(options))
                    )
                    if 'selected' in case:
                        preview = replacement_preview(
                            options, case['selected'], live, plan_evaluator=self.service
                        )
                        self.assertEqual(case['preview'], preview_value(preview))
                        self.assertEqual(
                            canonical_json(case['preview']), canonical_json(preview_value(preview))
                        )
                        self.assertNotEqual(preview.result.plan_id, payload['plan_id'])
                        frozen = source_payload(
                            dict(
                                profile_id=live.profile_id,
                                result=preview.result,
                                records=preview.records,
                                snapshot=preview.snapshot,
                            ),
                            plan_evaluator=self.service,
                        )
                        self.assertEqual(frozen, validate_plan(frozen, plan_evaluator=self.service))
                    self.assertEqual(before, payload)
            for mock in mocks:
                mock.assert_not_called()
        print(
            'NATIVE_REPLACEMENT_GOLDEN_OK cases=7 exact_scores_grades_reasons_sort_previews=True Python_evaluators=0'
        )

    def test_native_preview_rechecks_forged_available_and_score_fields(self):
        _, payload, live = self.case()
        options = self.candidates(payload, live)
        blocked = next(row for row in options.candidates if '保护' in row.reason)
        forged = replace(
            options,
            candidates=tuple(
                replace(row, available=True, current_cents=999999)
                if row.item_id == blocked.item_id
                else row
                for row in options.candidates
            ),
        )
        with self.assertRaisesRegex(ValueError, '保护'):
            replacement_preview(forged, blocked.item_id, live, plan_evaluator=self.service)
        valid = next(row for row in options.candidates if row.available)
        spoofed = replace(
            options,
            candidates=tuple(
                replace(row, current_cents=999999, delta_cents=999999)
                if row.item_id == valid.item_id
                else row
                for row in options.candidates
            ),
        )
        preview = replacement_preview(spoofed, valid.item_id, live, plan_evaluator=self.service)
        self.assertEqual(valid.current_cents, preview.result.roles[0].scores[0])
        with self.assertRaisesRegex(ValueError, '来源'):
            replacement_preview(
                replace(options, source_plan_id=str(UUID(int=999999))),
                valid.item_id,
                live,
                plan_evaluator=self.service,
            )

    def test_native_rejects_duplicate_cross_profile_and_changed_target_not_unrelated_role(self):
        _, payload, live = self.case()
        with self.assertRaisesRegex(ValueError, '重复'):
            self.candidates(payload, replace(live, records=(*live.records, live.records[0])))
        bad = replace(live.records[-1], profile_id=str(UUID(int=999999)))
        with self.assertRaisesRegex(ValueError, '跨档案'):
            self.candidates(payload, replace(live, records=(*live.records[:-1], bad)))
        target = payload['result']['roles'][0]['item_ids'][1]
        changed = replace(
            live,
            records=tuple(
                replace(r, name='目标物理内容变化') if r.item_id == target else r
                for r in live.records
            ),
        )
        with self.assertRaisesRegex(ValueError, '原方案声骸'):
            self.candidates(payload, changed)

    def test_cancel_and_gui_guard_do_not_submit_native_work(self):
        _, payload, live = self.case()
        stop = threading.Event()
        stop.set()
        with patch.object(self.jobs, 'submit') as submit:
            with self.assertRaises(OperationCancelled):
                replacement_candidates(
                    payload, live, '1304', 0, plan_evaluator=self.service, cancel_event=stop
                )
            submit.assert_not_called()
        token = object()
        qt = SimpleNamespace(
            QCoreApplication=SimpleNamespace(
                instance=lambda: SimpleNamespace(thread=lambda: token)
            ),
            QThread=SimpleNamespace(currentThread=lambda: token),
        )
        with (
            patch.dict(sys.modules, {'PySide6.QtCore': qt}),
            patch.object(self.jobs, 'submit') as submit,
        ):
            with self.assertRaises(ComputeFailure) as error:
                self.candidates(payload, live)
            self.assertEqual('gui_thread', error.exception.kind)
            submit.assert_not_called()

    def test_large_candidate_pool_is_one_native_batch_and_subset_objective_has_no_python_copy(self):
        _, payload, live = self.case()
        record = live.records[-1]
        extra = tuple(
            replace(
                record, item_id=str(UUID(int=800000 + i)), name=f'批量候选{i}', review='confirmed'
            )
            for i in range(1200)
        )
        live = replace(live, records=(*live.records, *extra))
        with patch.object(self.jobs, 'submit', wraps=self.jobs.submit) as submit:
            options = self.candidates(payload, live)
        operations = [call.args[0] for call in submit.call_args_list]
        self.assertEqual(1, operations.count('replacement_candidates'))
        self.assertEqual(len(live.records) - 5, len(options.candidates))
        captured = []
        actual_submit = self.jobs.submit

        def observed(op, builder, **kwargs):
            def build():
                data = builder()
                if op == 'replace_preview':
                    captured.append(len(data['records']))
                return data

            return actual_submit(op, build, **kwargs)

        with patch.object(self.jobs, 'submit', side_effect=observed):
            chosen = next(row.item_id for row in options.candidates if row.available)
            replacement_preview(options, chosen, live, plan_evaluator=self.service)
        self.assertEqual([6], captured)
        clone = clone_loadout_payload(payload, ('1304',), plan_evaluator=self.service)
        self.assertEqual(1, len(clone['result']['roles']))
        self.assertEqual(clone, validate_plan(clone, plan_evaluator=self.service))
        from echo_workshop.core.allocation import models

        self.assertFalse(hasattr(models, 'objective'))
        self.assertFalse(hasattr(models, 'target_objective'))

    def test_subset_priority_targets_and_incomplete_rows_use_native_objective(self):
        from tests.calculation.allocation_oracle import objective

        _, payload, _ = self.case()
        payload['request']['priority_groups'] = [
            [[r['character_id'], r['modal_id']]] for r in payload['request']['roles']
        ]
        for role, row in zip(payload['request']['roles'], payload['result']['roles']):
            role['target'] = 150
            row['target_cents'] = 15000
        payload['result']['request'] = copy.deepcopy(payload['request'])
        payload['result']['policy_version'] = PRIORITY_POLICY_VERSION
        payload['result'].update(exhaustive=False, stop_reason='manual_preview', nodes=0, elapsed=0)
        row = payload['result']['roles'][1]
        row.update(item_ids=[], scores=[], total_cents=0, moves=0)
        rows = [
            RoleAllocation(
                **{
                    **r,
                    'item_ids': tuple(r['item_ids']),
                    'scores': tuple(r['scores']),
                    'reasons': tuple(r['reasons']),
                }
            )
            for r in payload['result']['roles']
        ]
        payload['result']['objective'] = list(objective(rows))
        for row in rows:
            clone = clone_loadout_payload(payload, (row.character_id,), plan_evaluator=self.service)
            self.assertEqual(list(objective((row,))), clone['result']['objective'])
            self.assertEqual(
                [[[row.character_id, row.modal_id]]], clone['request']['priority_groups']
            )
            self.assertEqual(clone, validate_plan(clone, plan_evaluator=self.service))


if __name__ == '__main__':
    unittest.main()
