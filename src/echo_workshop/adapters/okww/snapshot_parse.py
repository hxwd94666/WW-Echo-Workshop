# 复用冻结帧的详情解析与回退，不读取新的实时画面。
import re

from ok.feature.Box import Box
from echo_workshop.capture.scan.pipeline import bag_region
from echo_workshop.capture.viewport import EchoViewport, detect_echo_viewport


def _scene_from_text(text):
    compact = re.sub(r'\s+', '', text)
    if any(marker in compact for marker in ('声骸管理方案', '等级顺序', '批量管理')) or re.search(
        r'声骸\d+/\d+', compact
    ):
        return 'bag'
    if any(marker in compact for marker in ('声骸推荐', '简述')):
        return 'display'
    if any(marker in compact for marker in ('声骸强化', '强化并调谐', '已完成全部调谐')):
        return 'enhancement'
    return ''


def detect_scene(parser, frame, viewport):
    height, width = frame.shape[:2]
    viewport = viewport or detect_echo_viewport(frame)
    if viewport is None:
        return ''
    try:
        area = EchoViewport.checked(viewport, width, height)
    except ValueError:
        return ''
    x = area.x + round(area.width * 0.10)
    y = area.y + round(area.height * 0.01)
    region = (x, y, round(area.width * 0.80), max(1, round(area.height * 0.12)))
    try:
        boxes = parser.ocr(frame=frame, box=Box(*region, name='echo-scene'))
    except Exception:
        return ''
    return _scene_from_text(' '.join(str(box.name) for box in (boxes or ())))


def _detail_region(scene):
    return (0.72, 0.06, 0.995, 0.95) if scene == 'display' else (0.665, 0.08, 0.995, 0.93)


def _read_metadata(
    parser,
    frame,
    viewport,
    settings,
    weights,
    *,
    boxes,
    scene,
    observations,
    cancel_event,
    cost_lookup,
):
    return parser.read_metadata(
        frame,
        settings,
        weights,
        viewport=viewport,
        boxes=boxes,
        scan=True,
        layout_hint=scene,
        observation_service=observations,
        cancel_event=cancel_event,
        cost_lookup=cost_lookup,
    )


def _needs_fallback(metadata, expected_level=None):
    if metadata is None:
        return True
    analysis = getattr(metadata, 'analysis', None)
    if analysis is None:
        return True
    if getattr(analysis, 'layout', '') not in ('bag', 'display'):
        return True
    if getattr(analysis, 'layout_provisional', False):
        return True
    if getattr(metadata, 'issues', ()):
        return True
    if expected_level is not None:
        if len(getattr(analysis, 'main_rows', ())) != 2:
            return True
        if len(getattr(analysis, 'sub_rows', ())) != expected_level // 5:
            return True
    return False


def parse_snapshot_record(
    parser,
    frame,
    viewport,
    settings,
    weights,
    profile,
    sonata,
    *,
    scene='',
    boxes=None,
    initial=None,
    expected_level=None,
    observations=None,
    cancel_event=None,
    cost_lookup=None,
):
    """Parse one frozen panel, using the same retries for manual and scan paths."""
    metadata = initial
    if metadata is None:
        metadata = _read_metadata(
            parser,
            frame,
            viewport,
            settings,
            weights,
            boxes=boxes,
            scene=scene,
            observations=observations,
            cancel_event=cancel_event,
            cost_lookup=cost_lookup,
        )
    x1, y1, x2, y2 = _detail_region(scene)
    if _needs_fallback(metadata, expected_level):
        rect = bag_region(frame, viewport, (x1, y1, x2, y2))
        boxes = parser.ocr(frame=frame, box=Box(*rect, name='echo-detail-retry'))
        metadata = _read_metadata(
            parser,
            frame,
            viewport,
            settings,
            weights,
            boxes=boxes,
            scene=scene,
            observations=observations,
            cancel_event=cancel_event,
            cost_lookup=cost_lookup,
        )
    if _needs_fallback(metadata, expected_level):
        bands = []
        for index, (top, bottom) in enumerate(((y1, 0.39), (0.39, 0.735), (0.735, y2))):
            rect = bag_region(frame, viewport, (x1, top, x2, bottom))
            bands.extend(
                parser.ocr(frame=frame, box=Box(*rect, name='echo-detail-retry-' + str(index)))
            )
        metadata = _read_metadata(
            parser,
            frame,
            viewport,
            settings,
            weights,
            boxes=bands,
            scene=scene,
            observations=observations,
            cancel_event=cancel_event,
            cost_lookup=cost_lookup,
        )
    record = parser.read_record(metadata, frame, viewport, profile, weights, sonata)
    return metadata, record
