# 校验插件私有 OCR 引擎不依赖宿主引擎，并且插件内串行。
import threading
import time
import unittest
from types import SimpleNamespace

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
        self.assertIsNone(service._engine)

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


if __name__ == '__main__':
    unittest.main()
