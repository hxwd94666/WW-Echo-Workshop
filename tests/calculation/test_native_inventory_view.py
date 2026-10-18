# 验证原生视图分类、精确检索范围和最新请求发布，关闭不等待计算线程。
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.inventory_view import InventoryViewJob, ViewMetadata
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.filters import InventoryFilters
from echo_workshop.core.models import EchoRecord
from tests.native_runtime import _inventory_queries

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'
FIXTURE = Path(__file__).with_name('fixtures') / 'native_inventory_view.json'


class NativeInventoryView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def setUp(self):
        self.rows = tuple(
            (
                EchoRecord.from_dict(row['record']),
                row['kind'],
                ViewMetadata(row['search_text'], row['reservation']),
            )
            for row in self.data['rows']
        )

    def test_frozen_status_union_category_intersection_text_and_order_without_python_predicates(
        self,
    ):
        # Python 行谓词已删除；属性不存在比"调用即失败"是更强的保证。
        self.assertFalse(hasattr(InventoryFilters, 'accepts'))
        with ExitStack() as stack:
            for name in ('validation_issues', 'usable'):
                stack.enter_context(
                    patch.object(
                        EchoRecord,
                        name,
                        new_callable=PropertyMock,
                        side_effect=AssertionError(name),
                    )
                )
            stack.enter_context(
                patch(
                    'echo_workshop.resources.search.search_matches',
                    side_effect=AssertionError('Python search'),
                )
            )
            for case in self.data['cases']:
                with self.subTest(case=case):
                    actual = _inventory_queries.filter_view(
                        self.rows, InventoryFilters(**case['filters']), text=case['text']
                    )
                    self.assertEqual(tuple(case['indices']), actual)

    def test_reserved_and_search_metadata_participate_in_input_identity(self):
        record = self.rows[2][0]
        spec = InventoryFilters(statuses=('locked',))
        first = ((record, 'item', ViewMetadata('needle')),)
        reserved = ((record, 'item', ViewMetadata('needle', 'current-plan')),)
        other = ((record, 'item', ViewMetadata('other', 'current-plan')),)
        self.assertEqual((), _inventory_queries.filter_view(first, spec, text='needle'))
        self.assertEqual((0,), _inventory_queries.filter_view(reserved, spec, text='needle'))
        self.assertEqual((), _inventory_queries.filter_view(other, spec, text='needle'))

    def test_latest_request_coalesces_and_never_publishes_stale_context(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def evaluate(rows, filters, *, text, cancel_event):
            calls.append(text)
            if text == 'old':
                entered.set()
                self.assertTrue(release.wait(2))
                return (0,)
            return (1,)

        job = InventoryViewJob(queries=SimpleNamespace(filter_view=evaluate))
        try:
            job.request(self.rows, InventoryFilters(), text='old', context=('old-profile', 1))
            self.assertTrue(entered.wait(2))
            job.request(self.rows, InventoryFilters(), text='discarded', context=('mid', 2))
            generation = job.request(
                self.rows, InventoryFilters(), text='latest', context=('new-profile', 3)
            )
            release.set()
            self.assertTrue(job.wait_closed(2))
            status = job.status
            self.assertEqual(
                (generation, 'ready', ('new-profile', 3), (1,)),
                (status.generation, status.state, status.context, status.indices),
            )
            self.assertEqual(['old', 'latest'], calls)
        finally:
            release.set()
            job.close()
            self.assertTrue(job.wait_closed())

    def test_close_is_nonblocking_and_cancels_a_live_hung_native_child(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)
        jobs.submit('ping', lambda: {}).result(timeout=5)
        child = jobs._workers['interactive']._process
        entered = threading.Event()

        class Hang:
            def submit(self, operation, build, **options):
                future = jobs.submit('fault_hang', build, **options)
                entered.set()
                return future

        job = InventoryViewJob(queries=InventoryQueryService(compute=Hang()))
        try:
            job.request(self.rows, InventoryFilters(), context=('profile', 1))
            self.assertTrue(entered.wait(2))
            time.sleep(0.05)
            self.assertIsNone(child.poll())
            at = time.monotonic()
            job.close()
            self.assertLess(time.monotonic() - at, 0.1)
            self.assertTrue(job.wait_closed(1))
            self.assertEqual('closed', job.status.state)
            self.assertEqual((), job.status.indices)
        finally:
            job.close()
            jobs.close()
            self.assertTrue(jobs.wait_closed())
        self.assertIsNotNone(child.poll())

    def test_gui_wait_guard_and_cancelled_or_invalid_input_do_not_submit(self):
        gui = object()
        qt = SimpleNamespace(
            QCoreApplication=SimpleNamespace(instance=lambda: SimpleNamespace(thread=lambda: gui)),
            QThread=SimpleNamespace(currentThread=lambda: gui),
        )
        with (
            patch.dict(sys.modules, {'PySide6.QtCore': qt}),
            self.assertRaises(ComputeFailure) as caught,
        ):
            _inventory_queries.filter_view(self.rows, InventoryFilters())
        self.assertEqual('gui_thread', caught.exception.kind)
        job = InventoryViewJob(queries=_inventory_queries)
        with patch.dict(sys.modules, {'PySide6.QtCore': qt}), self.assertRaises(ComputeFailure):
            job.wait_closed(0.01)
        job.close()
        cancel = threading.Event()
        cancel.set()
        with patch.object(_inventory_queries.compute, 'submit') as submit:
            with self.assertRaises(OperationCancelled):
                _inventory_queries.filter_view(self.rows, InventoryFilters(), cancel_event=cancel)
            with self.assertRaises(ValueError):
                _inventory_queries.filter_view(
                    ((self.rows[0][0], 'item', None),), InventoryFilters()
                )
            with self.assertRaises(ComputeFailure) as caught:
                _inventory_queries.filter_view((self.rows[0],) * 20001, InventoryFilters())
            self.assertEqual('input_limit', caught.exception.kind)
        submit.assert_not_called()
