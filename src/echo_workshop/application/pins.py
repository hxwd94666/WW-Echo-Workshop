# 准备可固定声骸的候选目录。
"""Immutable, full-pool selector labels prepared off the Qt thread."""

from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Mapping
from concurrent.futures import CancelledError
from uuid import uuid4

from echo_workshop.application.operations import checkpoint, report_progress
from echo_workshop.application.compute_bridge import require_background, record_payload
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.requirements import RoleRequirements


def pin_error_text(slot, code):
    return (
        f'第 {slot} 槽固定声骸缺失或待核对；原 UUID 已保留。'
        if code == 'missing'
        else f'第 {slot} 槽固定声骸与 COST 或主词条要求冲突。'
    )


@dataclass(frozen=True)
class PinCheck:
    future: object
    identity: str
    keys: tuple


class PinCheckService:
    def __init__(self, *, compute):
        self.compute = compute

    def start(self, requirements, records, *, cancel_event=None):
        """Only freeze explicit inputs here; record projection runs in the shared worker."""
        if not isinstance(requirements, Mapping) or len(requirements) > 128:
            raise ValueError('固定要求需要明确的角色要求映射，最多 128 项')
        keys, specs = [], []
        for key, value in requirements.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or any(not isinstance(part, str) or not part for part in key)
                or not isinstance(value, RoleRequirements)
            ):
                raise ValueError('固定要求的角色身份或结构无效')
            keys.append(key)
            specs.append(
                dict(
                    key=key,
                    costs=value.costs,
                    main_names=value.main_names,
                    fixed_item_ids=value.fixed_item_ids or ('',) * 5,
                    allowed_sonatas=value.allowed_sonatas,
                    set_requirements=value.set_requirements,
                )
            )
        records = tuple(records)
        if len(records) > 20000:
            raise ComputeFailure('input_limit', '固定要求候选超过单批规模限额')
        identity = uuid4().hex

        def build():
            requested = {id for spec in specs for id in spec['fixed_item_ids'] if id}
            projected = []
            for record in records:
                checkpoint(cancel_event)
                if record.item_id in requested:
                    projected.append(record_payload(record))
            checkpoint(cancel_event)
            return dict(identity=identity, records=projected, requirements=specs)

        future = self.compute.submit('pin_check', build, timeout=10, cancel_event=cancel_event)
        return PinCheck(future, identity, tuple(keys))

    @staticmethod
    def response(request):
        if not request.future.done():
            raise ComputeFailure('pending', '固定要求尚未校验完成')
        value = request.future.result()  # Already completed; never wait on the GUI thread.
        if not isinstance(value, dict) or value.get('identity') != request.identity:
            raise ComputeFailure('request_identity', '固定要求返回身份错误')
        verdicts = value.get('verdicts')
        if not isinstance(verdicts, list) or len(verdicts) != len(request.keys):
            raise ComputeFailure('protocol', '固定要求返回数量错误')
        errors = []
        for key, verdict in zip(request.keys, verdicts):
            if not isinstance(verdict, dict) or verdict.get('key') != list(key):
                raise ComputeFailure('request_identity', '固定要求返回角色错误')
            slot, code = verdict.get('slot'), verdict.get('code')
            if not (
                (code == '' and slot is None)
                or (code in ('missing', 'conflict') and type(slot) is int and 1 <= slot <= 5)
            ):
                raise ComputeFailure('protocol', '固定要求返回结论错误')
            if code:
                errors.append((key, slot, code))
        return tuple(errors)

    def check(self, requirements, records, *, cancel_event=None):
        require_background()
        job = begin_job('fixed-items')
        job.stage('checking')
        try:
            request = self.start(requirements, records, cancel_event=cancel_event)
            request.future.result(timeout=12)
            checkpoint(cancel_event)
            errors = self.response(request)
        except (CancelledError, OperationCancelled):
            job.finish('cancelled')
            raise OperationCancelled('固定要求校验已取消') from None
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise OperationCancelled('固定要求校验已取消') from error
            job.finish('failed', error=error)
            raise
        except Exception as error:
            job.finish('failed', error=error)
            raise
        job.finish(counters=dict(roles=len(requirements), errors=len(errors)))
        return errors


@dataclass(frozen=True)
class PinCatalog:
    records: tuple
    labels: tuple
    positions: object
    display_labels: tuple
    usable: tuple
    cost_positions: object


EMPTY_PIN_CATALOG = PinCatalog(
    (),
    ('不固定',),
    MappingProxyType({'': 0}),
    ('不固定',),
    (),
    MappingProxyType({cost: frozenset((0,)) for cost in (1, 3, 4)}),
)


def prepare_pin_catalog(records, *, queries, usable_only=False, cancel_event=None, progress=None):
    require_background()
    checkpoint(cancel_event)
    if queries is None:
        raise ValueError('固定候选需要显式的 Rust 库存查询服务')
    records = tuple(records)
    report_progress(progress, '准备完整固定实例候选', 0, len(records), cancel_event)
    records, facts, cost_positions = queries.inspect_pins(
        records, usable_only=usable_only, cancel_event=cancel_event
    )
    labels = ['不固定']
    display_labels = ['不固定']
    positions = {'': 0}
    total = len(records)
    for index, record in enumerate(records, 1):
        checkpoint(cancel_event)
        if record.item_id in positions:
            raise ValueError('固定实例候选 UUID 重复')
        state = ['已确认' if record.review == 'confirmed' else '待确认']
        if record.locked:
            state.append('保护')
        if facts[index - 1][1]:
            state.append('需纠错')
        stats = ' / '.join(
            f'{stat.stat_name} {stat.value:g}' for stat in record.main_stats + record.sub_stats
        )
        labels.append(
            f'{record.name} · {record.sonata} · C{record.cost} · {record.item_id} · {"/".join(state)} · {stats}'
        )
        name = record.name if len(record.name) <= 18 else record.name[:17] + '…'
        display_labels.append(f'{name or "名称待核对"} · C{record.cost}')
        positions[record.item_id] = index
        if index % 100 == 0 or index == total:
            report_progress(progress, '准备完整固定实例候选', index, total, cancel_event)
    checkpoint(cancel_event)
    return PinCatalog(
        records,
        tuple(labels),
        MappingProxyType(positions),
        tuple(display_labels),
        tuple(fact[0] for fact in facts),
        MappingProxyType(cost_positions),
    )
