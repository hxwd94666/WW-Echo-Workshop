# 按完整业务输入复用有界库存投影，不缓存筛选结果或替代原生求值。
import sys
import threading
from uuid import uuid4

from echo_workshop.application.compute_bridge import record_payload
from echo_workshop.core.cancellation import checkpoint
from echo_workshop.core.text import simplify_echo_text
from echo_workshop.resources.search import _normal, search_keys
from echo_workshop.application.text import simplify_echo_text as simplify_search_text

CACHE_BYTES = 8 * 1024 * 1024
CACHE_ENTRIES = 2


def _weight(value, cancel_event=None):
    pending, seen, total = [value], set(), 0
    while pending:
        checkpoint(cancel_event)
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if total > CACHE_BYTES:
            return total
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


def _content(rows, search_mode, with_age, cancel_event, view=False):
    values = []
    for record, kind, metadata in rows:
        checkpoint(cancel_event)
        values.append(
            (
                record.item_id,
                record.profile_id,
                record.cost,
                tuple((s.stat_name, s.value) for s in record.main_stats),
                tuple((s.stat_name, s.value) for s in record.sub_stats),
                record.name,
                record.sonata,
                record.level,
                record.review,
                record.issues,
                record.locked,
                record.owner,
                record.owner_state,
                record.game_equipped,
                record.note,
                record.captured_at,
                kind,
                (metadata.search_text, metadata.reservation) if view else (),
            )
        )
    return search_mode, with_age, view, tuple(values)


class InventoryQueryInputs:
    def __init__(self):
        self._entries = []
        self._bytes = 0
        self._lock = threading.Lock()

    def prepare(self, rows, *, search_mode, with_age, captured, cancel_event, view=False):
        key = _content(rows, search_mode, with_age, cancel_event, view)
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry['key'] == key:
                    self._entries.append(self._entries.pop(index))
                    return entry, True
        values = self.project(
            rows,
            search_mode=search_mode,
            with_age=with_age,
            captured=captured,
            cancel_event=cancel_event,
            view=view,
        )
        entry = dict(key=key, snapshot_id=str(uuid4()), rows=values)
        entry['bytes'] = _weight((key, entry['snapshot_id']), cancel_event)
        return entry, False

    @staticmethod
    def project(rows, *, search_mode, with_age, captured, cancel_event, view=False):
        values = []
        for record, kind, metadata in rows:
            checkpoint(cancel_event)
            search = dict(literal='', plain='', keys='')
            if search_mode:
                content = (
                    metadata.search_text
                    if view
                    else '\n'.join(
                        (record.name, record.sonata, record.owner, record.note, record.item_id)
                    )
                )
                search = dict(
                    literal=_normal(content),
                    plain=_normal((simplify_search_text if view else simplify_echo_text)(content)),
                    keys=search_keys(content) if search_mode == 2 else '',
                )
            values.append(
                dict(
                    record=record_payload(record),
                    kind=kind,
                    captured_us=captured(record) if with_age else None,
                    search=search,
                    reservation=metadata.reservation if view else '',
                )
            )
        return values

    def remember(self, entry):
        if entry['bytes'] > CACHE_BYTES:
            return
        with self._lock:
            if any(value['snapshot_id'] == entry['snapshot_id'] for value in self._entries):
                return
            while self._entries and (
                len(self._entries) >= CACHE_ENTRIES or self._bytes + entry['bytes'] > CACHE_BYTES
            ):
                self._bytes -= self._entries.pop(0)['bytes']
            # The native process owns the prepared rows. Retain only the exact
            # content identity here; an evicted child snapshot is rebuilt once.
            self._entries.append(dict(entry, rows=None))
            self._bytes += entry['bytes']
