# 插件私有 CPU OCR 引擎；不导入、不调用宿主 executor.ocr_lib。
from contextlib import nullcontext
from dataclasses import dataclass
import logging
import threading
import time


@dataclass
class _EngineGeneration:
    engine: object | None = None
    failed: bool = False
    last_used: float = 0.0


class PluginOcrService:
    def __init__(self, engine_factory=None):
        self._inference_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._generation = _EngineGeneration()
        self._factory = engine_factory
        self.calls = 0
        self.load_seconds = 0.0
        self.logger = logging.getLogger('ww_echo_workshop.ocr')

    def available(self):
        state = self._current()
        with self._inference_lock:
            return (
                self._current() is state
                and self._ensure_engine(state) is not None
                and self._current() is state
            )

    def _current(self):
        with self._state_lock:
            return self._generation

    def _ensure_engine(self, state):
        if state.failed:
            return None
        if state.engine is not None:
            return state.engine
        started = time.monotonic()
        try:
            if self._factory is not None:
                engine = self._factory()
            else:
                from onnxocr.onnx_paddleocr import ONNXPaddleOcr

                engine = ONNXPaddleOcr(
                    use_angle_cls=False,
                    use_npu=False,
                    use_openvino=True,
                    logger=self.logger,
                )
        except Exception as error:
            state.failed = True
            if self._current() is state:
                self.logger.error('私有 OCR 初始化失败：%s', error)
            return None
        if self._current() is not state:
            return None
        state.engine = engine
        self.load_seconds = time.monotonic() - started
        self.logger.info('私有 OCR 初始化完成：%.2fs', self.load_seconds)
        return engine

    def infer(self, task, box, image, match, scale_factor, threshold, lib):
        state = self._current()
        with self._inference_lock:
            if self._current() is not state:
                return [], []
            engine = self._ensure_engine(state)
            if engine is None or self._current() is not state:
                return [], []
            self.calls += 1
            state.last_used = time.monotonic()
            try:
                call = getattr(engine, 'ocr', None)
                if callable(call):
                    result = call(image, det=True, rec=True, cls=False)
                else:
                    result = engine(image, use_det=True, use_cls=False, use_rec=True)
            except Exception as error:
                if self._current() is state:
                    self.logger.error('私有 OCR 推理失败：%s', error)
                return [], []
            if self._current() is not state:
                return [], []
            parsed = self._parse(task, box, result, match, scale_factor, threshold)
            return parsed if self._current() is state else ([], [])

    def release_if_idle(self, idle_seconds):
        if not self._inference_lock.acquire(blocking=False):
            return
        try:
            state = self._current()
            if state.engine is not None and time.monotonic() - state.last_used >= idle_seconds:
                self.close()
        finally:
            self._inference_lock.release()

    def close(self):
        # A running call retains its own generation until native OCR returns.
        # Do not make the host unload path wait for the inference lock.
        with self._state_lock:
            previous = self._generation
            self._generation = _EngineGeneration()
        del previous

    @staticmethod
    def _parse(task, box, result, match, scale_factor, threshold):
        detected_boxes = []
        try:
            items = result[0] if result and result[0] is not None else ()
            for item in items:
                pos = item[0]
                text = item[1][0]
                confidence = item[1][1]
                width = round(pos[2][0] - pos[0][0])
                height = round(pos[2][1] - pos[0][1])
                if width <= 0 or height <= 0:
                    continue
                detected = task.get_box(
                    box, confidence, height, pos, scale_factor, text, threshold, width
                )
                if detected is not None:
                    detected_boxes.append(detected)
            task.fix_texts(detected_boxes)
        except Exception:
            return [], []
        ocr_boxes = detected_boxes
        if match is not None:
            from ok.feature.Box import find_boxes_by_name

            detected_boxes = find_boxes_by_name(detected_boxes, match)
        return detected_boxes, ocr_boxes


PLUGIN_OCR = PluginOcrService()


def frozen_reader(task, infer, *, timings=None):
    """Read only supplied pixels; never use task.ocr's pause or frame hooks."""
    from ok import Box

    def read(frame, region, name):
        x, y, w, h = region
        image = frame[y : y + h, x : x + w, :3]
        if not image.size:
            return []
        stage = (
            'level_ocr'
            if name.startswith('echo-level')
            else 'sort_ocr'
            if name == 'echo-sort'
            else 'detail_ocr'
        )
        with timings.stage(stage) if timings is not None else nullcontext():
            detected, _ = infer(
                Box(x, y, w, h, name=name), image, None, 1.0, task.ocr_default_threshold, 'default'
            )
        return sorted(detected, key=lambda box: (box.y, box.x))

    return read
