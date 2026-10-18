# 验证原生筛选的异步确认意图、已选 UUID 恢复和失效请求撤销。
from dataclasses import replace
from types import MethodType, SimpleNamespace
import time
import unittest
from unittest.mock import Mock

from PySide6.QtWidgets import QApplication, QWidget, QAbstractItemView
from PySide6.QtTest import QTest

from echo_workshop.application.matching.inventory import MatchInventorySession, InventoryViewState
from echo_workshop.ui.inventory.page import InventoryPage
from echo_workshop.ui.shell import EchoAssistantPanel
from tests.calculation.helpers import tuned
from tests.native_runtime import inventory_queries
from concurrent.futures import ThreadPoolExecutor

PROFILE = '00000000-0000-4000-8000-000000000001'


def _quality(rows):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(inventory_queries.review, rows).result(timeout=12)[2]


class _Library(InventoryPage):
    """Actual page/model/grid; only unrelated repository and capture work is omitted."""

    def __init__(self, rows, session):
        QWidget.__init__(self)
        self.runtime = SimpleNamespace(
            inventory_queries=inventory_queries,
            backups=SimpleNamespace(cancel=Mock(), status=SimpleNamespace(busy=False)),
            _closed=False,
        )
        self.task = SimpleNamespace(
            _destroyed=False,
            _panel=SimpleNamespace(
                _match_inventory_session=session,
                finish_inventory_selection=Mock(),
                cancel_inventory_selection=Mock(),
            ),
        )
        self._editor = None
        self._embedded_ready = False
        self._match_selection_restoring = False
        self._rows = rows
        self._reader = SimpleNamespace(status=SimpleNamespace(state='ready'))
        self.build_inventory_ui()
        self.profile_box.addItem('合成档案', userData=PROFILE)
        self.inventory_model.reset_rows(rows, quality=_quality(rows))
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        self._restore_match_selection()

    def show_details(self):
        pass

    def showEvent(self, event):
        QWidget.showEvent(self, event)

    def hideEvent(self, event):
        QWidget.hideEvent(self, event)

    def closeEvent(self, event):
        QWidget.closeEvent(self, event)

    def refresh(self, **kwargs):
        self._restore_match_selection()

    def _refresh_sonata_options(self):
        pass


class InventorySelectionContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.rows = tuple((tuned(PROFILE, name=f'Echo-{i}'), 'item', ()) for i in range(4))
        self.session = MatchInventorySession(PROFILE, None, InventoryViewState(PROFILE))
        self.library = _Library(self.rows, self.session)
        self.addCleanup(self.library.deleteLater)
        self.addCleanup(self.library.table.proxy.close_job)
        self.panel = None

    def tearDown(self):
        pending = getattr(self.panel, '_inventory_confirmation', None)
        if pending is not None:
            pending.cancel()

    def wait_until(self, condition):
        end = time.monotonic() + 6
        while not condition() and time.monotonic() < end:
            QTest.qWait(10)
        self.assertTrue(condition(), self.library.table.proxy.error)

    def search(self, text):
        self.library.search_box.setText(text)
        self.library.search_timer.stop()
        self.library._apply_search()
        self.wait_until(lambda: self.library.table.proxy.projection_ready)

    def make_panel(self, route='comparison'):
        self.session.return_route = route
        requester = SimpleNamespace(
            profile_box=self.library.profile_box,
            _rule_dirty=False,
            _generation=0,
            _apply_inventory_ids=Mock(),
            check_inventory_selection_context=Mock(),
            apply_inventory_selection=Mock(),
        )
        panel = self.panel = SimpleNamespace(
            _match_inventory_session=self.session,
            _match_origin=requester,
            _match_origin_context=None,
            _inventory_confirmation=None,
            proxy=SimpleNamespace(inventory_window=self.library),
            _restore_inventory_selection_view=Mock(),
            _activate=Mock(),
            feedback=Mock(),
        )
        panel._finish_inventory_selection = MethodType(
            EchoAssistantPanel._finish_inventory_selection, panel
        )
        return panel, requester

    def begin_confirmation(self, *, filtered=False, route='comparison'):
        self.library.table.restore_selection({self.rows[0][0].item_id})
        panel, requester = self.make_panel(route)
        self.library.search_box.setText('Echo-')
        self.library.search_timer.stop()
        job = Mock()
        response = panel._finish_inventory_selection(filtered=filtered, _job=job)
        self.assertTrue(response['deferred'])
        job.finish.assert_not_called()
        return panel, requester, job

    def test_previously_hidden_checks_are_restored_before_confirmation_sync(self):
        expected = {self.rows[i][0].item_id for i in (0, 3)}
        self.library.table.restore_selection(expected)
        self.search('Echo-0')
        self.search('Echo-3')
        self.assertEqual(self.library.table.selected_ids(), (self.rows[3][0].item_id,))
        self.library._sync_match_selection()
        self.assertEqual(self.session.selected_ids, expected)

    def test_active_filter_model_refresh_retains_uuid_draft(self):
        expected = {self.rows[i][0].item_id for i in (0, 3)}
        self.library.table.restore_selection(expected)
        self.search('Echo-0')
        self.library._rows = tuple(
            (replace(record, note='updated'), kind, detail) for record, kind, detail in self.rows
        ) + ((tuned(PROFILE, name='Other'), 'item', ()),)
        self.library.inventory_model.reset_rows(
            self.library._rows, quality=_quality(self.library._rows)
        )
        self.wait_until(lambda: self.library.table.proxy.projection_ready)
        self.assertEqual(self.library.table.selected_ids(), (self.rows[0][0].item_id,))
        self.library._sync_match_selection()
        self.assertEqual(self.session.selected_ids, expected)

    def test_deferred_checked_confirmation_keeps_clicked_uuid_snapshot(self):
        panel, requester, job = self.begin_confirmation()
        expected = frozenset({self.rows[0][0].item_id})
        self.session.clear_selection(PROFILE)  # Later draft changes cannot reinterpret the click.
        self.wait_until(lambda: requester._apply_inventory_ids.called)
        requester._apply_inventory_ids.assert_called_once_with(expected)
        self.assertIsNone(panel._inventory_confirmation)
        job.finish.assert_called_once()

    def test_deferred_filtered_confirmation_uses_visible_ids_for_allocation(self):
        panel, requester, job = self.begin_confirmation(filtered=True, route='allocation')
        self.wait_until(lambda: requester.apply_inventory_selection.called)
        requester.apply_inventory_selection.assert_called_once_with(
            frozenset(record.item_id for record, _, _ in self.rows)
        )
        self.assertIsNone(panel._inventory_confirmation)
        job.finish.assert_called_once()

    def test_new_filter_revokes_confirmation_and_disconnects_callback(self):
        panel, requester, job = self.begin_confirmation()
        pending = panel._inventory_confirmation
        self.search('Echo-3')
        self.assertIsNone(panel._inventory_confirmation)
        self.assertEqual(pending._connections, [])
        requester._apply_inventory_ids.assert_not_called()
        self.assertTrue(self.session.active)
        job.finish.assert_called_once_with('cancelled')

    def test_replaced_session_never_commits_old_request(self):
        panel, requester, job = self.begin_confirmation()
        panel._match_inventory_session = MatchInventorySession(
            PROFILE, None, InventoryViewState(PROFILE)
        )
        self.wait_until(lambda: panel._inventory_confirmation is None)
        requester._apply_inventory_ids.assert_not_called()
        job.finish.assert_called_once_with('cancelled')

    def test_changed_requester_requirements_never_commit(self):
        panel, requester, job = self.begin_confirmation()
        requester._cost_layouts = {('1304', 'default'): (4, 3, 3, 1, 1)}
        self.wait_until(lambda: panel._inventory_confirmation is None)
        requester._apply_inventory_ids.assert_not_called()
        job.finish.assert_called_once_with('cancelled')

    def test_close_before_dispatch_revokes_pending_confirmation(self):
        panel, requester, job = self.begin_confirmation()
        self.library.table.proxy.close_job()
        self.wait_until(lambda: panel._inventory_confirmation is None)
        requester._apply_inventory_ids.assert_not_called()
        self.assertTrue(self.session.active)
        job.finish.assert_called_once_with('cancelled')

    def test_native_filter_failure_revokes_confirmation_without_commit(self):
        panel, requester, job = self.begin_confirmation()
        self.library.table.proxy._publish(frozenset(), error='synthetic filter failure')
        self.assertIsNone(panel._inventory_confirmation)
        requester._apply_inventory_ids.assert_not_called()
        self.assertTrue(self.session.active)
        job.finish.assert_called_once_with('cancelled')
