# 裁剪声骸网格并准备识别区域。
"""Automatic bag's fixed field crops; original pixels and OCR coordinates kept."""

from copy import copy
from echo_workshop.capture.scan.pipeline import bag_region


def field_regions(frame, viewport):
    # Keep the small real skill heading as layout evidence, not its long text.
    # Nothing is invented to make a clipped/provisional layout pass validation.
    return tuple(
        bag_region(frame, viewport, rect)
        for rect in (
            (0.68, 0.10, 0.975, 0.28),
            (0.68, 0.414, 0.985, 0.725),
            (0.68, 0.725, 0.81, 0.785),
            (0.68, 0.795, 0.98, 0.875),
        )
    )


def pack_fields(frame, viewport):
    import numpy as np

    regions = field_regions(frame, viewport)
    gap = 16
    width = max(r[2] for r in regions)
    height = sum(r[3] for r in regions) + gap * (len(regions) - 1)
    packed = np.zeros((height, width, 3), dtype=frame.dtype)
    offsets = []
    cursor = 0
    for x, y, w, h in regions:
        packed[cursor : cursor + h, :w] = frame[y : y + h, x : x + w, :3]
        offsets.append(cursor)
        cursor += h + gap
    packed.flags.writeable = False
    return packed, regions, tuple(offsets)


def restore_boxes(boxes, regions, offsets):
    result = []
    for box in boxes:
        matches = [
            (region, offset)
            for region, offset in zip(regions, offsets)
            if 0 <= box.x
            and box.x + box.width <= region[2]
            and offset <= box.y
            and box.y + box.height <= offset + region[3]
        ]
        if len(matches) != 1:
            raise ValueError('Packed OCR box crosses a crop boundary')
        (x, y, w, h), offset = matches[0]
        mapped = copy(box)
        mapped.x = x + box.x
        mapped.y = y + box.y - offset
        result.append(mapped)
    return sorted(result, key=lambda b: (b.y, b.x))


def read_fields(frame, viewport, read, *, mode, box_factory):
    # read is the existing single OCR facade; no additional engine or worker.
    if mode == 'separate':
        return sorted(
            [
                box
                for index, region in enumerate(field_regions(frame, viewport))
                for box in read(
                    frame=frame, box=box_factory(*region, name='echo-bag-field-' + str(index))
                )
            ],
            key=lambda b: (b.y, b.x),
        )
    if mode == 'packed':
        packed, regions, offsets = pack_fields(frame, viewport)
        boxes = read(
            frame=packed,
            box=box_factory(0, 0, packed.shape[1], packed.shape[0], name='echo-bag-packed'),
        )
        return restore_boxes(boxes, regions, offsets)
    raise ValueError('Unknown bag crop strategy')
