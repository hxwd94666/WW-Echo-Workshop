# 从同一识别帧裁出选中声骸详情，保留原始像素和裁剪坐标证据。
from dataclasses import replace
import json
import math

from echo_workshop.capture.viewport import EchoViewport


def evidence_bounds(analysis, width, height):
    """One continuous detail panel, never the OCR accelerator's packed canvas."""
    layout = getattr(analysis, 'layout', '')
    rows = tuple(getattr(analysis, 'main_rows', ()))
    if layout not in ('bag', 'display') or len(rows) != 2:
        raise ValueError('仅背包或完整展示页可保存声骸详情截图')
    area = EchoViewport.checked(getattr(analysis, 'viewport', ()) or None, width, height)
    if area.narrow:
        raise ValueError('声骸详情区域过窄')
    # Same selected-panel geometry as scan_ocr_regions and metadata_regions.
    # Both display and dual-Echo mode collect the selected right-hand item.
    left, top = (0.665, 0.08) if layout == 'bag' else (0.755, 0.105)
    if getattr(analysis, 'comparison', False):
        left = 0.775
    dx, dy = area.offset('right')
    x1, y1 = round(dx + area.reference_width * left), round(dy + area.height * top)
    x2, y2 = round(dx + area.reference_width * 0.995), round(dy + area.height * 0.93)
    if not (area.x <= x1 < x2 <= area.x + area.width and area.y <= y1 < y2 <= area.y + area.height):
        raise ValueError('声骸详情裁剪超出游戏区域')
    # A known page is not sufficient: every actual parsed stat must fit inside
    # this selected panel. Never silently clip values or keep the whole screen.
    for row in rows + tuple(getattr(analysis, 'sub_rows', ())):
        values = (row.x, row.y, row.width, row.height)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('词条裁剪坐标无效')
        if not (
            row.width > 0
            and row.height > 0
            and x1 <= row.x
            and y1 <= row.y
            and row.x + row.width <= x2
            and row.y + row.height <= y2
        ):
            raise ValueError('声骸词条不完整或超出选中详情区域')
    return (x1, y1, x2 - x1, y2 - y1)


def describe_evidence(record, analysis, width, height):
    bounds = evidence_bounds(analysis, width, height)
    provenance = dict(
        source_size=(width, height),
        bounds=bounds,
        layout=analysis.layout,
        comparison=bool(getattr(analysis, 'comparison', False)),
    )
    return replace(
        record,
        raw_text=record.raw_text
        + '\n[echo-evidence-crop-v1] '
        + json.dumps(provenance, ensure_ascii=False, allow_nan=False),
    )


def encode_evidence(frame, analysis):
    """Encode only unscaled, unpainted pixels from this very recognition frame."""
    import cv2

    if frame is None or frame.ndim != 3 or frame.shape[2] != 3 or str(frame.dtype) != 'uint8':
        raise ValueError('声骸截图必须为原始8位三通道画面')
    height, width = frame.shape[:2]
    x, y, w, h = evidence_bounds(analysis, width, height)
    success, encoded = cv2.imencode('.png', frame[y : y + h, x : x + w])
    if not success:
        raise ValueError('声骸详情截图编码失败')
    return encoded.tobytes()


def prepare_evidence(record, frame, analysis):
    image = encode_evidence(frame, analysis)
    height, width = frame.shape[:2]
    return describe_evidence(record, analysis, width, height), image
