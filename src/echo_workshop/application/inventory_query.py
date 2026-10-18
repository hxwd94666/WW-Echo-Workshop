# 将冻结库存批量提交 Rust 筛选与统计，Python 仅解析时间、准备文本索引并映射原始行。
from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
import time
from uuid import uuid4

from echo_workshop.application.compute_bridge import require_background
from echo_workshop.application.inventory_query_inputs import InventoryQueryInputs
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.application.matching.filtering import validate_filter_options
from echo_workshop.core.cancellation import OperationCancelled, checkpoint
from echo_workshop.resources.search import _normal
from echo_workshop.application.inventory_view import ViewMetadata
from echo_workshop.application.record_quality import RecordQuality
from echo_workshop.core.filters import InventoryFilters

SCOPE_FEEDBACK_CODES = frozenset(
    (
        '',
        'inventory_empty',
        'review_required',
        'selection_empty',
        'selection_filtered_empty',
        'filters_empty',
        'selection_no_match',
    )
)


@dataclass(frozen=True)
class MatchingScope:
    catalog: tuple
    records: tuple
    feedback_code: str


def _utc_microseconds(value):
    """Encode an already parsed datetime exactly; query thresholds stay native."""
    delta = value.replace(tzinfo=None) - datetime(1970, 1, 1) - value.utcoffset()
    return str(delta.days * 86400000000 + delta.seconds * 1000000 + delta.microseconds)


def _wall_microseconds(value):
    return _utc_microseconds(value.replace(tzinfo=timezone.utc))


def _offset_microseconds(value):
    delta = value.utcoffset()
    return str(delta.days * 86400000000 + delta.seconds * 1000000 + delta.microseconds)


def _captured_microseconds(record):
    try:
        value = datetime.fromisoformat(record.captured_at)
        return None if value.tzinfo is None else _utc_microseconds(value)
    except (ValueError, OverflowError):
        return None


class InventoryQueryService:
    def __init__(self, *, compute):
        self.compute = compute
        self._inputs = InventoryQueryInputs()

    def filter_records(self, records, *, cancel_event=None, **options):
        require_background()
        checkpoint(cancel_event)
        options = validate_filter_options(**options)
        rows = self._matching_rows(records, cancel_event)
        indices, _, _ = self._execute(rows, 'matching', matching=options, cancel_event=cancel_event)
        selected = tuple(rows[index][0] for index in indices)
        checkpoint(cancel_event)
        return selected

    @staticmethod
    def _matching_rows(records, cancel_event):
        rows = []
        for record in records:
            checkpoint(cancel_event)
            rows.append((record, 'item', None))
            if len(rows) > 20000:
                raise ComputeFailure('input_limit', '库存筛选超过单批规模限额')
        return tuple(rows)

    def matching_scope(self, records, *, selected_ids=None, cancel_event=None, **options):
        require_background()
        checkpoint(cancel_event)
        options = validate_filter_options(**options)
        if not isinstance(options['text'], str) or isinstance(selected_ids, (str, bytes)):
            raise ValueError('匹配选择范围需要明确的 ID 集合与检索文本')
        if selected_ids is not None:
            frozen = []
            for identity in selected_ids:
                checkpoint(cancel_event)
                if not isinstance(identity, str) or not identity or len(identity) > 128:
                    raise ValueError('匹配选择包含无效实例 ID')
                frozen.append(identity)
                if len(frozen) > 20000:
                    raise ComputeFailure('input_limit', '匹配选择超过单批限额')
            selected_ids = tuple(dict.fromkeys(frozen))
        rows = self._matching_rows(records, cancel_event)
        indices, _, details = self._execute(
            rows,
            'matching',
            matching=options,
            scope=dict(selected_ids=selected_ids, text=options['text']),
            cancel_event=cancel_event,
        )
        return MatchingScope(
            tuple(rows[index][0] for index in indices),
            tuple(rows[index][0] for index in details['scope_indices']),
            details['feedback_code'],
        )

    def review(self, rows, *, mode=0, fully_tuned=True, cancel_event=None):
        require_background()
        checkpoint(cancel_event)
        if type(mode) is not int or mode not in range(3):
            raise ValueError('未知库存确认筛选')
        frozen = []
        for row in rows:
            checkpoint(cancel_event)
            frozen.append(row)
            if len(frozen) > 20000:
                raise ComputeFailure('input_limit', '库存复核超过单批规模限额')
        indices, counts, details = self._execute(
            tuple(frozen),
            'review',
            review_mode=mode,
            fully_tuned=fully_tuned,
            cancel_event=cancel_event,
        )
        selected = tuple(frozen[index] for index in indices)
        checkpoint(cancel_event)
        return selected, counts, details['quality']

    def inspect_pins(self, records, *, usable_only=False, cancel_event=None):
        require_background()
        checkpoint(cancel_event)
        if type(usable_only) is not bool:
            raise ValueError('固定候选可用性选项无效')
        rows = []
        for record in records:
            checkpoint(cancel_event)
            rows.append((record, 'item', None))
            if len(rows) > 20000:
                raise ComputeFailure('input_limit', '固定候选超过单批规模限额')
        indices, _, details = self._execute(
            tuple(rows), 'pins', review_mode=2 if usable_only else 0, cancel_event=cancel_event
        )
        selected = tuple(rows[index][0] for index in indices)
        checkpoint(cancel_event)
        return selected, details['facts'], details['cost_positions']

    def filter_view(self, rows, filters, *, text='', cancel_event=None):
        require_background()
        checkpoint(cancel_event)
        if not isinstance(filters, InventoryFilters) or not isinstance(text, str):
            raise ValueError('库存视图需要明确的筛选和检索文本')
        frozen = []
        for record, kind, metadata in rows:
            checkpoint(cancel_event)
            if not isinstance(metadata, ViewMetadata) or not isinstance(kind, str):
                raise ValueError('库存视图需要冻结的逐行元数据')
            frozen.append((record, kind, metadata))
            if len(frozen) > 20000:
                raise ComputeFailure('input_limit', '库存视图超过单批规模限额')
        spec = dict(asdict(filters), text=text)
        indices, _, _ = self._execute(tuple(frozen), 'view', view=spec, cancel_event=cancel_event)
        return indices

    def _execute(
        self,
        rows,
        mode,
        *,
        matching=None,
        review_mode=0,
        fully_tuned=True,
        cancel_event=None,
        view=None,
        scope=None,
    ):
        query_id = str(uuid4())
        cutoff_offset = None
        prepared = None
        referenced = False

        def build(force=False):
            nonlocal prepared, referenced
            checkpoint(cancel_event)
            query = None
            text = ''
            compact = ''
            pinyin = False
            if matching is not None or view is not None:
                text = _normal((matching if matching is not None else view)['text'])
                compact = ''.join(c for c in text if c not in " '\t")
                pinyin = bool(compact and compact.isascii() and compact.isalpha())
            if matching is not None:
                query = {
                    key: value
                    for key, value in matching.items()
                    if key not in ('now', 'protected_ids', 'text')
                }
                query.update(
                    text=text,
                    compact=compact,
                    protected_ids=list(matching['protected_ids']),
                    now_us=_utc_microseconds(matching['now']),
                    now_wall_us=_wall_microseconds(matching['now']),
                    cutoff_offset_us=cutoff_offset
                    if cutoff_offset is not None
                    else _offset_microseconds(matching['now']),
                )
            if prepared is None:
                prepared, referenced = self._inputs.prepare(
                    rows,
                    search_mode=2 if pinyin else int(bool(text)),
                    with_age=bool(matching is not None and matching['max_age_days']),
                    captured=_captured_microseconds,
                    cancel_event=cancel_event,
                    view=view is not None,
                )
            projected = prepared['rows']
            if force and projected is None:
                projected = self._inputs.project(
                    rows,
                    search_mode=2 if pinyin else int(bool(text)),
                    with_age=bool(matching is not None and matching['max_age_days']),
                    captured=_captured_microseconds,
                    cancel_event=cancel_event,
                    view=view is not None,
                )
            return dict(
                query_id=query_id,
                mode=mode,
                snapshot_id=prepared['snapshot_id'],
                rows=projected if force or not referenced else None,
                matching=query,
                review_mode=review_mode,
                fully_tuned=bool(fully_tuned),
                view=dict(view, text=text, compact=compact) if view is not None else None,
                scope=scope,
            )

        job = begin_job('inventory-query')
        job.stage(mode)
        try:
            if (
                matching is not None
                and matching['max_age_days']
                and not isinstance(matching['now'].tzinfo, timezone)
            ):
                job.stage('timezone')
                clock = self.compute.submit(
                    'inventory_clock',
                    lambda: dict(
                        query_id=query_id,
                        now_wall_us=_wall_microseconds(matching['now']),
                        max_age_days=matching['max_age_days'],
                    ),
                    timeout=10,
                    cancel_event=cancel_event,
                ).result(timeout=12)
                checkpoint(cancel_event)
                if clock.get('query_id') != query_id:
                    raise ComputeFailure('request_identity', '库存时间查询身份错误')
                # Rust computes the local-wall boundary; Python only resolves
                # the caller's tzinfo metadata at that exact returned instant.
                wall = datetime(1970, 1, 1) + timedelta(microseconds=int(clock['cutoff_wall_us']))
                cutoff_offset = _offset_microseconds(wall.replace(tzinfo=matching['now'].tzinfo))
                job.stage(mode)
            deadline = time.monotonic() + 10
            try:
                value = self.compute.submit(
                    'inventory_query', build, timeout=10, cancel_event=cancel_event
                ).result(timeout=12)
            except ComputeFailure as error:
                if error.kind != 'cache_miss' or not referenced:
                    raise
                checkpoint(cancel_event)
                job.count('snapshot_reupload')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ComputeFailure('timeout', '库存快照重传已超过原截止时间') from error
                value = self.compute.submit(
                    'inventory_query',
                    lambda: build(force=True),
                    timeout=remaining,
                    cancel_event=cancel_event,
                ).result(timeout=remaining + 2)
            checkpoint(cancel_event)
            if value.get('query_id') != query_id or value.get('mode') != mode:
                raise ComputeFailure('request_identity', '库存查询返回身份错误')
            if value.get('snapshot_id') != prepared['snapshot_id']:
                raise ComputeFailure('request_identity', '库存查询返回快照身份错误')
            if type(value.get('snapshot_cached')) is not bool:
                raise ComputeFailure('protocol', '库存快照保留状态错误')
            indices, identities, counts = value['indices'], value['item_ids'], value['counts']
            if (
                not isinstance(indices, list)
                or not isinstance(identities, list)
                or len(indices) != len(identities)
            ):
                raise ComputeFailure('protocol', '库存查询返回行结构错误')
            previous = -1
            for index, identity in zip(indices, identities):
                checkpoint(cancel_event)
                if (
                    type(index) is not int
                    or not previous < index < len(rows)
                    or rows[index][0].item_id != identity
                ):
                    raise ComputeFailure('request_identity', '库存查询返回行序或 UUID 错误')
                previous = index
            if (
                not isinstance(counts, dict)
                or set(counts) != {'saved', 'pending', 'confirmed', 'eligible'}
                or any(type(n) is not int or not 0 <= n <= len(rows) for n in counts.values())
                or counts['saved'] != len(rows)
                or counts['pending'] + counts['confirmed'] != len(rows)
                or counts['eligible'] > counts['confirmed']
            ):
                raise ComputeFailure('protocol', '库存查询统计结构错误')
            details = {}
            if mode == 'review':
                quality = value['quality']
                if not isinstance(quality, list) or len(quality) != len(indices):
                    raise ComputeFailure('protocol', '库存质量返回数量错误')
                frozen_quality = []
                for index, fact in zip(indices, quality):
                    checkpoint(cancel_event)
                    frozen_quality.append(RecordQuality.from_response(rows[index][0], fact))
                details['quality'] = tuple(frozen_quality)
            if scope is not None:
                details = self._scope_details(
                    value.get('scope'), rows, indices, scope, cancel_event
                )
            if mode == 'pins':
                facts, positions = value['facts'], value['cost_positions']
                if (
                    not isinstance(facts, list)
                    or len(facts) != len(indices)
                    or any(
                        not isinstance(fact, dict)
                        or set(fact) != {'usable', 'needs_correction'}
                        or any(type(flag) is not bool for flag in fact.values())
                        for fact in facts
                    )
                    or not isinstance(positions, dict)
                    or set(positions) != {'1', '3', '4'}
                ):
                    raise ComputeFailure('protocol', '固定候选证据结构错误')
                combined = []
                for group in positions.values():
                    if (
                        not isinstance(group, list)
                        or not group
                        or group[0] != 0
                        or any(
                            type(index) is not int or not 0 <= index <= len(indices)
                            for index in group
                        )
                        or any(a >= b for a, b in zip(group, group[1:]))
                    ):
                        raise ComputeFailure('protocol', '固定候选 COST 行映射错误')
                    combined.extend(group[1:])
                if sorted(combined) != list(range(1, len(indices) + 1)):
                    raise ComputeFailure('protocol', '固定候选 COST 行映射缺失或重复')
                details = dict(
                    facts=tuple((fact['usable'], fact['needs_correction']) for fact in facts),
                    cost_positions={
                        int(cost): frozenset(group) for cost, group in positions.items()
                    },
                )
            checkpoint(cancel_event)
            if value['snapshot_cached']:
                self._inputs.remember(prepared)
        except (CancelledError, OperationCancelled) as error:
            job.finish('cancelled')
            raise OperationCancelled('库存查询已取消') from error
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise OperationCancelled('库存查询已取消') from error
            job.finish('failed', error=error)
            if error.kind == 'unknown_level':
                raise TypeError(str(error)) from error
            if error.kind == 'datetime_overflow':
                raise OverflowError(str(error)) from error
            if error.kind in (
                'invalid_filter',
                'invalid_request',
                'invalid_record',
                'numeric_limit',
                'invalid_numeric',
            ):
                raise ValueError(str(error)) from error
            raise
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            job.finish('failed', error=error)
            raise ComputeFailure('protocol', '库存查询返回结构错误') from error
        except Exception as error:
            job.finish('failed', error=error)
            raise
        job.finish('completed', counters={'records': len(rows), 'selected': len(indices)})
        return tuple(indices), counts, details

    @staticmethod
    def _scope_details(value, rows, catalog, scope, cancel_event):
        if not isinstance(value, dict) or set(value) != {'indices', 'item_ids', 'feedback'}:
            raise ComputeFailure('protocol', '匹配选择结果结构错误')
        indices, identities, code = value['indices'], value['item_ids'], value['feedback']
        if (
            not isinstance(indices, list)
            or not isinstance(identities, list)
            or len(indices) != len(identities)
            or not isinstance(code, str)
            or code not in SCOPE_FEEDBACK_CODES
        ):
            raise ComputeFailure('protocol', '匹配选择结果尺寸或原因码错误')
        previous = -1
        catalog_set = set(catalog)
        for index, identity in zip(indices, identities):
            checkpoint(cancel_event)
            if (
                type(index) is not int
                or not previous < index < len(rows)
                or index not in catalog_set
                or rows[index][0].item_id != identity
            ):
                raise ComputeFailure('request_identity', '匹配选择返回行序或 UUID 错误')
            previous = index
        # Verify explicit opaque-ID coverage only; no quality/filter rule is evaluated here.
        requested = None if scope['selected_ids'] is None else set(scope['selected_ids'])
        returned = set(indices)
        for index in catalog:
            checkpoint(cancel_event)
            if (requested is None or rows[index][0].item_id in requested) != (index in returned):
                raise ComputeFailure('request_identity', '匹配选择范围被扩大或丢失')
        return dict(scope_indices=tuple(indices), feedback_code=code)
