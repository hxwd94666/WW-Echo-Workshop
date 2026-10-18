# 验证收录观测的原生数值合同、几何不缓存、显式模板和故障取消边界。
from contextlib import ExitStack
import ast
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from echo_workshop.adapters.okww.stat_overlay import analyze_echo_stats
from echo_workshop.application.observations import ObservationService
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.live_selection import LiveSelection
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import StatValue, canonical_json
from echo_workshop.resources.weights import WeightSnapshot
from tests.native_runtime import _jobs

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name('fixtures') / 'native_observations.json'
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def numeric_value(service, case, snapshot):
    value = service.evaluate(
        tuple(StatValue(**r) for r in case['main_rows']),
        tuple(StatValue(**r) for r in case['sub_rows']),
        selections=tuple((c, m, snapshot.scoring_template(c, m)) for c, m in case['selections']),
        weight_version=snapshot.version,
        cost=case['cost'],
        cost_key=case['cost_key'],
    )
    return dict(
        cost=value.cost,
        cost_key=value.cost_key,
        scores=[
            dict(character_id=k[0], modal_id=k[1], score=asdict(s) if s else None)
            for k, s in value.scores
        ],
        tiers=[asdict(t) if t is not None else None for t in value.tiers],
    )


def ocr_value(service, case, snapshot):
    options = dict(case['options'])
    invalid = options.pop('invalid_template', False)
    actual = options.pop('actual_resolver', False)

    def resolver(boxes, kind):
        if invalid:
            raise ValueError('指定模版已删除')
        if actual:
            from echo_workshop.application.live_selection import resolve_live_selection

            return resolve_live_selection(
                snapshot, boxes, fixed=('1304', 'default'), page_kind=kind
            )
        key = ('1304', 'default')
        template = snapshot.scoring_template(*key)
        return LiveSelection(key, template['name'], template, snapshot.version, '固定角色 / 模态')

    return asdict(
        analyze_echo_stats(
            [SimpleNamespace(**b) for b in case['boxes']],
            case['width'],
            case['height'],
            '今汐-通用',
            template_resolver=resolver,
            observation_service=service,
            weight_version=snapshot.version,
            **options,
        )
    )


class NativeObservations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        cls.snapshot = WeightSnapshot.builtin()

    def test_frozen_numeric_and_ocr_outputs_are_byte_exact_without_python_evaluators(self):
        self.assertEqual(self.data['weight_version'], self.snapshot.version)
        service = ObservationService(compute=_jobs)
        with ExitStack() as stack:
            for name in (
                'calculate_echo_score',
                '_entry_weight',
                '_score_entry',
                'substat_tier',
                'substat_tier_label',
            ):
                stack.enter_context(
                    patch(
                        'echo_workshop.core.scoring.' + name,
                        side_effect=AssertionError(name),
                        create=True,
                    )
                )
            for kind, project in [('numeric_cases', numeric_value), ('ocr_cases', ocr_value)]:
                for case in self.data[kind]:
                    with self.subTest(kind=kind, case=case['name']):
                        self.assertEqual(
                            canonical_json(case['expected']),
                            canonical_json(project(service, case, self.snapshot)),
                        )

    def test_reference_templates_are_one_batch_and_no_ocr_geometry_crosses_process(self):
        case = self.data['numeric_cases'][-1]
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            numeric_value(ObservationService(compute=_jobs), case, self.snapshot)
        self.assertEqual(1, submit.call_count)
        args, options = submit.call_args
        self.assertEqual('observe_stats', args[0])
        payload = args[1]()
        self.assertEqual(len(case['selections']), len(payload['selections']))
        self.assertEqual({'stat_name', 'value'}, set(payload['main_rows'][0]))
        self.assertEqual({'timeout', 'cancel_event'}, set(options))

    def test_repeated_numerics_reuse_cache_but_rebuild_geometry_and_track_content(self):
        case = next(c for c in self.data['ocr_cases'] if c['name'] == 'actual_metadata_resolver')
        service = ObservationService(compute=_jobs)
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            original = ocr_value(service, case, self.snapshot)
            changed = dict(case, boxes=[dict(b, x=b['x'] + 1) for b in case['boxes']])
            moved = ocr_value(service, changed, self.snapshot)
            self.assertEqual(1, submit.call_count)
            self.assertEqual(original['row_scores'], moved['row_scores'])
            self.assertEqual(original['rectangles'][0]['x'] + 1, moved['rectangles'][0]['x'])
            inferred = next(c for c in self.data['ocr_cases'] if c['name'] == 'inferred_cost')
            ocr_value(service, inferred, self.snapshot)
            self.assertEqual(2, submit.call_count)
        numeric = self.data['numeric_cases'][0]
        # An explicit snapshot version alone is never enough to reuse changed template contents.
        main = tuple(StatValue(**r) for r in numeric['main_rows'])
        template = dict(self.snapshot.scoring_template('1304', 'default'))
        for i in range(10):
            service.evaluate(
                main,
                (),
                selections=(('1304', 'default', template),),
                weight_version=self.snapshot.version,
                cost=1,
                cost_key=str(i),
            )
        self.assertEqual(8, len(service._cache))
        large = dict(template, display_only='x' * 70000)
        before = tuple(service._cache)
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            for _ in range(2):
                service.evaluate(
                    main,
                    (),
                    selections=(('1304', 'default', large),),
                    weight_version=self.snapshot.version,
                    cost=1,
                )
        self.assertEqual(2, submit.call_count)
        self.assertEqual(before, tuple(service._cache))
        edited = dict(template, score_max=[value * 2 for value in template['score_max']])
        before_score = service.evaluate(
            main,
            (),
            selections=(('1304', 'default', template),),
            weight_version=self.snapshot.version,
            cost=1,
        ).score_for(('1304', 'default'))
        changed_score = service.evaluate(
            main,
            (),
            selections=(('1304', 'default', edited),),
            weight_version=self.snapshot.version,
            cost=1,
        ).score_for(('1304', 'default'))
        self.assertNotEqual(before_score.potential_score, changed_score.potential_score)

    def test_cancel_before_cache_gui_guard_and_bad_response_never_publish(self):
        case = self.data['numeric_cases'][0]
        main = tuple(StatValue(**r) for r in case['main_rows'])
        template = self.snapshot.scoring_template('1304', 'default')
        service = ObservationService(compute=_jobs)
        kwargs = dict(
            selections=(('1304', 'default', template),),
            weight_version=self.snapshot.version,
            cost=1,
        )
        service.evaluate(main, (), **kwargs)
        event = threading.Event()
        event.set()
        with self.assertRaises(OperationCancelled), patch.object(_jobs, 'submit') as submit:
            service.evaluate(main, (), cancel_event=event, **kwargs)
        submit.assert_not_called()
        app = SimpleNamespace(thread=lambda: 'gui')
        qt = SimpleNamespace(
            QCoreApplication=SimpleNamespace(instance=lambda: app),
            QThread=SimpleNamespace(currentThread=lambda: 'gui'),
        )
        with (
            patch.dict(sys.modules, {'PySide6.QtCore': qt}),
            self.assertRaises(ComputeFailure) as failure,
        ):
            service.evaluate(main, (), **kwargs)
        self.assertEqual('gui_thread', failure.exception.kind)

        class Wrong:
            def submit(self, operation, build, **options):
                from concurrent.futures import Future

                future = Future()
                future.set_result(
                    dict(weight_version=build()['weight_version'], scores=[{}], tiers=[])
                )
                return future

        service = ObservationService(compute=Wrong())
        with self.assertRaises(ComputeFailure) as failure:
            service.evaluate(main, (), **kwargs)
        self.assertEqual('protocol', failure.exception.kind)
        self.assertFalse(service._cache)

    def test_child_panic_is_not_a_python_fallback_or_cached_partial_observation(self):
        case = self.data['numeric_cases'][0]
        jobs = ComputeJobs(ENGINE, fault_tests=True)

        class FaultOnce:
            failed = False

            def submit(self, operation, build, **options):
                if not self.failed:
                    self.failed = True
                    operation = 'fault_panic'
                return jobs.submit(operation, build, **options)

        service = ObservationService(compute=FaultOnce())
        try:
            with self.assertRaises(ComputeFailure):
                numeric_value(service, case, self.snapshot)
            self.assertFalse(service._cache)
            self.assertEqual(
                canonical_json(case['expected']),
                canonical_json(numeric_value(service, case, self.snapshot)),
            )
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    def test_retired_template_adapters_and_python_observation_math_are_deleted(self):
        from echo_workshop.application import templates
        from echo_workshop.adapters.okww import stat_overlay

        for name in (
            'calculate_echo_score',
            'auto_match_template',
            'resolve_template_name',
            '_selected_template',
        ):
            self.assertFalse(hasattr(templates, name), name)
        for name in (
            '_find_cost',
            '_cost_key',
            'find_echo_stat_rectangles',
            'calculate_echo_score',
            'substat_tier',
        ):
            self.assertFalse(hasattr(stat_overlay, name), name)

    def test_scan_pipeline_cancel_reaches_live_child_without_error_diagnostics_or_partial_result(
        self,
    ):
        from echo_workshop.capture.scan.pipeline import ScanPipeline, ScanTimings

        jobs = ComputeJobs(ENGINE, fault_tests=True)
        self.assertEqual(
            {'warm': True}, jobs.submit('ping', lambda: {'warm': True}).result(timeout=5)
        )
        child = jobs._workers['interactive']._process
        self.assertIsNone(child.poll())
        entered = threading.Event()
        cancel = threading.Event()
        diagnostics = Mock()
        timings = ScanTimings()

        class Hang:
            def submit(self, operation, build, **options):
                future = jobs.submit('fault_hang', build, **options)
                entered.set()
                return future

        service = ObservationService(compute=Hang())
        case = self.data['numeric_cases'][0]
        main = tuple(StatValue(**r) for r in case['main_rows'])

        def parse(_):
            return service.evaluate(
                main,
                (),
                selections=(
                    ('1304', 'default', self.snapshot.scoring_template('1304', 'default')),
                ),
                weight_version=self.snapshot.version,
                cost=1,
                cancel_event=cancel,
            )

        pipeline = ScanPipeline(
            parse, cancel_event=cancel, diagnostics=diagnostics, timings=timings
        )
        try:
            self.assertTrue(pipeline.submit(object()))
            self.assertTrue(entered.wait(2))
            time.sleep(0.05)
            self.assertIsNone(child.poll())
            at = time.monotonic()
            pipeline.cancel()
            pipeline.close()
            self.assertLess(time.monotonic() - at, 0.5)
            self.assertFalse(pipeline._thread.is_alive())
            self.assertIsNone(pipeline.poll())
            diagnostics.exception.assert_not_called()
            diagnostics.count.assert_any_call('detail_parse_cancelled')
            self.assertEqual(1, timings.snapshot()['counters']['parse_cancelled'])
            self.assertNotIn('parse_errors', timings.snapshot()['counters'])
            self.assertFalse(service._cache)
        finally:
            pipeline.close()
            jobs.close()
            self.assertTrue(jobs.wait_closed())
        self.assertIsNotNone(child.poll())

    def test_actual_metadata_method_uses_shared_service_and_reuses_only_numeric_evidence(self):
        from echo_workshop.capture.viewport import EchoViewport
        from echo_workshop.capture.metadata import MetadataPass, refine_echo_metadata
        from echo_workshop.application.live_selection import resolve_live_selection
        from echo_workshop.application.templates import DEFAULT_TEMPLATE

        namespace = dict(
            EchoViewport=EchoViewport,
            MetadataPass=MetadataPass,
            refine_echo_metadata=refine_echo_metadata,
            resolve_live_selection=resolve_live_selection,
            analyze_echo_stats=analyze_echo_stats,
            DEFAULT_TEMPLATE=DEFAULT_TEMPLATE,
            Box=lambda x, y, w, h, **kw: SimpleNamespace(x=x, y=y, width=w, height=h, **kw),
        )
        method = self._overlay_method('read_metadata', namespace)
        case = next(c for c in self.data['ocr_cases'] if c['name'] == 'actual_metadata_resolver')
        boxes = tuple(SimpleNamespace(**b) for b in case['boxes'])
        task = SimpleNamespace(
            ocr=lambda **kw: tuple(
                b
                for b in boxes
                if kw['box'].x <= b.x < kw['box'].x + kw['box'].width
                and kw['box'].y <= b.y < kw['box'].y + kw['box'].height
            )
        )
        frame = SimpleNamespace(shape=(900, 1600, 3))
        settings = dict(TemplateCharacterID='1304', TemplateModalID='default')
        settings['角色评分模板'] = '今汐-通用'
        service = ObservationService(compute=_jobs)
        event = threading.Event()
        with patch.object(_jobs, 'submit', wraps=_jobs.submit) as submit:
            result = method(
                task,
                frame,
                settings,
                self.snapshot,
                observation_service=service,
                cancel_event=event,
                viewport=(0, 0, 1600, 900),
                boxes=boxes,
            )
        self.assertEqual(1, submit.call_count)
        self.assertEqual(canonical_json(case['expected']), canonical_json(asdict(result.analysis)))
        event.set()
        with self.assertRaises(OperationCancelled), patch.object(_jobs, 'submit') as submit:
            method(
                task,
                frame,
                settings,
                self.snapshot,
                observation_service=service,
                cancel_event=event,
                viewport=(0, 0, 1600, 900),
                boxes=boxes,
            )
        submit.assert_not_called()
        for relative in [
            'adapters/okww/overlay_task.py',
            'adapters/okww/manual_capture.py',
            'adapters/okww/scan_observation.py',
        ]:
            source = ROOT / 'src/echo_workshop' / relative
            for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'read_metadata'
                ):
                    self.assertTrue(
                        {'observation_service', 'cancel_event'} <= {k.arg for k in node.keywords},
                        (relative, node.lineno),
                    )

    def test_actual_overlay_run_clears_failed_or_cancelled_visuals_without_host_exception(self):
        method = self._overlay_method(
            'run', dict(ComputeFailure=ComputeFailure, OperationCancelled=OperationCancelled)
        )
        capture = SimpleNamespace(single_request=None, interrupt=Mock())
        runtime = SimpleNamespace(_closed=False, capture=capture)
        settings = SimpleNamespace(runtime=runtime)
        overlay = object()
        task = SimpleNamespace(
            _settings_task=lambda: settings,
            _run_frame=Mock(),
            get_overlay_view=lambda: overlay,
            _clear=Mock(),
            log_warning=Mock(),
        )
        task._run_frame.side_effect = ComputeFailure('panic', 'synthetic child panic')
        self.assertFalse(method(task))
        capture.interrupt.assert_called_once_with()
        task._clear.assert_called_once_with(overlay, True)
        task._clear.reset_mock()
        task.log_warning.reset_mock()
        capture.interrupt.reset_mock()
        task._run_frame.side_effect = OperationCancelled('synthetic cancellation')
        self.assertFalse(method(task))
        task._clear.assert_called_once_with(overlay, True)
        task.log_warning.assert_not_called()
        capture.interrupt.assert_not_called()
        runtime.compute = object()
        task._run_frame.reset_mock()
        task._run_frame.side_effect = ComputeFailure('restart_limit', 'synthetic restart limit')
        self.assertFalse(method(task))
        self.assertIs(runtime.compute, task._compute_failed_owner)
        self.assertFalse(method(task))
        self.assertEqual(1, task._run_frame.call_count)
        self.assertEqual(1, task.log_warning.call_count)
        runtime.compute = object()  # A new plugin runtime explicitly owns the next engine.
        task._run_frame.side_effect = None
        task._run_frame.return_value = False
        self.assertFalse(method(task))
        self.assertEqual(2, task._run_frame.call_count)

    @staticmethod
    def _overlay_method(name, namespace):
        source = ROOT / 'src/echo_workshop/adapters/okww/overlay_task.py'
        tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == 'EchoWorkshopOverlayTask'
        )
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        return namespace[name]


if __name__ == '__main__':
    unittest.main()
