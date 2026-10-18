# 定义匹配和配装的候选范围。
"""One matching scope; cardinality changes presentation, never allocation."""

from echo_workshop.application.operations import checkpoint


def select_match_records(records, selected_ids=None, cancel_event=None):
    checkpoint(cancel_event)
    if selected_ids is None:
        result = tuple(records)
        checkpoint(cancel_event)
        return result
    chosen = frozenset(selected_ids)
    result = []
    for record in records:
        checkpoint(cancel_event)
        if record.item_id in chosen:
            result.append(record)
    checkpoint(cancel_event)
    return tuple(result)


def match_view(role_count, item_count):
    if not role_count or not item_count:
        return 'empty'
    if role_count == item_count == 1:
        return 'single'
    if role_count == 1:
        return 'role'
    if item_count == 1:
        return 'item'
    return 'matrix'
