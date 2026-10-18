# 验证实际评分门面的 Rust 通路、冻结矩阵、缓存与 GUI 等待约束。
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.scoring import ScoreService
from echo_workshop.application.runtime import PluginRuntime
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.cancellation import ComparisonCancelled
from echo_workshop.core.models import EchoRecord
from echo_workshop.resources.weights import WeightSnapshot
from tests.calculation.helpers import tuned

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


class TestNativeScoreService(unittest.TestCase):
    def test_runtime_close_cancels_waiting_match_without_partial_result(self):
        release, entered, done = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def prepare():
            entered.set()
            release.wait()
            return {}

        def compare():
            try:
                self.service.compare(
                    [tuned(str(uuid4()), 1)], [('1304', 'default')], inventory_revision=0
                )
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=compare)
        try:
            self.jobs.submit('ping', prepare)
            self.assertTrue(entered.wait(1))
            thread.start()
            deadline = time.monotonic() + 1
            while self.jobs._queues['interactive'].empty() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(self.jobs._queues['interactive'].empty())
            self.jobs.close()
            self.assertTrue(done.wait(0.5))
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], ComparisonCancelled)
            self.assertEqual(0, len(self.service._cache))
        finally:
            release.set()
            if thread.ident is not None:
                thread.join(1)

    def setUp(self):
        self.jobs = ComputeJobs(ENGINE)
        self.addCleanup(self.jobs.wait_closed)
        self.addCleanup(self.jobs.close)
        self.provider = SimpleNamespace(active=WeightSnapshot.builtin())
        self.service = ScoreService(self.provider, compute=self.jobs)

    def test_production_facade_reproduces_frozen_112_cells_without_python_evaluation(self):
        fixture = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_matching.json').read_text(encoding='utf-8')
        )
        records = [EchoRecord.from_dict(row) for row in fixture['payload']['records']]
        selected = [
            (row['character_id'], row['modal_id']) for row in fixture['payload']['selections']
        ]
        rules = {
            key: CompatibilityRule.from_dict(row['rule'])
            for key, row in zip(selected, fixture['payload']['selections'])
        }
        with (
            patch(
                'echo_workshop.core.scoring.prepare_score',
                side_effect=AssertionError('Python'),
                create=True,
            ),
            patch(
                'echo_workshop.core.scoring.score_prepared',
                side_effect=AssertionError('Python'),
                create=True,
            ),
            patch(
                'echo_workshop.core.compatibility.compatibility',
                side_effect=AssertionError('Python'),
            ),
        ):
            actual = self.service.compare(records, selected, rules=rules, inventory_revision=19)
        expected = fixture['expected']
        standards = expected['standards']
        from echo_workshop.application.compute_bridge import compact_score
        from echo_workshop.application.scoring import (
            _COMPATIBILITY,
            _REASON,
            _RECOMMENDATION,
            _SONATA,
        )

        self.assertEqual(112, len(actual))
        for position, row in enumerate(actual):
            cell = expected['cells'][position]
            character, modal = selected[position % len(selected)]
            self.assertEqual(expected['ids'][position // len(selected)], row.item_id)
            self.assertEqual(character, row.character_id)
            self.assertEqual(modal, row.modal_id)
            self.assertEqual(compact_score(cell[0], standards), row.score)
            self.assertEqual(tuple(cell[5]), row.issues)
            self.assertEqual(_RECOMMENDATION[cell[3]], row.recommendation)
            self.assertEqual(_COMPATIBILITY[cell[1]], row.compatibility)
            self.assertEqual(_SONATA[cell[2]], row.sonata_compatibility)
            self.assertEqual(_REASON[cell[4]], row.exclusion)
        print('PRODUCTION_RUST_MATCH_GOLDEN_OK cells=112 Python_evaluators=0')

    def test_500_by_8_matrix_is_batched_and_cached_without_changing_order(self):
        base = tuned(str(uuid4()), 1)
        records = [replace(base, item_id=str(UUID(int=10000 + i))) for i in range(500)]
        selected = [(str(i), 'default') for i in range(1102, 1110)]
        with patch.object(self.jobs, 'submit', wraps=self.jobs.submit) as submit:
            first = self.service.compare(records, selected, inventory_revision=1)
            calls = submit.call_count
            again = self.service.compare(records, selected, inventory_revision=1)
            self.assertEqual(calls, submit.call_count)
        self.assertEqual(1, calls)
        self.assertEqual(4000, len(first))
        self.assertTrue(all(a is b for a, b in zip(first, again)))
        self.assertEqual(
            [(r.item_id, char, modal) for r in records for char, modal in selected],
            [(cell.item_id, cell.character_id, cell.modal_id) for cell in first],
        )
        print(f'PRODUCTION_RUST_BATCH_OK cells=4000 blocks={calls} cached_calls=0')

    def test_gui_thread_rejects_blocking_facade_before_submitting_work(self):
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
            self.assertRaises(ComputeFailure) as error,
        ):
            self.service.compare([], [], inventory_revision=0)
        self.assertEqual('gui_thread', error.exception.kind)
        submit.assert_not_called()

    def test_duplicate_uuid_and_role_are_rejected_without_partial_cache(self):
        record = tuned(str(uuid4()), 1)
        for records, selected in (
            ([record, record], [('1304', 'default')]),
            ([record], [('1304', 'default')] * 2),
        ):
            with self.assertRaises(ValueError):
                self.service.compare(records, selected, inventory_revision=0)
            self.assertEqual(0, len(self.service._cache))

    def test_missing_native_program_fails_without_using_python_scoring(self):
        jobs = ComputeJobs(ENGINE.with_name('missing-service-engine.exe'))
        self.addCleanup(jobs.wait_closed)
        self.addCleanup(jobs.close)
        service = ScoreService(self.provider, compute=jobs)
        with self.assertRaises(ComputeFailure) as error:
            service.compare([tuned(str(uuid4()), 1)], [('1304', 'default')], inventory_revision=0)
        self.assertEqual('engine_missing', error.exception.kind)
        self.assertEqual(0, len(service._cache))

    def test_runtime_shares_and_reaps_the_actual_native_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = PluginRuntime(Path(folder) / 'config.json', compute=self.jobs)
            try:
                self.assertIs(self.jobs, runtime.scores.compute)
                self.assertIs(runtime.allocation, runtime.repository.plan_evaluator)
                self.assertIs(runtime.allocation, runtime.plans.plan_evaluator)
                self.assertIs(runtime.allocation, runtime.workspaces.plan_evaluator)
                runtime.scores.compare(
                    [tuned(str(uuid4()), 1)], [('1304', 'default')], inventory_revision=0
                )
                self.assertIsNotNone(self.jobs._workers['interactive'].pid)
            finally:
                runtime.close()
            self.assertTrue(self.jobs.wait_closed())
            self.assertIsNone(self.jobs._workers['interactive'].pid)

    def test_runtime_constructor_failure_also_closes_compute_service(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                'echo_workshop.application.runtime.WeightProvider', side_effect=ValueError('init')
            ),
            self.assertRaisesRegex(ValueError, 'init'),
        ):
            PluginRuntime(Path(folder) / 'config.json', compute=self.jobs)
        self.assertTrue(self.jobs.wait_closed())
        with self.assertRaises(ComputeFailure) as error:
            self.jobs.submit('ping', dict)
        self.assertEqual('closed', error.exception.kind)

    def test_workspace_startup_failure_closes_owner_before_providers_or_native_start(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                'echo_workshop.application.runtime.WorkspaceManager',
                side_effect=ValueError('startup'),
            ),
            self.assertRaisesRegex(ValueError, 'startup'),
        ):
            PluginRuntime(Path(folder) / 'config.json', compute=self.jobs)
        self.assertTrue(self.jobs._closed.is_set())
        self.assertTrue(self.jobs.wait_closed())
        self.assertEqual({}, self.jobs._workers)
