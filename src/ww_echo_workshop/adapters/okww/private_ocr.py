# 插件私有 CPU OCR 引擎；不导入、不调用宿主 executor.ocr_lib。
import logging
import threading
import time


class PluginOcrService:
    def __init__(self, engine_factory=None):
        self._lock = threading.RLock()
        self._engine = None
        self._factory = engine_factory
        self._failed = False
        self._last_error = ''
        self._last_used = 0.0
        self.calls = 0
        self.load_seconds = 0.0
        self.logger = logging.getLogger('ww_echo_workshop.ocr')

    def available(self):
        with self._lock:
            return self._ensure_engine() is not None

    def _ensure_engine(self):
        if self._failed:
            return None
        if self._engine is not None:
            return self._engine
        started = time.monotonic()
        try:
            if self._factory is not None:
                self._engine = self._factory()
            else:
                from onnxocr.onnx_paddleocr import ONNXPaddleOcr

                self._engine = ONNXPaddleOcr(
                    use_angle_cls=False,
                    use_npu=False,
                    use_openvino=True,
                    logger=self.logger,
                )
        except Exception as error:
            self._failed = True
            self._last_error = str(error)
            self.logger.error('私有 OCR 初始化失败：%s', error)
            return None
        self.load_seconds = time.monotonic() - started
        self.logger.info('私有 OCR 初始化完成：%.2fs', self.load_seconds)
        return self._engine

    def infer(self, task, box, image, match, scale_factor, threshold, lib):
        with self._lock:
            engine = self._ensure_engine()
            if engine is None:
                return [], []
            self.calls += 1
            self._last_used = time.monotonic()
            try:
                call = getattr(engine, 'ocr', None)
                if callable(call):
                    result = call(image, det=True, rec=True, cls=False)
                else:
                    result = engine(image, use_det=True, use_cls=False, use_rec=True)
            except Exception as error:
                self._last_error = str(error)
                self.logger.error('私有 OCR 推理失败：%s', error)
                return [], []
            return self._parse(task, box, result, match, scale_factor, threshold)

    def release_if_idle(self, idle_seconds):
        with self._lock:
            if self._engine is not None and time.monotonic() - self._last_used >= idle_seconds:
                self.close()

    def close(self):
        with self._lock:
            self._engine = None
            self._last_error = ''
            self._failed = False
            self._last_used = 0.0

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
