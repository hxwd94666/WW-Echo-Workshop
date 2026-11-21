# 将宿主的文字修正和坐标转换封在只读图片边界。
from functools import partial
from types import SimpleNamespace

from ww_echo_workshop.capture.panel_reader import OcrTextBox, PanelReader
from ww_echo_workshop.ui.widgets.rating_style import dark_rating_rgb
from .private_ocr import PLUGIN_OCR, frozen_reader


def frame_reader(executor, *, threshold, enabled=True, service=None, timings=None):
    if not enabled:
        return lambda frame, region, name: []

    from ok import OCR

    ocr_config = executor.config.get('ocr', {})
    locale = getattr(executor, 'locale', 'zh_CN')
    locale_name = getattr(locale, 'name', None)
    bridge = SimpleNamespace(
        ocr_default_threshold=threshold,
        executor=SimpleNamespace(
            config={
                'ocr': {
                    'auto_simplify': isinstance(ocr_config, dict)
                    and ocr_config.get('auto_simplify', False)
                }
            },
            locale=locale_name() if callable(locale_name) else str(locale),
            ocr_po_translation=getattr(executor, 'ocr_po_translation', None),
            text_fix=dict(getattr(executor, 'text_fix', {})),
        ),
    )
    bridge.get_box = partial(OCR.get_box, bridge)
    bridge.fix_texts = partial(OCR.fix_texts, bridge)
    engine = PLUGIN_OCR if service is None else service
    read_boxes = frozen_reader(bridge, partial(engine.infer, bridge), timings=timings)

    def read(frame, region, name):
        return [
            OcrTextBox(box.x, box.y, box.width, box.height, box.name, box.confidence)
            for box in read_boxes(frame, region, name)
        ]

    return read


def make_panel_reader(
    executor, settings, *, threshold, remembered=None, timings=None, scan_icons=False
):
    read = frame_reader(
        executor,
        threshold=threshold,
        enabled=settings.get('使用独立OCR引擎', True),
        timings=timings,
    )
    return PanelReader(
        read,
        remembered=remembered,
        timings=timings,
        scan_icons=scan_icons,
        rating_rgb=dark_rating_rgb,
    )
