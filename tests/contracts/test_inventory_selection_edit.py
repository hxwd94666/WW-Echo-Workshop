# 验证库存选中项操作只提交预览绑定的 UUID、档案和修订。
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from ww_echo_workshop.application.inventory import InventoryService
from ww_echo_workshop.core.models import EchoRecord
from ww_echo_workshop.storage.inventory import EchoRepository


class InventorySelectionEditContract(unittest.TestCase):
    def test_delete_preview_preserves_unselected_rows_and_related_pending_on_refusal(self):
        with TemporaryDirectory() as directory, closing(EchoRepository(Path(directory))) as repo:
            service = InventoryService(repo, Event())
            profile, other = service.create_profile('甲'), service.create_profile('乙')
            target, untouched, elsewhere, observation = (
                EchoRecord(key, 1, (), name='同名合成声骸')
                for key in (profile, profile, other, profile)
            )
            for row in (target, untouched, elsewhere):
                repo.add(row)
            repo.stage_capture(observation, 'duplicate', (target.item_id,))
            ids = [target.item_id, target.item_id]
            change = service.prepare_selection(
                profile, ids, 'delete', expected_revision=repo.revision
            )
            self.assertEqual((target.item_id,), change.item_ids)
            self.assertEqual(1, change.related_pending)
            self.assertEqual((), change.affected_loadouts)
            ids[:] = [untouched.item_id]
            with self.assertRaises(FrozenInstanceError):
                change.action = 'unlock'
            self.assertEqual((target, untouched), tuple(repo.list(profile)))
            self.assertEqual([observation], [row[0] for row in repo.pending_captures(profile)])
            with self.assertRaisesRegex(ValueError, '档案已变化'):
                service.apply_selection(change, profile_id=other)
            self.assertEqual(change.inventory_revision, repo.revision)

            self.assertEqual(1, service.apply_selection(change, profile_id=profile))
            self.assertEqual([untouched], repo.list(profile))
            self.assertEqual([elsewhere], repo.list(other))
            self.assertEqual([], repo.pending_captures(profile))

    def test_apply_rejects_changed_inventory_or_loadout_revision_then_can_repreview(self):
        with TemporaryDirectory() as directory, closing(EchoRepository(Path(directory))) as repo:
            service = InventoryService(repo, Event())
            profile = service.create_profile('合成库存')
            first, second = (EchoRecord(profile, 1, (), game_locked=False) for _ in range(2))
            for row in (first, second):
                repo.add(row)
            selected = (first.item_id, second.item_id)
            change = service.prepare_selection(
                profile, selected, 'lock', expected_revision=repo.revision
            )
            service.create_profile('更改库存版本')
            with self.assertRaisesRegex(ValueError, '声骸库已变化'):
                service.apply_selection(change, profile_id=profile)
            change = service.prepare_selection(
                profile, selected, 'lock', expected_revision=repo.revision
            )
            # Bump only the independent loadout clock to isolate its CAS guard.
            with repo._db:
                repo._db.execute("UPDATE metadata SET value=value+1 WHERE key='loadout_revision'")
            with self.assertRaisesRegex(ValueError, '当前配装已变化'):
                service.apply_selection(change, profile_id=profile)
            self.assertEqual((first, second), tuple(repo.get(key) for key in selected))
            self.assertEqual([], repo.history(first.item_id))

            change = service.prepare_selection(
                profile, selected, 'lock', expected_revision=repo.revision
            )
            self.assertEqual(2, service.apply_selection(change, profile_id=profile))
            for old in (first, second):
                current = repo.get(old.item_id)
                self.assertTrue(current.locked)
                self.assertFalse(current.game_locked)
                self.assertEqual([old], repo.history(old.item_id))
