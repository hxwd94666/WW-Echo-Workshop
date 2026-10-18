# 核验整批及单角色历史备份不改当前归属并保持原始范围。
import tempfile
import threading
import unittest
from unittest.mock import patch

from echo_workshop.application.allocation import AllocationService
from echo_workshop.application.plan_jobs import PlanCommands
from tests.native_runtime import ScoreService
from echo_workshop.core.allocation.models import AllocationRequest, SlotRule
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.resources.weights import WeightProvider
from tests.native_runtime import EchoRepository
from tests.native_runtime import AllocationStore
from tests.calculation.helpers import role, tuned


class LoadoutActionsContract(unittest.TestCase):
    def test_backup_subset_is_independent_history_and_new_save_keeps_full_source(self):
        with tempfile.TemporaryDirectory() as directory, EchoRepository(directory) as repo:
            profile = repo.create_profile('保存应用合成档案')
            weights = WeightProvider(directory)
            store = AllocationStore(directory)
            commands = PlanCommands(store, repo)
            records = tuple(tuned(profile) for _ in range(10))
            for record in records:
                repo.add(record)
            specs = tuple(
                role(character, slots=tuple(SlotRule(1) for _ in range(5)))
                for character in ('1304', '1109')
            )
            result = AllocationService(ScoreService(weights)).solve(
                records,
                AllocationRequest(profile, specs),
                inventory_revision=repo.revision,
                snapshot=weights.active,
            )
            args = dict(profile_id=profile, result=result, records=records, snapshot=weights.active)
            before = repo.export_backup()
            all_backup = commands.execute('backup-loadout', args)
            self.assertEqual(repo.export_backup(), before)
            original_bytes = store._path(all_backup.payload['plan_id']).read_bytes()
            one = commands.execute(
                'backup-loadout',
                dict(profile_id=profile, payload=all_backup.payload, characters=('1304',)),
            )
            again = commands.execute(
                'backup-loadout',
                dict(profile_id=profile, payload=all_backup.payload, characters=('1304',)),
            )
            self.assertNotEqual(one.payload['plan_id'], all_backup.payload['plan_id'])
            self.assertNotEqual(one.payload['plan_id'], again.payload['plan_id'])
            self.assertEqual(len(one.payload['request']['roles']), 1)
            self.assertEqual(len(one.payload['records']), 5)
            self.assertEqual(len(one.source_payload['result']['roles']), 2)
            self.assertEqual(repo.export_backup(), before)
            self.assertEqual(
                store._path(all_backup.payload['plan_id']).read_bytes(), original_bytes
            )
            # History publication and atomic current assignment have distinct receipts.
            for cause in (ValueError('合成提交冲突'), OperationCancelled('合成提交前取消')):
                with self.subTest(cause=type(cause).__name__):
                    inventory = repo.export_backup()
                    revisions = repo.revision, repo.loadout_revision

                    def reject_current():
                        raise cause

                    with self.assertLogs(
                        'echo_workshop.application.plan_jobs', level='INFO'
                    ) as logs:
                        partial = commands.execute(
                            'save-loadout',
                            dict(
                                args,
                                characters=('1304',),
                                expected_revision=revisions[0],
                                expected_loadout_revision=revisions[1],
                            ),
                            before_commit=reject_current,
                        )
                    self.assertTrue(partial.committed)
                    self.assertIs(partial.current_committed, False)
                    self.assertIn('历史备选已保留，当前配装未更新', partial.notice)
                    self.assertEqual(
                        len(store.load(partial.payload['plan_id'])['result']['roles']), 1
                    )
                    self.assertEqual(repo.export_backup(), inventory)
                    self.assertEqual((repo.revision, repo.loadout_revision), revisions)
                    self.assertTrue(all(row.exc_info is None for row in logs.records))
                    if isinstance(cause, OperationCancelled):
                        self.assertTrue(all(row.levelname == 'INFO' for row in logs.records))
            cancelled = threading.Event()
            cancelled.set()
            count = len(list(store.folder.glob('*.json')))
            with self.assertRaises(OperationCancelled):
                commands.execute('backup-loadout', args, cancel_event=cancelled)
            self.assertEqual(len(list(store.folder.glob('*.json'))), count)
            receipt = commands.execute(
                'save-loadout',
                dict(
                    args,
                    characters=('1304',),
                    expected_revision=repo.revision,
                    expected_loadout_revision=repo.loadout_revision,
                ),
            )
            self.assertTrue(receipt.committed)
            self.assertIs(receipt.current_committed, True)
            self.assertEqual(len(receipt.payload['result']['roles']), 1)
            self.assertEqual(len(receipt.source_payload['result']['roles']), 2)
            self.assertIsNotNone(receipt.loadouts)
            self.assertEqual(len(receipt.loadouts.current), 1)
            first_ids = set(result.roles[0].item_ids)
            for record in repo.list(profile):
                if record.item_id in first_ids:
                    self.assertEqual(record.owner_character_id, '1304')
                    self.assertEqual(record.owner, weights.active.characters.names['1304'])
                else:
                    self.assertEqual(record.owner_character_id, '')
            first_current = receipt.loadouts.current[0]
            second = commands.execute(
                'save-loadout',
                dict(
                    args,
                    characters=('1109',),
                    expected_revision=repo.revision,
                    expected_loadout_revision=repo.loadout_revision,
                ),
            )
            self.assertEqual(len(second.source_payload['result']['roles']), 2)
            self.assertEqual(
                next(row for row in second.loadouts.current if row['character_id'] == '1304'),
                first_current,
            )
            full_arguments = dict(
                args,
                expected_revision=repo.revision,
                expected_loadout_revision=repo.loadout_revision,
                replace_plans={
                    row['character_id']: row['plan_id'] for row in second.loadouts.current
                },
            )
            with patch.object(
                repo,
                'loadout_snapshot',
                side_effect=[second.loadouts, ValueError('synthetic postcommit readback failure')],
            ):
                full = commands.execute('save-loadout', full_arguments)
            self.assertTrue(full.committed)
            self.assertIsNone(full.loadouts)
            actual = repo.loadout_snapshot(profile)
            self.assertEqual(len(actual.claims), 10)
            self.assertEqual({row['plan_id'] for row in actual.current}, {full.payload['plan_id']})
            with self.assertRaisesRegex(ValueError, '已变化'):
                commands.execute(
                    'save-loadout',
                    dict(args, expected_revision=before['revision'], expected_loadout_revision=0),
                )
            self.assertEqual(
                store._path(all_backup.payload['plan_id']).read_bytes(), original_bytes
            )
            print(
                'LOADOUT_ACTIONS_OK: repeat/full/single backups immutable and DB-free; single save updates only chosen5 and retains full source scope; stale/cancel protected'
            )
