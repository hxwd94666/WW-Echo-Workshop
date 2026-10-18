# 核验合成旧档案、库存版本、冻结历史及失败取消的无损数据合同。
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from echo_workshop.storage.inventory import EchoRepository

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures'


class DataCompatibilityContract(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'build/test-temp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.previous_temp = tempfile.tempdir
        tempfile.tempdir = str(temp_root)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.data = json.loads((FIXTURE / 'dev115-data.json').read_text(encoding='utf-8'))

    def tearDown(self):
        self.temp.cleanup()
        tempfile.tempdir = self.previous_temp

    def test_revision_probe_is_live_and_keeps_writer_transaction_semantics(self):
        with EchoRepository(self.root) as repo:
            self.assertEqual(repo.try_revision(), 0)
            repo.create_profile('合成探测档案')
            self.assertEqual(repo.try_revision(), 1)
            with closing(sqlite3.connect(self.root / 'inventory.sqlite3', timeout=0)) as other:
                other.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
                other.commit()
            self.assertEqual(repo.try_revision(), 2)
            with repo._lock:
                repo._db.execute('BEGIN IMMEDIATE')
                repo._bump()
                self.assertEqual(repo.try_revision(), 3)
                repo._db.rollback()
            self.assertEqual(repo.try_revision(), 2)
            self.assertFalse(repo._revision_probe._connection.in_transaction)
            self.assertEqual(repo._db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
        with self.assertRaises(ValueError):
            repo.try_revision()

    def test_revision_probe_does_not_wait_or_change_busy_timeout(self):
        with EchoRepository(self.root) as repo:
            repo.try_revision()
            with closing(sqlite3.connect(self.root / 'inventory.sqlite3', timeout=0)) as other:
                repo._db.execute('BEGIN')
                other.execute('BEGIN EXCLUSIVE')
                start = time.perf_counter()
                self.assertIsNone(repo.try_revision())
                self.assertLess(time.perf_counter() - start, 0.25)
                self.assertEqual(repo._db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
                repo._db.rollback()
                # The ordinary read-only path has the same zero-wait contract.
                self.assertIsNone(repo.try_revision())
                other.rollback()
            locked, release = threading.Event(), threading.Event()

            def hold():
                with repo._lock:
                    locked.set()
                    release.wait(5)

            thread = threading.Thread(target=hold)
            thread.start()
            try:
                self.assertTrue(locked.wait(2))
                start = time.perf_counter()
                self.assertIsNone(repo.try_revision())
                self.assertLess(time.perf_counter() - start, 0.25)
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(repo.try_revision(), 0)
            self.assertEqual(repo._db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)

    def test_readonly_revision_probe_and_real_metadata_error(self):
        with EchoRepository(self.root) as repo:
            repo.create_profile('只读合成档案')
        with closing(sqlite3.connect(self.root / 'inventory.sqlite3')) as other:
            other.execute('PRAGMA user_version=4')
        with EchoRepository(self.root, read_only=True) as reader:
            self.assertEqual(reader.try_revision(), 1)
        with closing(sqlite3.connect(self.root / 'inventory.sqlite3')) as other:
            other.execute('PRAGMA user_version=3')
        # 旧版 schema 不再支持：只读打开也显式拒绝，不迁移原件。
        with self.assertRaises(ValueError):
            EchoRepository(self.root, read_only=True)
        with closing(sqlite3.connect(self.root / 'inventory.sqlite3')) as other:
            other.execute('PRAGMA user_version=4')
        with EchoRepository(self.root) as repo:
            with repo._db:
                repo._db.execute("DELETE FROM metadata WHERE key='revision'")
            with self.assertRaises(ValueError):
                repo.try_revision()

    def test_current_inventory_schema_roundtrips_and_old_schemas_are_rejected(self):
        # 旧版本兼容已删除：当前 schema 往返无损，旧 schema 显式拒绝且不改动原件。
        with EchoRepository(self.root / 'current') as repo:
            backup = deepcopy(self.data['backup'])
            backup['schema_version'] = 4
            report = repo.import_backup(backup, expected_revision=repo.revision)
            actual = repo.export_backup()
            self.assertEqual(report, 5)
            self.assertEqual(actual['profiles'], backup['profiles'])
            # 归档功能已删除：旧包里的 archived 标记读取时丢弃，不再往返。
            expected_items = [
                {key: value for key, value in row.items() if key != 'archived'}
                for row in backup['items']
            ]
            self.assertEqual(actual['items'], expected_items)
            self.assertEqual(actual['history'], self._without_archived(backup['history']))
            self.assertEqual(
                actual['pending_captures'],
                self._without_archived(backup.get('pending_captures', [])),
            )
            self.assertEqual(actual['current_loadouts'], backup.get('current_loadouts', []))
        for version in (1, 2, 3):
            with self.subTest(schema=version), EchoRepository(self.root / str(version)) as repo:
                old = deepcopy(self.data['backup'])
                old['schema_version'] = version
                with self.assertRaises(ValueError):
                    repo.import_backup(old, expected_revision=repo.revision)

    @staticmethod
    def _without_archived(rows):
        """归档功能已删除：旧包记录里的 archived 标记读取时丢弃，不再往返。"""
        return [
            {
                **row,
                'record': {key: value for key, value in row['record'].items() if key != 'archived'},
            }
            for row in rows
        ]
