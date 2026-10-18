# 验证生产配装门面的真实评分、达标重分配、预检和证据重验通路。
import copy
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import threading
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from echo_workshop.application.allocation import AllocationService
from echo_workshop.application.compute_bridge import plain
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.plan_jobs import PlanCommands
from echo_workshop.application.scoring import ScoreService
from echo_workshop.core.allocation.models import AllocationCancelled, AllocationRequest, SlotRule
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import StatValue
from echo_workshop.resources.weights import WeightSnapshot
from tests.native_runtime import EchoRepository
from tests.native_runtime import AllocationStore
from tests.calculation.helpers import role, tuned

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def target_fixture():
    profile = str(UUID(int=1))
    template = WeightSnapshot.builtin().to_dict()['templates']['1304']['default']
    template['main_props'] = {
        cost: {name: 0 for name in weights} for cost, weights in template['main_props'].items()
    }
    template['main_props']['1']['攻击%'] = 1
    template['sub_props'] = {name: 0 for name in template['sub_props']}
    template['sub_props']['暴击'] = 60
    template['score_max'] = [900, 900, 900]
    characters = [str(10001 + i) for i in range(5)]
    templates = {key: {'default': copy.deepcopy(template)} for key in ('default', *characters)}
    for key, variants in templates.items():
        variants['default']['name'] = f'合成角色-{key}'
    snapshot = WeightSnapshot(templates, {}, source='synthetic-target-fixture-v1')
    records = []
    for i, critical in enumerate([9.9] * 5 + [8.7] * 15 + [8.1] * 5):
        record = tuned(profile, item_id=str(UUID(int=1000 + i)), name=f'目标样本-{i}')
        records.append(
            replace(record, sub_stats=(StatValue('暴击', critical), *record.sub_stats[1:]))
        )
    roles = tuple(
        role(character, target=150, slots=(SlotRule(1),) * 5, cost_budget=5, set_requirements=())
        for character in characters
    )
    return tuple(records), AllocationRequest(profile, roles), snapshot


class TestNativeAllocationService(unittest.TestCase):
    def setUp(self):
        self.jobs = ComputeJobs(ENGINE)
        self.addCleanup(self.jobs.wait_closed)
        self.addCleanup(self.jobs.close)
        self.records, self.request, self.snapshot = target_fixture()
        self.scores = ScoreService(SimpleNamespace(active=self.snapshot), compute=self.jobs)
        self.service = AllocationService(self.scores)

    def solve(self, **options):
        return self.service.solve(
            self.records, self.request, inventory_revision=7, snapshot=self.snapshot, **options
        )

    def test_gui_thread_does_not_submit_or_wait_for_native_search(self):
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
            self.assertRaisesRegex(RuntimeError, '后台'),
        ):
            self.solve()
        submit.assert_not_called()

    def test_real_production_score_to_five_peer_rebalance_and_native_revalidation(self):
        matrix = self.scores.compare(
            self.records,
            [r.key for r in self.request.roles],
            inventory_revision=7,
            rules={r.key: r.rule for r in self.request.roles},
            snapshot=self.snapshot,
        )
        for i in range(5):
            self.assertEqual(
                [34] * 5 + [30] * 15 + [28] * 5, [cell.score.current_score for cell in matrix[i::5]]
            )
        with patch.object(
            self.scores, 'compare', side_effect=AssertionError('Python scoring path')
        ):
            result = self.solve(max_nodes=200000)
        self.assertEqual(5, result.objective[0])
        self.assertTrue(result.exhaustive)
        self.assertTrue(
            all(len(row.item_ids) == 5 and row.total_cents >= 15000 for row in result.roles)
        )
        self.assertEqual(25, len({value for row in result.roles for value in row.item_ids}))
        self.assertEqual(76000, sum(row.total_cents for row in result.roles))
        evidence = plain([asdict(row) for row in result.roles])
        validated = self.service.validate(
            self.records,
            self.request,
            inventory_revision=7,
            snapshot=self.snapshot,
            allocations=evidence,
            require_complete=True,
            completed=evidence[0]['item_ids'][:2],
        )
        self.assertEqual(list(result.objective), validated['objective'])
        self.assertEqual((2, 25), (validated['completed_count'], validated['item_count']))
        self.assertIsNotNone(self.jobs._workers['search'].pid)
        with tempfile.TemporaryDirectory() as folder:
            store = AllocationStore(folder)
            frozen = store.save(result, self.records, self.snapshot)
            self.assertEqual(frozen, store.load(result.plan_id))
            self.assertEqual(5, frozen['result']['objective'][0])
        evidence[0]['scores'][0] += 1
        with self.assertRaisesRegex(ValueError, 'scores'):
            self.service.validate(
                self.records,
                self.request,
                inventory_revision=7,
                snapshot=self.snapshot,
                allocations=evidence,
            )
        print(
            'PRODUCTION_NATIVE_FIVE_PEER_OK old=170/150/150/150/140 new_cents='
            + str([row.total_cents for row in result.roles])
            + ' validated=True'
        )

    def test_native_deadline_node_limit_and_cancel_are_honest(self):
        for options, reason in (
            ({'max_nodes': 1}, 'node_limit'),
            ({'seconds': 1e-9}, 'time_limit'),
        ):
            result = self.solve(**options)
            self.assertFalse(result.exhaustive)
            self.assertEqual(reason, result.stop_reason)
            self.assertTrue(all(len(row.item_ids) in (0, 5) for row in result.roles))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AllocationCancelled):
            self.solve(cancel_event=cancel)
        with self.assertRaises(OperationCancelled):
            self.service.precheck(
                self.records,
                self.request,
                inventory_revision=7,
                snapshot=self.snapshot,
                cancel_event=cancel,
            )

    def test_plan_precheck_uses_injected_native_service_without_database_writes(self):
        with tempfile.TemporaryDirectory() as folder, EchoRepository(folder) as repository:
            profile = repository.create_profile('原生预检合成档案')
            records = tuple(replace(record, profile_id=profile) for record in self.records)
            for record in records:
                repository.add(record)
            request = replace(self.request, profile_id=profile)
            commands = PlanCommands(AllocationStore(folder), repository, allocation=self.service)
            before = repository.export_backup()
            with patch.object(self.service, 'precheck', wraps=self.service.precheck) as precheck:
                result = commands.execute(
                    'precheck',
                    {'request': request, 'snapshot': self.snapshot},
                    candidate_ids=tuple(record.item_id for record in records),
                    scope_generation=3,
                )
            self.assertEqual(1, precheck.call_count)
            report = result.payload[-1]
            self.assertEqual(profile, report['profile_id'])
            self.assertEqual(3, report['scope_generation'])
            self.assertEqual(5, len(report['roles']))
            self.assertTrue(
                all(
                    row['slot_ids'] == (tuple(r.item_id for r in records),) * 5
                    for row in report['roles']
                )
            )
            self.assertEqual(before, repository.export_backup())

    def test_input_generator_cancel_after_last_record_does_not_start_search(self):
        cancel = threading.Event()

        def records():
            yield from self.records
            cancel.set()

        with self.assertRaises(AllocationCancelled):
            self.service.solve(
                records(),
                self.request,
                inventory_revision=7,
                snapshot=self.snapshot,
                cancel_event=cancel,
            )
        self.assertEqual({}, self.jobs._workers)
