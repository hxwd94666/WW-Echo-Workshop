# 验证备份只留快照、设为当前才占用、逐角色移除和双模式优化交互。
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

from echo_workshop.core.models import canonical_json


def digest(payload):
    return hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()


def in_background(call, *args, **kwargs):
    """契约测试遵守后台计算守卫：显式在后台线程执行，不改变被测行为。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(call, *args, **kwargs).result()


class _OffThread:
    """Plan commands/loaders validate frozen evidence natively; keep them off the GUI thread."""

    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        value = getattr(self._target, name)
        if name.startswith('_') or not callable(value):
            return value
        return lambda *args, **kwargs: in_background(value, *args, **kwargs)


class SavedModesStorageContract(unittest.TestCase):
    def setUp(self):
        from echo_workshop.application.runtime import PluginRuntime
        from echo_workshop.application.compute_jobs import ComputeJobs
        from tests.native_runtime import ENGINE
        from echo_workshop.core.allocation.models import AllocationRequest, SlotRule
        from tests.calculation.helpers import role, tuned

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = PluginRuntime(
            Path(self.temp.name) / 'config.json', compute=ComputeJobs(ENGINE)
        )
        self.addCleanup(self.runtime.close)
        self.repo = self.runtime.repository
        self.profile = self.repo.create_profile('备份语义合成档案')
        self.records = tuple(tuned(self.profile) for _ in range(11))
        for record in self.records:
            self.repo.add(record)
        roles = tuple(
            role(character, slots=tuple(SlotRule(1) for _ in range(5)))
            for character in ('1304', '1109')
        )
        self.result = in_background(
            self.runtime.allocation.solve,
            self.records,
            AllocationRequest(self.profile, roles),
            inventory_revision=self.repo.revision,
            snapshot=self.runtime.weights.active,
        )
        self.args = dict(
            profile_id=self.profile,
            result=self.result,
            records=self.records,
            snapshot=self.runtime.weights.active,
        )
        self.commands = _OffThread(self.runtime.plan_commands)
        self.store = _OffThread(self.runtime.plans)

    def backup(self):
        return self.commands.execute('backup-loadout', self.args).payload

    def remove_args(self, payload):
        return dict(
            profile_id=self.profile,
            plan_id=payload['plan_id'],
            character_id='1304',
            modal_id='default',
            expected_hash=digest(payload),
        )

    def test_explicit_origins_backup_has_no_current_side_effect_and_apply_is_atomic(self):
        before = self.repo.export_backup()
        backup = self.backup()
        self.assertEqual(
            backup['publication'], dict(kind='backup', source_plan_id=self.result.plan_id)
        )
        self.assertEqual(self.repo.export_backup(), before)
        saved = self.commands.execute(
            'save-loadout',
            dict(
                self.args,
                characters=('1304',),
                expected_revision=self.repo.revision,
                expected_loadout_revision=self.repo.loadout_revision,
            ),
        )
        self.assertEqual(saved.payload['publication']['kind'], 'generated')
        self.assertTrue(saved.current_committed)
        current = self.repo.loadout_snapshot(self.profile)
        self.assertEqual(len(current.current), 1)
        self.assertEqual(len(current.claims), 5)
        self.assertEqual(self.store.load(backup['plan_id']), backup)
        print('BACKUP_NO_DB_SIDE_EFFECT_APPLY_CURRENT_UPDATES_ONLY_SELECTED_FIVE_OK')

    def test_remove_backup_role_is_scoped_persistent_and_keeps_live_plan_evidence(self):
        backup = self.backup()
        before = self.repo.export_backup()
        result = self.commands.execute('remove-backup', self.remove_args(backup))
        self.assertTrue(result.committed)
        self.assertEqual(result.payload['removed_roles'], [['1304', 'default']])
        for field in (
            'request',
            'result',
            'records',
            'weight_snapshot',
            'completed',
            'publication',
        ):
            self.assertEqual(result.payload[field], backup[field])
        self.assertEqual(self.repo.export_backup(), before)
        from tests.native_runtime import AllocationStore

        self.assertEqual(
            AllocationStore(self.runtime.data_dir).load(backup['plan_id']), result.payload
        )
        summaries = {}
        self.store.list(self.profile, summary_sink=summaries)
        self.assertEqual(summaries[backup['plan_id']]['removed_roles'], [['1304', 'default']])
        self.assertEqual(len(summaries[backup['plan_id']]['roles']), 2)
        with self.assertRaises(ValueError):
            self.commands.execute('remove-backup', self.remove_args(backup))
        print('REMOVE_ONE_BACKUP_PRESERVES_OTHER_ROLE_DB_AND_FROZEN_EVIDENCE_OK')

    def test_removal_cancel_failure_wrong_profile_and_malformed_metadata_preserve_original(self):
        from echo_workshop.core.cancellation import OperationCancelled
        from tests.native_runtime import validate_plan

        backup = self.backup()
        path = self.store._path(backup['plan_id'])
        original = path.read_bytes()
        event = threading.Event()
        event.set()
        with self.assertRaises(OperationCancelled):
            self.commands.execute('remove-backup', self.remove_args(backup), cancel_event=event)

        def fail():
            raise OSError('synthetic precommit failure')

        with self.assertRaises(OSError):
            self.commands.execute('remove-backup', self.remove_args(backup), before_commit=fail)
        other = self.repo.create_profile('另一档案')
        with self.assertRaises(ValueError):
            self.commands.execute('remove-backup', dict(self.remove_args(backup), profile_id=other))
        self.assertEqual(path.read_bytes(), original)
        for changes in (
            dict(removed_roles=[['bad', 'default']]),
            dict(removed_roles=[['1304', 'default']] * 2),
            dict(publication=dict(kind='executable', source_plan_id=backup['plan_id'])),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_plan(dict(backup, **changes))

    def test_optimized_backup_creates_new_frozen_backup_without_activating_or_overwriting(self):
        from tests.native_runtime import replacement_candidates, replacement_preview

        backup = self.backup()
        before = self.repo.export_backup()
        old_bytes = self.store._path(backup['plan_id']).read_bytes()
        reservations = self.repo.loadout_snapshot(self.profile)
        options = replacement_candidates(backup, reservations, '1304', 0)
        replacement = next(
            row
            for row in options.candidates
            if row.available and row.item_id != options.old_record.item_id
        )
        preview = replacement_preview(options, replacement.item_id, reservations)
        result = self.commands.execute(
            'backup-loadout',
            dict(
                profile_id=self.profile,
                result=preview.result,
                records=preview.records,
                snapshot=preview.snapshot,
                characters=('1304',),
            ),
        )
        self.assertEqual(result.payload['publication']['kind'], 'backup')
        self.assertEqual(len(result.payload['result']['roles']), 1)
        self.assertEqual(result.payload['result']['roles'][0]['item_ids'][0], replacement.item_id)
        self.assertNotEqual(result.payload['plan_id'], backup['plan_id'])
        self.assertEqual(self.store._path(backup['plan_id']).read_bytes(), old_bytes)
        self.assertEqual(self.repo.export_backup(), before)
        print('OPTIMIZED_BACKUP_NEW_ID_NO_OWNERSHIP_OR_SOURCE_OVERWRITE_OK')

    def test_retired_equip_command_has_no_storage_method_or_writable_dispatch(self):
        self.assertFalse(hasattr(self.repo, 'mark_loadout_equipped'))
        before = self.repo.export_backup()
        with self.assertRaises(ValueError):
            self.commands.execute('mark-equipped', {})
        self.assertEqual(self.repo.export_backup(), before)

    def test_profile_archive_roundtrip_preserves_origin_removal_and_frozen_scores(self):
        from echo_workshop.storage.profile_transfer import (
            export_profile_archive,
            import_profile_archive,
        )
        from tests.native_runtime import inspect_profile_archive
        from tests.native_runtime import EchoRepository
        from tests.native_runtime import AllocationStore

        backup = self.backup()
        self.commands.execute('remove-backup', self.remove_args(backup))
        archive = Path(self.temp.name) / 'profile.zip'
        export_profile_archive(self.repo, self.store, {}, self.profile, archive)
        preview = inspect_profile_archive(archive)
        with EchoRepository(Path(self.temp.name) / 'restored') as repo:
            store = AllocationStore(repo.data_dir)
            import_profile_archive(
                repo,
                store,
                archive,
                expected_revision=repo.revision,
                expected_archive_sha256=preview.source_sha256,
            )
            restored = store.load(backup['plan_id'])
            self.assertEqual(restored['publication'], backup['publication'])
            self.assertEqual(restored['removed_roles'], [['1304', 'default']])
            self.assertEqual(restored['result'], backup['result'])
            self.assertEqual(restored['weight_snapshot'], backup['weight_snapshot'])
        print('NEW_PROFILE_ARCHIVE_ORIGIN_REMOVAL_FROZEN_SCORE_ROUNDTRIP_OK')
