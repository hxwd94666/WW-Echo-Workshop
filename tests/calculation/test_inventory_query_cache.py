# 验证库存输入快照的内容失效、有界占用、原生重传及取消不缓存。
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.inventory_query_inputs import CACHE_BYTES, InventoryQueryInputs
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import EchoRecord, StatValue

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'
FIXTURE = Path(__file__).with_name('fixtures') / 'native_inventory_query.json'


class InventoryQueryCache(unittest.TestCase):
    def setUp(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.record = EchoRecord.from_dict(data['records'][0])
        self.now = datetime.fromisoformat(data['now'])
        self.jobs = ComputeJobs(ENGINE, fault_tests=True)
        self.service = InventoryQueryService(compute=self.jobs)
        self.requests = []
        original = self.jobs.submit

        def submit(operation, build, **options):
            def observed():
                value = build()
                if operation == 'inventory_query':
                    self.requests.append((value['snapshot_id'], value['rows'] is not None))
                return value

            return original(operation, observed, **options)

        self.patcher = patch.object(self.jobs, 'submit', submit)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.jobs.close()
        self.assertTrue(self.jobs.wait_closed())

    def query(self, record=None, **options):
        return self.service.filter_records((record or self.record,), now=self.now, **options)

    def test_reconstructed_equal_content_reuses_input_but_not_query_results(self):
        self.assertEqual((self.record,), self.query())
        clone = EchoRecord.from_dict(self.record.to_dict())
        self.assertEqual((), self.query(clone, cost=4))
        self.assertEqual((clone,), self.query(clone, minimum_level=25))
        self.assertEqual([True, False, False], [full for _, full in self.requests])
        self.assertEqual(1, len({identity for identity, _ in self.requests}))
        self.assertLessEqual(self.service._inputs._bytes, CACHE_BYTES)
        self.assertTrue(all(entry['rows'] is None for entry in self.service._inputs._entries))

    def test_full_input_identity_tracks_each_business_field_and_review_kind(self):
        self.query()
        changes = (
            dict(item_id='44444444-4444-4444-8444-444444444444'),
            dict(profile_id='44444444-4444-4444-8444-444444444444'),
            dict(cost=3),
            dict(name=''),
            dict(sonata=''),
            dict(level=20),
            dict(review='pending'),
            dict(issues=('ocr:needs_review',)),
            dict(locked=True),
            dict(owner='owner'),
            dict(owner_state='ambiguous'),
            dict(game_equipped=True),
            dict(note='changed'),
            dict(captured_at=(self.now + timedelta(microseconds=1)).isoformat()),
            dict(main_stats=(StatValue('攻击%', 9), self.record.main_stats[1])),
            dict(sub_stats=self.record.sub_stats[:-1]),
        )
        for change in changes:
            with self.subTest(change=change):
                self.query()
                original_id = self.requests[-1][0]
                self.query(replace(self.record, **change))
                self.assertTrue(self.requests[-1][1])
                self.assertNotEqual(original_id, self.requests[-1][0])
                self.assertLessEqual(len(self.service._inputs._entries), 2)
                self.assertLessEqual(self.service._inputs._bytes, CACHE_BYTES)
        rows = ((self.record, 'item', None),)
        _, stats, _ = self.service.review(rows)
        self.assertEqual(1, stats['confirmed'])
        _, stats, _ = self.service.review(((self.record, 'duplicate', None),))
        self.assertEqual(0, stats['confirmed'])
        self.assertEqual(1, stats['pending'])
        self.assertTrue(self.requests[-1][1])

    def test_text_and_timestamp_changes_invalidate_even_with_same_uuid_and_query(self):
        target = replace(self.record, note='needle')
        self.assertEqual((target,), self.query(target, text='needle'))
        self.assertEqual((), self.query(replace(target, note='other'), text='needle'))
        recent = replace(target, captured_at=self.now.isoformat())
        self.assertEqual((recent,), self.query(recent, max_age_days=1))
        stale = replace(recent, captured_at=(self.now - timedelta(days=2)).isoformat())
        self.assertEqual((), self.query(stale, max_age_days=1))
        self.assertTrue(all(full for _, full in self.requests))

    def test_native_eviction_and_restart_reupload_once_without_python_evaluation(self):
        self.query()
        identity = self.requests[-1][0]
        # Another explicit service shares the owner and can evict these native snapshots.
        other = InventoryQueryService(compute=self.jobs)
        for i in range(3):
            other.filter_records((replace(self.record, note=str(i)),), now=self.now)
        self.requests.clear()
        self.assertEqual((self.record,), self.query())
        self.assertEqual([(identity, False), (identity, True)], self.requests)
        with self.assertRaises(ComputeFailure):
            self.jobs.submit('fault_panic', lambda: {}).result(timeout=5)
        self.requests.clear()
        self.assertEqual((self.record,), self.query())
        self.assertEqual([(identity, False), (identity, True)], self.requests)
        child = self.jobs._workers['interactive']._process
        self.assertIsNone(child.poll())
        self.assertEqual(0, self.jobs._failures['interactive'])

    def test_cancelled_or_oversized_input_is_not_retained(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(OperationCancelled):
            self.service.filter_records((self.record,), now=self.now, cancel_event=event)
        self.assertFalse(self.service._inputs._entries)
        inputs = InventoryQueryInputs()
        entry, referenced = inputs.prepare(
            ((replace(self.record, note='x' * (CACHE_BYTES + 1)), 'item', None),),
            search_mode=0,
            with_age=False,
            captured=lambda r: None,
            cancel_event=None,
        )
        self.assertFalse(referenced)
        self.assertGreater(entry['bytes'], CACHE_BYTES)
        inputs.remember(entry)
        self.assertFalse(inputs._entries)
        self.assertEqual(0, inputs._bytes)
