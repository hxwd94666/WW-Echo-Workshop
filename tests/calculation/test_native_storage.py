# 验证冻结方案的原生重验、摘要、取消和提交竞态，不使用 Python 求值回退。
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from echo_workshop.application.allocation import AllocationService
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import canonical_json
from echo_workshop.storage.archives import export_archive, import_archive
from echo_workshop.storage.inventory import EchoRepository
from echo_workshop.storage.plans import AllocationStore, validate_plan
from tests.calculation.test_native_allocation_service import target_fixture

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


class TestNativeStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = ComputeJobs(ENGINE, fault_tests=True)
        self.addCleanup(self.jobs.wait_closed)
        self.addCleanup(self.jobs.close)
        self.service = AllocationService(None, compute=self.jobs)
        self.directory = Path(self.temp.name) / 'source'
        self.repo = EchoRepository(self.directory, plan_evaluator=self.service)
        self.addCleanup(self.repo.close)
        profile = self.repo.create_profile('原生存储合成样本')
        records, request, self.snapshot = target_fixture()
        self.records = tuple(replace(row, profile_id=profile) for row in records)
        self.request = replace(request, profile_id=profile)
        for row in self.records:
            self.repo.add(row)
        self.result = self.service.solve(
            self.records,
            self.request,
            inventory_revision=self.repo.revision,
            snapshot=self.snapshot,
            max_nodes=300,
        )
        self.store = AllocationStore(self.directory, plan_evaluator=self.service)
        self.payload = json.loads(
            canonical_json(
                dict(
                    schema_version=1,
                    plan_id=self.result.plan_id,
                    created_at='2026-10-17T00:00:00+00:00',
                    request=self.request.to_dict(),
                    result=self.result.to_dict(),
                    weight_snapshot=self.snapshot.to_dict(),
                    records=[r.to_dict() for r in self.records],
                    completed=[],
                    progress_revision=0,
                )
            )
        )

    def test_save_load_summary_and_progress_use_native_without_python_evaluators(self):
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
            submit = stack.enter_context(patch.object(self.jobs, 'submit', wraps=self.jobs.submit))
            self.assertEqual(self.payload, self.store.save_frozen(self.payload))
            self.assertEqual(self.payload, self.store.load(self.payload['plan_id']))
            item = self.payload['result']['roles'][0]['item_ids'][0]
            self.store.mark(
                self.payload['plan_id'],
                item,
                True,
                inventory_revision=self.result.inventory_revision,
                weight_version=self.snapshot.version,
                progress_revision=0,
            )
            summaries = {}
            rows, errors = self.store.list(self.request.profile_id, summary_sink=summaries)
            self.assertEqual((), errors)
            self.assertEqual(1, len(rows))
            summary = summaries[self.payload['plan_id']]
            self.assertEqual(1, summary['completed_count'])
            self.assertEqual(25, summary['item_count'])
            self.assertEqual(5, len(summary['roles']))
            self.assertTrue(all(row.get('rating_label') for row in summary['roles']))
            self.assertTrue(all(row['target_cents'] == 15000 for row in summary['roles']))
            self.assertTrue(
                all(call.args[0] == 'validate_allocation' for call in submit.call_args_list)
            )
            self.assertTrue(
                all(call.kwargs['lane'] == 'interactive' for call in submit.call_args_list)
            )
            for mock in mocks:
                mock.assert_not_called()
        self.assertLessEqual(len(self.jobs._workers), 2)
        print(
            'NATIVE_STORAGE_OK scoring_constraints_objective_grades_progress=Rust Python_evaluators=0'
        )

    def test_tampered_evidence_is_rejected_before_overwriting_frozen_file(self):
        self.store.save_frozen(self.payload)
        path = self.store._path(self.payload['plan_id'])
        original = path.read_bytes()
        for field in (
            'scores',
            'total_cents',
            'target_cents',
            'moves',
            'label',
            'objective',
            'duplicate',
            'completed',
        ):
            bad = copy.deepcopy(self.payload)
            row = bad['result']['roles'][0]
            if field == 'scores':
                row['scores'][0] += 1
            elif field == 'label':
                row['label'] = '伪造角色'
            elif field == 'objective':
                bad['result']['objective'][0] -= 1
            elif field == 'duplicate':
                row['item_ids'][1] = row['item_ids'][0]
            elif field == 'completed':
                bad['completed'] = [str(UUID(int=999999))]
            else:
                row[field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store._write(bad)
            self.assertEqual(original, path.read_bytes())

    def test_native_revalidation_precedes_sql_and_revision_is_rechecked_at_commit(self):
        self.store.save_frozen(self.payload)
        revision = self.repo.revision
        old_validate, changed = self.service.validate, []

        def racing(*args, **kwargs):
            self.assertFalse(self.repo._db.in_transaction)
            result = old_validate(*args, **kwargs)
            if not changed:
                self.repo.add(replace(self.records[0], item_id=str(UUID(int=900000))))
                changed.append(True)
            return result

        with patch.object(self.service, 'validate', side_effect=racing):
            with self.assertRaisesRegex(ValueError, '修订|变化'):
                self.repo.commit_current_loadouts(
                    self.payload, expected_revision=revision, expected_loadout_revision=0
                )
        self.assertEqual((), self.repo.loadout_snapshot(self.request.profile_id).current)
        self.assertEqual(revision + 1, self.repo.revision)
        self.assertEqual(self.payload, self.store.load(self.payload['plan_id']))

    def test_cancel_queued_revalidation_preserves_plan_and_reports_normal_cancel(self):
        self.store.save_frozen(self.payload)
        path = self.store._path(self.payload['plan_id'])
        original = path.read_bytes()
        self.jobs.submit('fault_hang', dict, timeout=5)
        cancel, queued = threading.Event(), threading.Event()
        submit = self.jobs.submit

        def observed(op, *args, **kwargs):
            result = submit(op, *args, **kwargs)
            if op == 'validate_allocation':
                queued.set()
            return result

        with (
            patch.object(self.jobs, 'submit', side_effect=observed),
            ThreadPoolExecutor(max_workers=1) as calls,
        ):
            future = calls.submit(
                self.store.mark,
                self.payload['plan_id'],
                self.result.roles[0].item_ids[0],
                True,
                inventory_revision=self.result.inventory_revision,
                weight_version=self.snapshot.version,
                progress_revision=0,
                cancel_event=cancel,
            )
            self.assertTrue(queued.wait(2))
            started = time.monotonic()
            cancel.set()
            with self.assertRaises(OperationCancelled):
                future.result(timeout=0.5)
            self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(0, self.repo.loadout_snapshot(self.request.profile_id).revision)

    def test_missing_engine_and_missing_injection_fail_without_python_fallback(self):
        self.store.save_frozen(self.payload)
        original = self.store._path(self.payload['plan_id']).read_bytes()
        with self.assertRaisesRegex(ValueError, 'Rust'):
            validate_plan(self.payload)
        missing = ComputeJobs(ENGINE.with_name('missing-storage-engine.exe'))
        try:
            evaluator = AllocationService(None, compute=missing)
            store = AllocationStore(self.directory, plan_evaluator=evaluator)
            with patch(
                'echo_workshop.core.scoring.calculate_echo_score',
                side_effect=AssertionError('fallback'),
                create=True,
            ):
                with self.assertRaises(ComputeFailure) as error:
                    store.load(self.payload['plan_id'])
            self.assertEqual('engine_missing', error.exception.kind)
        finally:
            missing.close()
            self.assertTrue(missing.wait_closed())
        self.assertEqual(original, self.store._path(self.payload['plan_id']).read_bytes())

    def test_gui_wait_is_rejected_before_submitting_validation(self):
        self.store.save_frozen(self.payload)
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
                self.store.load(self.payload['plan_id'])
            self.assertEqual('gui_thread', error.exception.kind)
            submit.assert_not_called()

    def test_archive_roundtrip_propagates_same_native_service_and_keeps_source_immutable(self):
        self.store.save_frozen(self.payload)
        self.repo.commit_current_loadouts(
            self.payload, expected_revision=self.repo.revision, expected_loadout_revision=0
        )
        original = self.repo.export_backup()
        plan_bytes = self.store._path(self.payload['plan_id']).read_bytes()
        archive = Path(self.temp.name) / 'synthetic.zip'
        with patch.object(self.service, 'validate', wraps=self.service.validate) as native:
            export_archive(self.repo, archive)
            with EchoRepository(
                Path(self.temp.name) / 'target', plan_evaluator=self.service
            ) as target:
                import_archive(target, archive)
                self.assertEqual(5, len(target.loadout_snapshot(self.request.profile_id).current))
                self.assertEqual(
                    self.payload,
                    AllocationStore(target.data_dir, plan_evaluator=self.service).load(
                        self.payload['plan_id']
                    ),
                )
            self.assertGreater(native.call_count, 0)
        self.assertEqual(original, self.repo.export_backup())
        self.assertEqual(plan_bytes, self.store._path(self.payload['plan_id']).read_bytes())
        self.assertLessEqual(len(self.jobs._workers), 2)


if __name__ == '__main__':
    unittest.main()
