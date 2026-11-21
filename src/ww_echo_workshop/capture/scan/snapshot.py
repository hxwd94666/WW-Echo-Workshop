# 从冻结背包图像解析与选择回执对应的声骸记录和证据。
from contextlib import nullcontext
from dataclasses import replace

from ww_echo_workshop.capture.evidence import prepare_evidence
from ww_echo_workshop.capture.metadata import settle_observation_issues
from ww_echo_workshop.capture.snapshot_parse import parse_snapshot_record
from ww_echo_workshop.core.cancellation import OperationCancelled, checkpoint
from .crop import read_fields


def snapshot_parser(
    parser,
    settings,
    profile,
    weights,
    sonata,
    observations,
    stop_event,
    *,
    crop_mode='packed',
    timings=None,
):
    if crop_mode not in ('original', 'packed'):
        raise ValueError('Unknown scan snapshot crop mode')

    def measure(name):
        return timings.stage(name) if timings is not None else nullcontext()

    def parse(snapshot):
        checkpoint(stop_event)
        frame, viewport = snapshot.frame, snapshot.viewport
        boxes = []
        metadata = None
        packed_observation = None
        if crop_mode == 'packed':
            try:
                boxes = read_fields(frame, viewport, parser.read_region, mode='packed')
                checkpoint(stop_event)
                with measure('metadata'):
                    metadata = parser.read_metadata(
                        frame,
                        settings,
                        weights,
                        viewport=viewport,
                        boxes=boxes,
                        scan=True,
                        observation_service=observations,
                        cancel_event=stop_event,
                    )
                packed_observation = metadata
            except OperationCancelled:
                raise
            except ValueError:
                # Geometry/crop ambiguity gets one legacy attempt on THIS
                # frozen image. Engine/runtime exceptions still propagate.
                if timings is not None:
                    timings.increment('packed_crop_errors')
            if timings is not None:
                timings.increment('packed_crop_attempts')
        if crop_mode == 'packed' and timings is not None and metadata is not None:
            if getattr(metadata, 'issues', ()) or getattr(metadata.analysis, 'layout', '') != 'bag':
                timings.increment('packed_legacy_fallbacks')
        with measure('metadata'):
            metadata, record = parse_snapshot_record(
                parser,
                frame,
                viewport,
                settings,
                weights,
                profile,
                sonata,
                scene='bag',
                boxes=boxes or None,
                initial=metadata,
                expected_level=snapshot.proof.level,
                observations=observations,
                cancel_event=stop_event,
            )
        boxes = getattr(metadata, 'original_boxes', boxes)
        if getattr(metadata.analysis, 'layout', '') != 'bag' or getattr(
            metadata.analysis, 'layout_provisional', False
        ):
            analysis = metadata.analysis
            row, column = snapshot.proof.position
            raise ValueError(
                'Queued snapshot is not a complete bag panel; '
                f'position=({row + 1},{column + 1}); level={snapshot.proof.level}; '
                f'layout={getattr(analysis, "layout", "")!r}; '
                f'provisional={getattr(analysis, "layout_provisional", False)}; '
                f'rows=({len(getattr(analysis, "main_rows", ()))},{len(getattr(analysis, "sub_rows", ()))}); '
                f'boxes={len(boxes)}'
            )
        if record is None or record.level != snapshot.proof.level:
            raise ValueError('Queued snapshot level/details disagree')
        if packed_observation is not metadata and getattr(packed_observation, 'issues', ()):
            # A later same-frame read must not erase an earlier conflicting
            # observation. Preserve its raw boxes and unresolved issues, but
            # drop warnings the final record has already resolved.
            packed_issues = tuple(settle_observation_issues(record, packed_observation.issues))
            record = replace(
                record,
                issues=tuple(dict.fromkeys(record.issues + packed_issues)),
                raw_text=record.raw_text
                + '\n[echo-scan-packed-observation]\n'
                + packed_observation.raw_text(),
            )
        checkpoint(stop_event)
        with measure('png_encode'):
            record, evidence = prepare_evidence(record, frame, metadata.analysis)
        checkpoint(stop_event)
        return record, evidence

    return parse
