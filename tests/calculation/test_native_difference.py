# 对照冻结差异证据验证 Rust 求值、最小输入投影和取消边界。
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.loadout_actions import result_from_payload
from echo_workshop.application.loadout_diff import plan_changes
from echo_workshop.application.scoring import ScoreService
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.loadout import LoadoutSnapshot
from echo_workshop.core.models import EchoRecord
from echo_workshop.resources.weights import WeightSnapshot

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def models(case, snapshot):
    result = result_from_payload({'result': case['result'], 'request': case['result']['request']})
    inventory = dict(case['inventory'])
    inventory['records'] = tuple(EchoRecord.from_dict(row) for row in inventory['records'])
    inventory['current'] = tuple(inventory['current'])
    inventory['claims'] = tuple(tuple(row) for row in inventory['claims'])
    return result, LoadoutSnapshot(**inventory), snapshot


def evidence(row):
    def grade(value):
        return asdict(value) if value is not None else None

    def item(value):
        return {
            'item_id': value.item_id,
            'score_cents': value.score_cents,
            'rating': grade(value.rating),
        }

    data = {
        key: getattr(row, key)
        for key in (
            'key',
            'label',
            'state',
            'notice',
            'old_plan_id',
            'old_total_cents',
            'new_total_cents',
            'delta_cents',
            'retained_count',
        )
    }
    data.update(
        old_items=[item(v) for v in row.old_items],
        new_items=[item(v) for v in row.new_items],
        old_rating=grade(row.old_rating),
        new_rating=grade(row.new_rating),
        pairs=[
            {
                'kind': v.kind,
                'old_id': v.old.item_id if v.old else None,
                'new_id': v.new.item_id if v.new else None,
                'delta_cents': v.delta_cents,
            }
            for v in row.pairs
        ],
    )
    return json.loads(json.dumps(data))


class TestNativeDifference(unittest.TestCase):
    def test_large_frozen_cent_scores_are_not_confused_with_per_entry_limit(self):
        result, inventory, snapshot = models(self.fixture['cases'][0], self.snapshot)
        limit = 100_000_000_000_000 * 7
        row = replace(result.roles[0], scores=(limit,) * 5, total_cents=limit * 5)
        result = replace(result, roles=(row,))
        change = plan_changes(result, replace(inventory, current=()), snapshot, self.scores)[0]
        self.assertEqual(limit * 5, change.new_total_cents)
        self.assertEqual('SSS', change.new_rating.label)
        self.assertTrue(all(item.rating is None for item in change.new_items))

    def setUp(self):
        self.fixture = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_difference.json').read_text(encoding='utf-8')
        )
        self.snapshot = WeightSnapshot.from_dict(self.fixture['snapshot'])
        self.jobs = ComputeJobs(ENGINE)
        self.addCleanup(self.jobs.wait_closed)
        self.addCleanup(self.jobs.close)
        self.scores = ScoreService(SimpleNamespace(active=self.snapshot), compute=self.jobs)

    def test_all_six_frozen_outputs_without_python_scoring_grading_or_comparison(self):
        with (
            patch.object(self.scores, 'compare', side_effect=AssertionError('Python comparison')),
            patch(
                'echo_workshop.core.scoring.calculate_echo_score',
                side_effect=AssertionError('Python score'),
                create=True,
            ),
        ):
            for case in self.fixture['cases']:
                result, inventory, snapshot = models(case, self.snapshot)
                row = plan_changes(result, inventory, snapshot, self.scores)[0]
                with self.subTest(case=case['name']):
                    self.assertEqual(case['expected'], evidence(row))
                    expected = {'comparable': 6, 'reordered': 5}.get(case['name'], 0)
                    self.assertEqual(expected, row.evaluated_records)
        print(
            'RUST_DIFF_GOLDEN_OK cases=6 exact_totals_grades_tooltips_pairs=True Python_evaluators=0'
        )

    def test_only_requested_records_are_sent_from_large_inventory(self):
        case = self.fixture['cases'][0]
        result, inventory, snapshot = models(case, self.snapshot)
        base = inventory.records[0]
        extra = tuple(
            replace(base, item_id=str(UUID(int=5000 + i)), name=f'无关样本{i}') for i in range(1000)
        )
        inventory = replace(inventory, records=inventory.records + extra)
        submitted = []
        original = self.jobs.submit

        def submit(operation, builder, **options):
            def payload():
                data = builder()
                submitted.append(data)
                return data

            return original(operation, payload, **options)

        with patch.object(self.jobs, 'submit', side_effect=submit):
            row = plan_changes(result, inventory, snapshot, self.scores)[0]
        self.assertEqual(case['expected'], evidence(row))
        self.assertEqual(1, len(submitted))
        self.assertEqual(6, len(submitted[0]['records']))
        self.assertEqual(1006, len(inventory.records))

    def test_pending_record_keeps_new_frozen_evidence_without_fabricating_old_total(self):
        result, inventory, snapshot = models(self.fixture['cases'][0], self.snapshot)
        changed = replace(inventory.records[0], review='pending')
        inventory = replace(inventory, records=(changed, *inventory.records[1:]))
        row = plan_changes(result, inventory, snapshot, self.scores)[0]
        self.assertEqual('old_unavailable', row.state)
        self.assertIsNone(row.old_total_cents)
        self.assertIsNone(row.delta_cents)
        self.assertEqual(result.roles[0].total_cents, row.new_total_cents)
        self.assertTrue(all(item.score_cents is None for item in row.old_items))

    def test_cross_profile_and_duplicate_inventory_are_errors_not_silent_projection(self):
        result, inventory, snapshot = models(self.fixture['cases'][0], self.snapshot)
        for records in (
            inventory.records + (inventory.records[0],),
            (replace(inventory.records[0], profile_id=str(UUID(int=444))), *inventory.records[1:]),
        ):
            with self.assertRaises(ValueError):
                plan_changes(result, replace(inventory, records=records), snapshot, self.scores)

    def test_cancel_does_not_publish_partial_changes(self):
        result, inventory, snapshot = models(self.fixture['cases'][0], self.snapshot)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            plan_changes(result, inventory, snapshot, self.scores, cancel)
        self.assertEqual({}, self.jobs._workers)
