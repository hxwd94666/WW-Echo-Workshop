# 验证自定义权重原生投影、归一化、原子保存与编辑器异步边界。
import ast
from concurrent.futures import Future
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from echo_workshop.application.custom_weights import CustomWeightService
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import canonical_json
from tests.calculation.helpers import set_custom_template
from echo_workshop.resources.weights import WeightProvider, WeightSnapshot
from echo_workshop.resources.weight_updates import WeightUpdateService
from tests.native_runtime import _jobs

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name('fixtures') / 'native_custom_weights.json'
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


class NativeCustomWeights(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def setUp(self):
        self.service = CustomWeightService(compute=_jobs)
        self.case = self.data['cases'][0]
        self.args = dict(identity=self.case['key'], weight_version=self.data['weight_version'])

    def test_all_frozen_projection_build_and_validation_are_exact_without_python_math(self):
        with ExitStack() as stack:
            for name in ('_entry_weight', '_main_name', '_score_entry', 'calculate_echo_score'):
                stack.enter_context(
                    patch(
                        'echo_workshop.core.scoring.' + name,
                        side_effect=AssertionError(name),
                        create=True,
                    )
                )
            for case in self.data['cases']:
                args = dict(identity=case['key'], weight_version=self.data['weight_version'])
                with self.subTest(key=case['key']):
                    values = self.service.editable_values(case['original'], **args)
                    self.assertEqual(canonical_json(case['projection']), canonical_json(values))
                    template = self.service.build(
                        case['original'], case['main'], case['sub'], **args
                    )
                    self.assertEqual(canonical_json(case['expected']), canonical_json(template))
                    self.assertEqual(
                        template, self.service.validate(case['original'], template, **args)
                    )

    def test_invalid_numeric_fields_do_not_restart_a_healthy_worker(self):
        self.service.editable_values(self.case['original'], **self.args)
        child = _jobs._workers['interactive']._process
        with self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'):
            for value in (-1, True, 1e6 + 1, float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    self.service.build(
                        self.case['original'],
                        self.case['main'],
                        dict(self.case['sub'], 暴击=value),
                        **self.args,
                    )
            with self.assertRaises(ValueError):
                self.service.build(self.case['original'], {}, self.case['sub'], **self.args)
            for value in (float('nan'), float('inf')):
                edited = deepcopy(self.case['expected'])
                edited['score_max'][0] = value
                with self.assertRaises(ValueError):
                    self.service.validate(self.case['original'], edited, **self.args)
        template = self.service.build(
            self.case['original'], self.case['main'], self.case['sub'], **self.args
        )
        self.assertEqual(self.case['expected'], template)
        self.assertIs(child, _jobs._workers['interactive']._process)
        self.assertIsNone(child.poll())
        self.assertEqual(0, _jobs._failures['interactive'])

    def test_provider_atomic_publication_and_rejection_preserve_current_files(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = WeightProvider(folder, custom_evaluator=self.service)
            base = provider.active
            case = next(c for c in self.data['cases'] if c['key'] == ['1304', 'default'])
            sub = dict(case['sub'], 暴击=case['sub']['暴击'] + 0.25)
            self.assertTrue(
                provider.set_custom_values(
                    '1304', 'default', case['main'], sub, expected_version=base.version
                )
            )
            current = provider.active
            files = {
                p.relative_to(folder).as_posix(): p.read_bytes()
                for p in Path(folder).rglob('*')
                if p.is_file()
            }
            template = (
                deepcopy(current.templates['1304']['default'])
                if isinstance(current.templates['1304']['default'], dict)
                else json.loads(canonical_json(current.to_dict()['templates']['1304']['default']))
            )
            template['score_max'][0] += 1
            with (
                self.assertRaises(ValueError),
                self.assertLogs('ok.Echo Workshop.Jobs', level='ERROR'),
            ):
                set_custom_template(
                    provider, '1304', 'default', template, expected_version=current.version
                )
            event = threading.Event()
            event.set()
            with self.assertRaises(OperationCancelled):
                provider.set_custom_values(
                    '1304',
                    'default',
                    case['main'],
                    sub,
                    expected_version=current.version,
                    cancel_event=event,
                )
            with patch.object(_jobs, 'submit') as submit, self.assertRaises(ValueError):
                provider.set_custom_values(
                    '1304', 'default', case['main'], sub, expected_version=base.version
                )
            submit.assert_not_called()
            after = {
                p.relative_to(folder).as_posix(): p.read_bytes()
                for p in Path(folder).rglob('*')
                if p.is_file()
            }
            self.assertEqual(files, after)
            self.assertEqual(current.version, provider.active.version)
            self.assertEqual(current.version, WeightProvider(folder).active.version)
            self.assertEqual({'1304'}, set(provider.customized_characters))
            self.assertTrue(
                provider.reset_custom_character('1304', expected_version=current.version)
            )
            self.assertEqual(base.version, provider.active.version)

    def test_update_worker_builds_only_after_explicit_request_and_publishes_once(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = WeightProvider(folder, custom_evaluator=self.service)
            updater = WeightUpdateService(
                provider, folder, client=SimpleNamespace(), jitter=lambda: 0
            )
            base = provider.active.version
            with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
                self.assertIsNone(updater._thread)
                self.assertEqual(0, submit.call_count)
                self.assertTrue(
                    updater.request_custom(
                        'default',
                        'default',
                        main=self.case['main'],
                        sub=self.case['sub'],
                        expected_version=base,
                    )
                )
                deadline = time.monotonic() + 3
                while (
                    updater.status.state in ('queued', 'checking') and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual('customized', updater.status.state)
                self.assertEqual(1, submit.call_count)
            updater.stop()
            self.assertFalse(updater._thread.is_alive())
            self.assertIn('default', provider.customized_characters)

    def test_projection_is_gui_submit_only_and_sync_build_checks_main_thread(self):
        app = SimpleNamespace(thread=lambda: 'gui')
        qt = SimpleNamespace(
            QCoreApplication=SimpleNamespace(instance=lambda: app),
            QThread=SimpleNamespace(currentThread=lambda: 'gui'),
        )
        with patch.dict(sys.modules, {'PySide6.QtCore': qt}):
            future = self.service.project_async(self.case['original'], **self.args)
            with self.assertRaises(ComputeFailure) as failed:
                self.service.build(
                    self.case['original'], self.case['main'], self.case['sub'], **self.args
                )
            self.assertEqual('gui_thread', failed.exception.kind)
        value = future.result(timeout=3)
        self.assertEqual(
            canonical_json(self.case['projection']),
            canonical_json(self.service.projected_values(value, **self.args)),
        )

    def test_actual_editor_methods_never_wait_on_pending_future_or_build_on_gui(self):
        snapshot = WeightSnapshot.builtin()
        future = Future()
        service = SimpleNamespace(
            project_async=Mock(return_value=future),
            projected_values=CustomWeightService.projected_values,
        )
        fake = SimpleNamespace(
            _projection=None,
            _ready=False,
            grid=SimpleNamespace(count=lambda: 0),
            characters=SimpleNamespace(currentData=lambda: '1304'),
            variants=SimpleNamespace(currentData=lambda: 'default'),
            runtime=SimpleNamespace(
                weights=SimpleNamespace(active=snapshot),
                custom_weights=service,
                updates=SimpleNamespace(request_custom=Mock(return_value=True)),
            ),
            reset_button=Mock(),
            state=Mock(),
            feedback=Mock(),
            body=Mock(),
            drafts={},
            _status=Mock(),
            _show_values=Mock(),
        )
        for name in ('_load', '_cancel_projection', '_poll_projection'):
            setattr(fake, name, MethodType(self.editor_method(name), fake))
        with patch.object(
            future, 'result', side_effect=AssertionError('GUI waited on unfinished request')
        ):
            fake._load()
            fake._poll_projection()
        self.assertIs(future, fake._projection[0])
        self.assertFalse(fake._ready)
        case = next(c for c in self.data['cases'] if c['key'] == ['1304', 'default'])
        future.set_result(
            dict(
                identity=['1304', 'default'],
                weight_version=snapshot.version,
                main=case['projection'][0],
                sub=case['projection'][1],
            )
        )
        fake._poll_projection()
        fake._show_values.assert_called_once_with(*case['projection'])
        stale = Future()
        fake._projection = (stale, threading.Event(), ('1304', 'default'), snapshot.version)
        fake._cancel_projection()
        self.assertIsNone(fake._projection)
        fake._show_values.reset_mock()
        fake._poll_projection()
        fake._show_values.assert_not_called()
        fake._ready = True
        fake.pending = False
        fake._valid_character = lambda: True
        fake.refresh = Mock()
        fake.main_controls = {('1', '攻击%'): Mock()}
        fake.sub_controls = {'暴击': Mock()}
        fake.values = lambda: (case['main'], case['sub'])
        with patch.object(
            self.service, 'build', side_effect=AssertionError('GUI built custom template')
        ):
            self.editor_method('save')(fake)
        fake.runtime.updates.request_custom.assert_called_once_with(
            '1304', 'default', main=case['main'], sub=case['sub'], expected_version=snapshot.version
        )
        self.assertTrue(fake.pending)

    def test_missing_service_bad_identity_and_cancel_have_no_python_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = WeightProvider(folder)
            with self.assertRaises(ValueError):
                set_custom_template(provider, 'default', 'default', self.case['expected'])
            self.assertFalse((Path(folder) / 'weights/current.json').exists())
        event = threading.Event()
        event.set()
        with patch.object(_jobs, 'submit') as submit, self.assertRaises(OperationCancelled):
            self.service.build(
                self.case['original'],
                self.case['main'],
                self.case['sub'],
                cancel_event=event,
                **self.args,
            )
        submit.assert_not_called()
        with self.assertRaises(ComputeFailure):
            self.service.projected_values(
                dict(identity=['wrong', 'default'], weight_version=self.data['weight_version']),
                **self.args,
            )
        from echo_workshop.resources import custom_weights

        for name in (
            'editable_values',
            'normalize_maximum',
            'build_custom_template',
            'validate_edit',
        ):
            self.assertFalse(hasattr(custom_weights, name))

    def test_child_panic_or_missing_engine_preserves_provider_until_a_fresh_valid_save(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)

        class FaultOnce:
            failed = False

            def submit(self, operation, build, **options):
                if not self.failed:
                    self.failed = True
                    operation = 'fault_panic'
                return jobs.submit(operation, build, **options)

        service = CustomWeightService(compute=FaultOnce())
        try:
            with tempfile.TemporaryDirectory() as folder:
                provider = WeightProvider(folder, custom_evaluator=service)
                version = provider.active.version
                with self.assertRaises(ComputeFailure):
                    provider.set_custom_values(
                        'default',
                        'default',
                        self.case['main'],
                        self.case['sub'],
                        expected_version=version,
                    )
                self.assertEqual(version, provider.active.version)
                self.assertFalse((Path(folder) / 'weights/current.json').exists())
                self.assertTrue(
                    provider.set_custom_values(
                        'default',
                        'default',
                        self.case['main'],
                        self.case['sub'],
                        expected_version=version,
                    )
                )
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())
        jobs = ComputeJobs(ROOT / 'build/missing-p12-engine.exe')
        try:
            missing = CustomWeightService(compute=jobs)
            with self.assertRaises(ComputeFailure):
                missing.build(
                    self.case['original'], self.case['main'], self.case['sub'], **self.args
                )
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    @staticmethod
    def editor_method(name):
        source = ROOT / 'src/echo_workshop/ui/settings/weights.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CustomWeightEditor'
        )
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
        namespace = dict(
            threading=threading,
            ComputeFailure=ComputeFailure,
            OperationCancelled=OperationCancelled,
        )
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        return namespace[name]


if __name__ == '__main__':
    unittest.main()
