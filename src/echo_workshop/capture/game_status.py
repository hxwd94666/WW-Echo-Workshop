# 保留历史状态并记录装备观察。
"""Lossless game-state snapshots and explicit, per-field observation merging."""

from dataclasses import replace
import json

# Legacy discard/lock fields remain readable in models and backups only.
FIELDS = ('game_equipped',)
MARKER = '[echo-game-status-v1] '


def status_evidence(record):
    for line in reversed(record.raw_text.splitlines()):
        if line.startswith(MARKER):
            try:
                data = json.loads(line[len(MARKER) :])
                return data if isinstance(data, dict) else {}
            except (ValueError, TypeError):
                return {}
    return {}


def merge_game_status(previous, observed):
    """Only called after explicit instance association. Unknown is not false."""
    old_evidence = status_evidence(previous).get('fields', {})
    new_evidence = status_evidence(observed).get('fields', {})
    old_evidence = old_evidence if isinstance(old_evidence, dict) else {}
    new_evidence = new_evidence if isinstance(new_evidence, dict) else {}
    changes = {name: getattr(previous, name) for name in ('game_discarded', 'game_locked')}
    fields = {}
    for name in FIELDS:
        retained = getattr(observed, name) is None and getattr(previous, name) is not None
        source, evidence = (previous, old_evidence) if retained else (observed, new_evidence)
        value = getattr(source, name)
        changes[name] = value
        details = evidence.get(name, {})
        details = details if isinstance(details, dict) else {}
        timestamp = details.get('observed_at', source.game_status_updated_at)
        timestamp = timestamp if isinstance(timestamp, str) else source.game_status_updated_at
        fields[name] = dict(value=value, observed_at=timestamp, retained=retained)
    observed_any = any(getattr(observed, name) is not None for name in FIELDS)
    changes['game_status_updated_at'] = (
        observed.game_status_updated_at if observed_any else previous.game_status_updated_at
    )
    evidence = dict(fields=fields, latest_observation=status_evidence(observed))
    changes['raw_text'] = (
        observed.raw_text
        + '\n'
        + MARKER
        + json.dumps(evidence, ensure_ascii=False, allow_nan=False)
    )
    return replace(observed, **changes)


def attach_game_status(record, analysis, boxes, width, height, frame):
    """Equipment observation is independent of resolving a new character ID."""
    from echo_workshop.core.models import utc_now
    from echo_workshop.capture.metadata import _observed_empty_owner

    if getattr(analysis, 'layout', '') not in ('bag', 'display'):
        return record
    equipment = None
    reliable = not getattr(analysis, 'layout_provisional', False)
    conflict = 'ocr:conflicting_owner' in record.issues or any(
        i.startswith('ocr:metadata_disagreement:owner') for i in record.issues
    )
    if reliable and not conflict:
        if record.owner:
            equipment = True
        elif record.owner_state == 'unequipped' or _observed_empty_owner(
            analysis, boxes, width, height, frame
        ):
            equipment = False
    values = dict(game_equipped=equipment)
    time = utc_now() if any(value is not None for value in values.values()) else ''
    evidence = dict(
        layout=analysis.layout,
        fields={
            name: dict(value=value, observed_at=time if value is not None else '')
            for name, value in values.items()
        },
        owner_raw=record.owner,
    )
    return replace(
        record,
        **values,
        game_status_updated_at=time,
        raw_text=record.raw_text
        + '\n'
        + MARKER
        + json.dumps(evidence, ensure_ascii=False, allow_nan=False),
    )
