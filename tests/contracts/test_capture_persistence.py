# 验证收录写入失败时只撤回本次新增的截图证据。
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import zlib

from ww_echo_workshop.capture.intake import CaptureSession
from ww_echo_workshop.core.models import EchoRecord
from ww_echo_workshop.storage.evidence import EvidenceStore
from ww_echo_workshop.storage.inventory import EchoRepository


def synthetic_png():
    def chunk(kind, payload):
        return (
            struct.pack('>I', len(payload))
            + kind
            + payload
            + struct.pack('>I', zlib.crc32(kind + payload))
        )

    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(bytes((0, 80, 120, 160))))
        + chunk(b'IEND', b'')
    )


class CapturePersistenceContract(unittest.TestCase):
    def test_inventory_rollback_removes_only_new_evidence_and_allows_retry(self):
        image = synthetic_png()
        for duplicate, preexisting in ((False, False), (True, False), (True, True)):
            with (
                self.subTest(duplicate=duplicate, preexisting=preexisting),
                TemporaryDirectory() as folder,
                closing(EchoRepository(Path(folder))) as repository,
            ):
                evidence = EvidenceStore(Path(folder))
                profile = repository.create_profile('合成档案')
                previous = EchoRecord(profile, 4, (), level=5, name='original')
                previous = replace(previous, screenshot_ref=evidence.put(previous.item_id, image))
                repository.add(previous)
                candidate = replace(
                    previous,
                    item_id=str(uuid4()),
                    name='original' if duplicate else 'new',
                    screenshot_ref='',
                )
                if preexisting:
                    evidence.put(candidate.item_id, image)
                before = set(evidence.folder.glob('*.png'))
                capture = CaptureSession(
                    repository,
                    evidence,
                    quality=lambda rows, **_: [SimpleNamespace(record=rows[0], issues=())],
                )
                capture.start_scan(profile, 1)
                revision = repository.revision
                with patch.object(
                    repository, '_bump', side_effect=sqlite3.OperationalError('synthetic rollback')
                ):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'synthetic rollback'):
                        capture._store_record(candidate, image, commit_guard=lambda: True)
                self.assertEqual(revision, repository.revision)
                self.assertEqual([previous], repository.list(profile))
                self.assertEqual([], repository.pending_captures(profile))
                self.assertEqual((0, 0), (capture.status.added, capture.status.pending))
                self.assertEqual(before, set(evidence.folder.glob('*.png')))
                self.assertEqual(image, evidence.read(previous.screenshot_ref))
                self.assertTrue(capture._store_record(candidate, image, commit_guard=lambda: True))
                saved = (
                    repository.pending_captures(profile)[0][0]
                    if duplicate
                    else repository.get(candidate.item_id)
                )
                self.assertEqual(candidate.item_id, saved.item_id)
                self.assertEqual(image, evidence.read(saved.screenshot_ref))
                self.assertEqual(2, len(list(evidence.folder.glob('*.png'))))
