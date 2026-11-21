# 校验插件私有 OCR 引擎不依赖宿主引擎，并且插件内串行。
import threading
import time
import unittest
from types import SimpleNamespace
from importlib.metadata import version
from unittest.mock import Mock, patch

from ww_echo_workshop.adapters.okww.private_ocr import PluginOcrService


class FakeTask:
    def get_box(self, box, confidence, height, pos, scale_factor, text, threshold, width):
        if confidence < threshold:
            return None
        return SimpleNamespace(text=text, confidence=confidence, x=pos[0][0], y=pos[0][1])

    def fix_texts(self, boxes):
        return None


class FakeEngine:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def ocr(self, image, **kwargs):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return [[([[0, 0], [10, 0], [10, 10], [0, 10]], ('暴击', 0.99))]]


class PrivateOcrContract(unittest.TestCase):
    def test_private_engine_parses_and_serializes(self):
        engine = FakeEngine()
        service = PluginOcrService(engine_factory=lambda: engine)
        task = FakeTask()
        results = []

        def worker():
            results.append(service.infer(task, None, object(), None, 1.0, 0.5, 'default'))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(engine.calls, 3)
        self.assertEqual(engine.max_active, 1)
        self.assertEqual(results[0][0][0].text, '暴击')
        service.release_if_idle(0)
        self.assertIsNone(service._current().engine)

    def test_close_detaches_blocked_engine_and_discards_its_result(self):
        entered, resume, closed = threading.Event(), threading.Event(), threading.Event()

        class BlockedEngine(FakeEngine):
            def ocr(self, image, **kwargs):
                entered.set()
                if not resume.wait(5):
                    raise TimeoutError('synthetic OCR did not resume')
                return super().ocr(image, **kwargs)

        first, second = BlockedEngine(), FakeEngine()
        factory = iter((first, second))
        service = PluginOcrService(engine_factory=lambda: next(factory))
        results = []

        def infer():
            return service.infer(FakeTask(), None, object(), None, 1.0, 0.5, 'default')

        def close():
            service.close()
            closed.set()

        worker = threading.Thread(target=lambda: results.append(infer()))
        worker.start()
        closer = threading.Thread(target=close)
        try:
            self.assertTrue(entered.wait(2))
            closer.start()
            self.assertTrue(closed.wait(1), 'OCR close waited for blocked inference')
        finally:
            resume.set()
            worker.join(3)
            if closer.ident is not None:
                closer.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual([([], [])], results)
        self.assertEqual('暴击', infer()[0][0].text)
        self.assertEqual((1, 1), (first.calls, second.calls))

    def test_overlay_unload_releases_waiting_manual_snapshot_during_inflight_ocr(self):
        entered, release_native, unloaded = (threading.Event() for _ in range(3))

        class BlockedEngine:
            def ocr(self, image, **kwargs):
                entered.set()
                if not release_native.wait(5):
                    raise TimeoutError('test did not release OCR')
                return [[([[0, 0], [10, 0], [10, 10], [0, 10]], ('暴击', 0.99))]]

        service = PluginOcrService(engine_factory=BlockedEngine)
        from ww_echo_workshop.adapters.okww.overlay_task import WWEchoWorkshopOverlayTask

        executor = SimpleNamespace(scene=None, config={}, ocr_po_translation=None, text_fix={})
        task = WWEchoWorkshopOverlayTask(executor=executor, app=None)
        task.get_overlay_view = lambda: None
        active, waiting, late = (SimpleNamespace(cancelled=threading.Event()) for _ in range(3))
        capture = SimpleNamespace(stop=Mock(side_effect=active.cancelled.set))
        runtime = SimpleNamespace(capture=capture, _closed=False)
        settings = SimpleNamespace(runtime=runtime)
        task._settings_task = lambda: settings
        bridge = SimpleNamespace(get_box=Mock(return_value=object()), fix_texts=Mock())
        recognized = []

        def parse(_task, _settings, _runtime, _request, _snapshot, _identity, _frozen):
            recognized.append(service.infer(bridge, None, object(), None, 1.0, 0.5, 'default'))

        def unload():
            task.on_destroy()
            unloaded.set()

        thread = threading.Thread(target=unload)
        with (
            patch('ww_echo_workshop.adapters.okww.private_ocr.PLUGIN_OCR', service),
            patch('ww_echo_workshop.adapters.okww.manual_capture.parse_manual_snapshot', parse),
        ):
            try:
                self.assertTrue(task._enqueue_manual('active', active, None, settings, runtime, {}))
                self.assertTrue(entered.wait(2))
                self.assertTrue(
                    task._enqueue_manual('waiting', waiting, None, settings, runtime, {})
                )
                thread.start()
                self.assertTrue(unloaded.wait(1), 'unload waited for blocked native OCR')
                self.assertTrue(active.cancelled.is_set())
                self.assertTrue(waiting.cancelled.is_set())
                self.assertTrue(task._manual_queue.empty())
                self.assertFalse(task._enqueue_manual('late', late, None, settings, runtime, {}))
            finally:
                release_native.set()
                if task._manual_thread is not None:
                    task._manual_thread.join(3)
                if thread.ident is not None:
                    thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(task._manual_thread.is_alive())
        self.assertEqual(0, task._manual_queue.unfinished_tasks)
        self.assertEqual([([], [])], recognized)
        bridge.get_box.assert_not_called()
        capture.stop.assert_called_once()
        print(f'CAPTURE_MANUAL_UNLOAD_OK {version("ok-script")}: drained=1, late_results=0')

    def test_engine_failure_returns_empty(self):
        def fail():
            raise RuntimeError('no model')

        service = PluginOcrService(engine_factory=fail)
        self.assertEqual(
            service.infer(FakeTask(), None, object(), None, 1.0, 0.5, 'default'),
            ([], []),
        )
        self.assertEqual(
            service.infer(FakeTask(), None, object(), None, 1.0, 0.5, 'default'),
            ([], []),
        )

    def test_manual_worker_replaces_cancelled_waiting_snapshot(self):
        from ww_echo_workshop.adapters.okww.overlay_task import WWEchoWorkshopOverlayTask
        from ww_echo_workshop.capture.intake import CaptureSession

        entered, release, parsed_latest = (threading.Event() for _ in range(3))
        frames = []
        capture = CaptureSession(
            SimpleNamespace(profiles=lambda: {'demo'}), None, job_factory=Mock()
        )
        runtime = SimpleNamespace(capture=capture)
        settings = SimpleNamespace(runtime=runtime)
        executor = SimpleNamespace(scene=None, config={}, ocr_po_translation=None, text_fix={})
        task = WWEchoWorkshopOverlayTask(executor=executor, app=None)

        def request():
            self.assertTrue(capture.request_once('demo'))
            request = capture.single_request
            self.assertTrue(capture.mark_manual_frozen(request))
            return request

        def enqueue(snapshot, pending):
            return task._enqueue_manual(snapshot, pending, None, settings, runtime, {})

        def parse(_task, _settings, _runtime, pending, snapshot, _identity, _frozen):
            frames.append((snapshot, pending))
            if snapshot == 'active':
                entered.set()
                release.wait(5)
            if snapshot == 'latest':
                parsed_latest.set()

        with patch('ww_echo_workshop.adapters.okww.manual_capture.parse_manual_snapshot', parse):
            try:
                active = request()
                self.assertTrue(enqueue('active', active))
                self.assertTrue(entered.wait(2))
                capture.stop()
                self.assertTrue(active.cancelled.is_set())
                waiting = request()
                self.assertTrue(enqueue('waiting', waiting))
                self.assertFalse(enqueue('same live request', waiting))
                capture.stop()
                self.assertTrue(waiting.cancelled.is_set())
                latest = request()
                self.assertTrue(enqueue('latest', latest), 'cancelled snapshot occupied the slot')
                self.assertEqual(1, task._manual_queue.qsize())
                release.set()
                self.assertTrue(parsed_latest.wait(2))
            finally:
                release.set()
                task._close_manual_worker()
                capture.stop()

        self.assertEqual([('active', active), ('latest', latest)], frames)
        self.assertEqual(0, task._manual_queue.unfinished_tasks)


if __name__ == '__main__':
    unittest.main()
