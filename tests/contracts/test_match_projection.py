# 验证真实 Qt 卡片只映射原生投影，角色切换不重算且过期矩阵不发布。
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.match_projection import MatchProjectionService
from echo_workshop.ui.matching.cards import MatchResultCards
from echo_workshop.ui.matching.matrix_model import MatrixModel
from tests.calculation.test_match_projection import FIXTURE, case_inputs
from tests.native_runtime import _jobs


class MatchProjectionContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.case = json.loads(FIXTURE.read_text('utf-8'))['cases'][0]
        cls.records, cls.keys, cls.scores = case_inputs(cls.case)
        cls.service = MatchProjectionService(compute=_jobs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            cls.projection = pool.submit(
                cls.service.project, cls.records, cls.keys, cls.scores
            ).result(timeout=10)

    def setUp(self):
        self.model = MatrixModel()
        self.model.reset_matrix(
            self.records,
            self.keys,
            self.scores,
            ('R0', 'R1', 'R2'),
            projections=self.projection,
        )
        self.cards = MatchResultCards()
        self.addCleanup(self.cards.deleteLater)
        self.addCleanup(self.model.deleteLater)

    def test_actual_role_switch_maps_frozen_order_and_never_submits_compute(self):
        with patch.object(_jobs, 'submit', side_effect=AssertionError('GUI initiated compute')):
            self.cards.project(self.model)
            for index, expected in enumerate(self.case['expected_views']):
                key = None if index == 0 else self.keys[index - 1]
                self.cards.role_selector.setCurrentIndex(self.cards.role_selector.findData(key))
                self.assertEqual(
                    tuple(self.records[row].item_id for row, _ in expected['entries']),
                    tuple(record.item_id for record in self.cards.source.records),
                )
                for position, (row, column) in enumerate(expected['entries']):
                    record, _, score = self.cards.source.rows[position]
                    self.assertIs(record, self.records[row])
                    self.assertIs(score, self.scores[row * len(self.keys) + column])
                mapped = self.cards._map_projection(key)
                self.assertEqual((expected['excluded'], expected['unknown']), mapped[2:])
                # Passive refresh of the same matrix also reuses the projection.
                self.cards.project(self.model, current_key=key)

    def test_stale_projection_cannot_attach_to_another_matrix(self):
        with self.assertRaises(ValueError):
            self.model.reset_matrix(
                self.records[::-1],
                self.keys,
                self.scores,
                ('R0', 'R1', 'R2'),
                projections=self.projection,
            )
        self.model.reset_matrix()
        self.assertIsNone(self.model.projections)
        self.cards.project(self.model)
        self.assertEqual(self.cards.source.records, ())

    def test_gui_cannot_wait_for_native_projection(self):
        with self.assertRaises(ComputeFailure) as error:
            self.service.project(self.records, self.keys, self.scores)
        self.assertEqual('gui_thread', error.exception.kind)
