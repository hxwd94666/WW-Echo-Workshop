# 验证原生固定候选证据、完整目录、显式依赖和界面零计算投影。
import ast
from concurrent.futures import Future
from contextlib import ExitStack
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.pins import prepare_pin_catalog
from echo_workshop.application.plan_jobs import PlanCommands
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import EchoRecord, canonical_json
from tests.native_runtime import _inventory_queries

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name('fixtures') / 'native_pin_catalog.json'
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def catalog_json(catalog):
    return dict(
        item_ids=[r.item_id for r in catalog.records],
        labels=catalog.labels,
        display_labels=catalog.display_labels,
        positions=dict(catalog.positions),
        usable=catalog.usable,
        cost_positions={str(cost): sorted(rows) for cost, rows in catalog.cost_positions.items()},
    )


class NativePinCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']

    def test_frozen_catalogs_and_cost_masks_without_python_business_evaluation(self):
        inputs = [
            tuple(EchoRecord.from_dict(row) for row in case['records']) for case in self.cases
        ]
        with ExitStack() as stack:
            for name in ('validation_issues', 'usable', 'fully_tuned'):
                stack.enter_context(
                    patch.object(
                        EchoRecord,
                        name,
                        new_callable=PropertyMock,
                        side_effect=AssertionError(name),
                    )
                )
            for case, records in zip(self.cases, inputs):
                with self.subTest(usable_only=case['usable_only'], size=len(records)):
                    catalog = prepare_pin_catalog(
                        records, queries=_inventory_queries, usable_only=case['usable_only']
                    )
                    self.assertEqual(
                        canonical_json(case['expected']), canonical_json(catalog_json(catalog))
                    )
                    for record in catalog.records:
                        self.assertTrue(any(record is source for source in records))
                    with self.assertRaises(TypeError):
                        catalog.cost_positions[1] = frozenset()

    def test_explicit_owner_missing_owner_and_cancelled_input(self):
        records = tuple(EchoRecord.from_dict(row) for row in self.cases[0]['records'])
        repository = Mock()
        repository.inventory_snapshot.return_value = (19, records)
        command = PlanCommands(None, repository, queries=_inventory_queries)
        with patch.object(
            EchoRecord,
            'validation_issues',
            new_callable=PropertyMock,
            side_effect=AssertionError('Python validation'),
        ):
            result = command.execute(
                'edit-role', dict(profile_id=records[0].profile_id), scope_generation=23
            )
        self.assertEqual(19, result.payload['revision'])
        self.assertEqual(23, result.payload['scope_generation'])
        self.assertEqual(
            canonical_json(self.cases[0]['expected']),
            canonical_json(catalog_json(result.payload['catalog'])),
        )
        with self.assertRaises(ValueError):
            prepare_pin_catalog(records, queries=None)
        cancel = threading.Event()
        cancel.set()
        with (
            patch.object(_inventory_queries, 'inspect_pins') as inspect,
            self.assertRaises(OperationCancelled),
        ):
            prepare_pin_catalog(records, queries=_inventory_queries, cancel_event=cancel)
        inspect.assert_not_called()

    def test_native_failure_never_falls_back_to_python_catalog_evaluation(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)

        class PanicOnce:
            failed = False

            def submit(self, operation, build, **options):
                if not self.failed:
                    self.failed = True
                    operation = 'fault_panic'
                return jobs.submit(operation, build, **options)

        queries = InventoryQueryService(compute=PanicOnce())
        records = tuple(EchoRecord.from_dict(row) for row in self.cases[0]['records'])
        try:
            with (
                self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'),
                self.assertRaises(ComputeFailure),
            ):
                prepare_pin_catalog(records, queries=queries)
            self.assertFalse(queries._inputs._entries)
            catalog = prepare_pin_catalog(records, queries=queries)
            self.assertEqual(
                canonical_json(self.cases[0]['expected']), canonical_json(catalog_json(catalog))
            )
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    def test_invalid_native_flags_and_cost_partition_are_rejected_before_publication(self):
        record = EchoRecord.from_dict(self.cases[0]['records'][0])

        class Replies:
            def __init__(self, damage):
                self.damage = damage

            def submit(self, operation, build, **kwargs):
                payload = build()
                value = dict(
                    query_id=payload['query_id'],
                    mode='pins',
                    snapshot_id=payload['snapshot_id'],
                    snapshot_cached=True,
                    indices=[0],
                    item_ids=[record.item_id],
                    counts=dict(saved=1, pending=0, confirmed=1, eligible=1),
                    facts=[dict(usable=True, needs_correction=False)],
                    cost_positions={'1': [0, 1], '3': [0], '4': [0]},
                )
                self.damage(value)
                future = Future()
                future.set_result(value)
                return future

        damages = (
            lambda value: value.update(facts=[]),
            lambda value: value['facts'][0].update(usable=1),
            lambda value: value.update(cost_positions={'1': [0, 1]}),
            lambda value: value['cost_positions'].update({'3': [0, 1]}),
            lambda value: value['cost_positions'].update({'1': [0]}),
            lambda value: value['cost_positions'].update({'1': [0, 2]}),
        )
        with self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'):
            for damage in damages:
                with self.subTest(damage=damage), self.assertRaises(ComputeFailure):
                    prepare_pin_catalog(
                        (record,), queries=InventoryQueryService(compute=Replies(damage))
                    )

    def test_widgets_project_prepared_catalog_and_native_cost_membership_only(self):
        path = ROOT / 'src/echo_workshop/ui/allocation/pin_model.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cost_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == 'CostPinModel'
        )
        method = next(
            node
            for node in cost_class.body
            if isinstance(node, ast.FunctionDef) and node.name == 'filterAcceptsRow'
        )
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                str(path),
                'exec',
            ),
            namespace,
        )
        records = tuple(EchoRecord.from_dict(row) for row in self.cases[0]['records'])
        catalog = prepare_pin_catalog(records, queries=_inventory_queries)
        fake = SimpleNamespace(sourceModel=lambda: SimpleNamespace(catalog=catalog))
        for cost in (None, 1, 3, 4):
            fake.cost = cost
            for row in range(len(catalog.records) + 2):
                self.assertEqual(
                    cost is None or row in catalog.cost_positions[cost],
                    namespace['filterAcceptsRow'](fake, row, None),
                )
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ('usable', 'validation_issues'))
            if isinstance(node, ast.Name):
                self.assertNotEqual('prepare_pin_catalog', node.id)
        editor = ast.parse(
            (ROOT / 'src/echo_workshop/ui/allocation/editor.py').read_text(encoding='utf-8')
        )
        self.assertFalse(
            any(
                isinstance(node, ast.ClassDef) and node.name == 'RoleEditor' for node in editor.body
            )
        )
        content = next(
            node
            for node in editor.body
            if isinstance(node, ast.ClassDef) and node.name == 'RoleEditorContent'
        )
        ctor = next(
            node
            for node in content.body
            if isinstance(node, ast.FunctionDef) and node.name == '__init__'
        )
        self.assertEqual('catalog', ctor.args.kwonlyargs[0].arg)
        self.assertIsNone(ctor.args.kw_defaults[0])
