# 验证真实 Qt 快速评分的原生质量刷新、最新请求合并及非阻塞关闭。
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.quick_score import QuickRecordState, refresh_record
from echo_workshop.core.models import EchoRecord
from echo_workshop.ui.inventory.quick_score import QuickScoreDialog
from echo_workshop.ui.inventory.controller import InventoryContent
from tests.calculation.test_quick_quality import record_fixture, runtime_fixture
from tests.native_runtime import inventory_queries


class QuickQualityQt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.record = record_fixture()
        self.runtime = runtime_fixture(self.record, directory.name)
        self.task = SimpleNamespace(runtime=self.runtime, config={}, _destroyed=False)
        self.stack = self.enterContext(ExitStack())
        for name in ('validation_issues', 'usable'):
            self.stack.enter_context(
                patch.object(
                    EchoRecord,
                    name,
                    new_callable=PropertyMock,
                    side_effect=AssertionError(name),
                )
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            _, _, facts = pool.submit(
                inventory_queries.review, ((self.record, 'item', ()),)
            ).result(timeout=12)
        self.quality = facts[0]
        self.dialog = QuickScoreDialog(
            self.task, self.record, quality=self.quality, inventory_revision=7
        )
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.close)
        self.dialog.picker.characters.setCurrentIndex(
            self.dialog.picker.characters.findData('1304')
        )
        self.dialog.picker.modals.setCurrentIndex(
            self.dialog.picker.modals.findData(('1304', 'default'))
        )
        self.assertEqual(('1304', 'default'), self.dialog.picker.key())
        main_thread = threading.get_ident()
        self.runtime.repository.get.side_effect = lambda _: self.background_read(main_thread)

    def background_read(self, main_thread):
        self.assertNotEqual(main_thread, threading.get_ident(), 'GUI performed repository read')
        return self.runtime.repository.record

    def wait_until(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), self.dialog.summary.text())

    def test_changed_quality_never_auto_scores_and_unrelated_update_preserves_result(self):
        d = self.dialog
        with patch.object(
            self.runtime.scores, 'compare', wraps=self.runtime.scores.compare
        ) as score:
            d.start_score()
            self.wait_until(lambda: d.result is not None)
            old = d.result
            self.assertEqual(29.96, old.score.current_score)
            d._result_notice = '已保存适配范围提示'
            d._show_message(d._result_notice)
            with patch.object(
                inventory_queries.compute, 'submit', wraps=inventory_queries.compute.submit
            ) as submit:
                self.runtime.repository.revision = 8
                d.refresh_context()
                self.assertFalse(d.score_button.isEnabled())
                self.wait_until(lambda: d._seen_revision == 8)
                self.assertIs(old, d.result)
                self.assertEqual('已保存适配范围提示', d.summary.text())
                submit.assert_not_called()
                self.runtime.repository.record = replace(self.record, main_stats=())
                self.runtime.repository.revision = 9
                d.refresh_context()
                self.wait_until(lambda: d._seen_revision == 9)
                self.assertFalse(d._record_available)
                self.assertIsNone(d.result)
                self.assertIn('main:expected_two_slots', d.card.data.issues)
                self.assertEqual(1, submit.call_count)
            self.runtime.repository.record = replace(self.record, note='repaired')
            self.runtime.repository.revision = 10
            d.refresh_context()
            self.wait_until(lambda: d._seen_revision == 10)
            self.assertTrue(d._record_available)
            self.assertEqual(1, score.call_count)
            d.start_score()
            self.wait_until(lambda: d.result is not None)
            self.assertEqual(2, score.call_count)
        owner = SimpleNamespace(
            task=self.task,
            runtime=self.runtime,
            table=SimpleNamespace(selected_ids=lambda: (self.record.item_id,)),
            selected=lambda: (self.record, 'item', ()),
            inventory_model=SimpleNamespace(quality=Mock(return_value=self.quality)),
            _mutation_revision=Mock(return_value=7),
        )
        self.runtime.inventory.get = Mock(return_value=replace(self.record))
        with patch('echo_workshop.ui.inventory.quick_score.QuickScoreDialog') as dialog:
            InventoryContent.quick_score_selected(owner)
            dialog.assert_called_once_with(
                self.task, self.record, owner, quality=self.quality, inventory_revision=7
            )
            self.runtime.inventory.get.return_value = replace(self.record, note='changed again')
            with self.assertRaises(ValueError):
                InventoryContent.quick_score_selected(owner)
            self.assertEqual(1, dialog.call_count)

    def test_refresh_keeps_only_latest_revision_and_fault_requires_explicit_retry(self):
        d = self.dialog
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def controlled(*args, **kwargs):
            if kwargs['revision'] == 8:
                entered.set()
                self.assertTrue(release.wait(4))
            return refresh_record(*args, **kwargs)

        target = 'echo_workshop.ui.inventory.quick_score.refresh_record'
        with patch(target, side_effect=controlled):
            self.runtime.repository.revision = 8
            d.refresh_context()
            self.assertTrue(entered.wait(2))
            first = d._quality_future
            self.runtime.repository.record = replace(self.record, note='latest')
            self.runtime.repository.revision = 9
            d.refresh_context()
            self.assertIs(first, d._quality_future)
            self.assertEqual(9, d._quality_pending_revision)
            release.set()
            self.wait_until(lambda: d._seen_revision == 9)
            self.assertEqual('latest', d.record.note)
        self.runtime.repository.revision = 10
        with patch(target, side_effect=ComputeFailure('timeout', 'test failure')) as failing:
            d.refresh_context()
            self.wait_until(lambda: d._quality_failed_revision == 10)
            d.refresh_context()
            d.refresh_context()
            self.assertEqual(1, failing.call_count)
            self.assertEqual('重新核对', d.score_button.text())
        d.start_score()
        self.wait_until(lambda: d._seen_revision == 10)
        self.assertIsNone(d.result)
        self.assertIsNone(d._future)
        # A completed reply can still be cancelled until its publication.
        self.runtime.repository.revision = 11
        completed = Future()
        completed.set_result(QuickRecordState(11, d._quality))
        d._quality_future, d._quality_revision = completed, 11
        d.cancel_score()
        d._poll_quality()
        self.assertEqual(10, d._seen_revision)
        self.assertEqual(11, d._quality_failed_revision)

    def test_live_slow_refresh_keeps_heartbeat_and_close_does_not_join_worker(self):
        d = self.dialog
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(4))
            return refresh_record(*args, **kwargs)

        with patch('echo_workshop.ui.inventory.quick_score.refresh_record', side_effect=slow):
            self.runtime.repository.revision = 8
            d.refresh_context()
            self.assertTrue(entered.wait(2))
            future = d._quality_future
            ticks = []
            timer = QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            timer.start()
            QTest.qWait(120)
            timer.stop()
            self.assertGreaterEqual(len(ticks), 5)
            self.assertFalse(future.done())
            start = time.monotonic()
            d.close()
            self.assertLess(time.monotonic() - start, 0.25)
            self.assertFalse(future.done())
            release.set()
            self.wait_until(future.done)
            self.assertIsNone(d.result)
            self.assertEqual(7, d._seen_revision)
