# 验证实际 Qt 库存模型与卡片只映射原生结论，并拒绝缺失或过期证据。
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import PropertyMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from echo_workshop.core.models import EchoRecord
from echo_workshop.ui.inventory.cards import EchoCardGrid
from echo_workshop.ui.inventory.model import InventoryModel
from tests.native_runtime import inventory_queries

FIXTURE = Path(__file__).parents[1] / 'calculation/fixtures/review_quality.json'


class ReviewQualityQt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cases = json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']
        self.rows = tuple(
            (EchoRecord.from_dict(case['record']), case['kind'], ()) for case in self.cases
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            _, _, self.facts = pool.submit(inventory_queries.review, self.rows).result(timeout=12)
        self.model = InventoryModel()
        self.addCleanup(self.model.deleteLater)

    def test_model_and_real_delegate_use_native_quality_without_rpc_or_python_predicate(self):
        with (
            patch.object(
                EchoRecord,
                'validation_issues',
                new_callable=PropertyMock,
                side_effect=AssertionError('Python quality'),
            ),
            patch.object(
                EchoRecord,
                'usable',
                new_callable=PropertyMock,
                side_effect=AssertionError('Python usable'),
            ),
            patch.object(
                inventory_queries.compute, 'submit', side_effect=AssertionError('GUI RPC')
            ),
        ):
            self.model.reset_rows(self.rows, quality=self.facts)
            grid = EchoCardGrid()
            self.addCleanup(grid.deleteLater)
            self.addCleanup(grid.proxy.close_job)
            grid.setModel(self.model)
            delegate = grid.itemDelegate()
            for index, case in enumerate(self.cases):
                source = self.model.index(index, 0)
                self.assertEqual(case['model_status'], source.data(Qt.DisplayRole))
                self.assertIs(self.facts[index], source.data(int(Qt.UserRole) + 7))
                card = delegate.projection(grid.proxy.mapFromSource(source))
                self.assertEqual(tuple(case['issues']), card.issues)
                self.assertEqual(case['card']['state'], card.state)
            self.assertFalse(self.model.reset_rows(self.rows, quality=self.facts))

    def test_missing_and_changed_record_evidence_leave_existing_model_untouched(self):
        self.model.reset_rows(self.rows, quality=self.facts)
        revision = self.model.revision
        with self.assertRaises(ValueError):
            self.model.reset_rows(self.rows)
        changed = (
            (replace(self.rows[0][0], note='edited', main_stats=()), *self.rows[0][1:]),
        ) + self.rows[1:]
        with self.assertRaises(ValueError):
            self.model.reset_rows(changed, quality=self.facts)
        self.assertEqual(revision, self.model.revision)
        self.assertIs(self.rows[0][0], self.model.rows[0][0])
        self.assertIs(self.facts[0], self.model.quality(self.rows[0][0]))
        before = self.model.index(0, 0).data(Qt.DisplayRole)
        with ThreadPoolExecutor(max_workers=1) as pool:
            _, _, fresh = pool.submit(inventory_queries.review, changed).result(timeout=12)
        with patch.object(
            EchoRecord,
            'validation_issues',
            new_callable=PropertyMock,
            side_effect=AssertionError('Python quality'),
        ):
            self.assertTrue(self.model.reset_rows(changed, quality=fresh))
            self.assertNotEqual(before, self.model.index(0, 0).data(Qt.DisplayRole))
            self.assertIn('main:expected_two_slots', self.model.quality(changed[0][0]).issues)
