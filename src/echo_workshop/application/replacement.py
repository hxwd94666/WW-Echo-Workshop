# 将冻结单槽替换提交共享 Rust 服务，仅映射证据与独立预览。
from concurrent.futures import CancelledError
from dataclasses import dataclass, replace
import json

from echo_workshop.application.allocation_transport import allocation_payload
from echo_workshop.application.compute_bridge import (
    plain,
    rating_result,
    record_payload,
    require_background,
)
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.application.loadout_actions import (
    clone_verified_payload,
    result_from_payload,
    subset_request,
)
from echo_workshop.core.allocation.models import (
    RoleAllocation,
    POLICY_VERSION,
    PRIORITY_POLICY_VERSION,
    AllocationCancelled,
)
from echo_workshop.core.cancellation import OperationCancelled, checkpoint
from echo_workshop.core.models import EchoRecord, canonical_json
from echo_workshop.resources.weights import WeightSnapshot
from echo_workshop.storage.plans import validate_plan


@dataclass(frozen=True)
class ReplacementCandidate:
    item_id: str
    name: str
    current_cents: int | None
    old_cents: int
    delta_cents: int | None
    total_cents: int | None
    item_rating: object
    old_rating: object
    total_rating: object
    available: bool
    reason: str


@dataclass(frozen=True)
class ReplacementOptions:
    profile_id: str
    source_plan_id: str
    character_id: str
    modal_id: str
    slot_index: int
    inventory_revision: int
    loadout_revision: int
    weight_version: str
    label: str
    old_record: EchoRecord
    old_cents: int
    old_rating: object
    source_total_cents: int
    source_total_rating: object
    records: tuple
    candidates: tuple[ReplacementCandidate, ...]
    source_json: str


@dataclass(frozen=True)
class ReplacementPreview:
    result: object
    records: tuple
    snapshot: WeightSnapshot
    source_plan_id: str
    character_id: str
    slot_index: int
    item_id: str
    inventory_revision: int
    loadout_revision: int


def _request(
    payload,
    reservations,
    character_id,
    slot_index,
    operation,
    plan_evaluator,
    cancel_event,
    item_id=None,
):
    require_background()
    checkpoint(cancel_event)
    validate_plan(payload, plan_evaluator=plan_evaluator, cancel_event=cancel_event)
    source = result_from_payload(payload)
    snapshot = WeightSnapshot.from_dict(payload['weight_snapshot'])
    if reservations.profile_id != source.request.profile_id:
        raise ValueError('替换库存属于其他档案')
    if type(slot_index) is not int or not 0 <= slot_index < 5:
        raise ValueError('请选择五个槽位中的一个')
    job = begin_job(
        operation,
        {'inventory_revision': reservations.inventory_revision, 'weight_version': snapshot.version},
    )

    def build():
        frozen = tuple(EchoRecord.from_dict(row) for row in payload['records'])
        data = allocation_payload(
            frozen,
            source.request,
            inventory_revision=source.inventory_revision,
            snapshot=snapshot,
            cancel_event=cancel_event,
        )
        data.update(
            allocations=plain(payload['result']['roles']), completed=plain(payload['completed'])
        )
        projected = None
        if item_id is not None:
            projected = {item_id}
            for row in source.roles:
                if row.character_id == character_id:
                    projected.update(row.item_ids)
        records = []
        for record in reservations.records:
            checkpoint(cancel_event)
            if projected is not None and record.item_id not in projected:
                continue
            records.append(record_payload(record))
            if len(records) > 20000:
                raise ValueError('替换库存超过 20000 件')
        return dict(
            source=data,
            records=records,
            profile_id=reservations.profile_id,
            source_plan_id=source.plan_id,
            weight_version=snapshot.version,
            inventory_revision=reservations.inventory_revision,
            loadout_revision=reservations.revision,
            claims=plain(tuple(reservations.owners.items())),
            character_id=character_id,
            slot_index=slot_index,
            item_id=item_id,
        )

    try:
        job.stage('preparing')
        data = plan_evaluator.compute.submit(
            operation, build, lane='interactive', timeout=10, cancel_event=cancel_event
        ).result(timeout=12)
        checkpoint(cancel_event)
        expected = dict(
            profile_id=reservations.profile_id,
            source_plan_id=source.plan_id,
            weight_version=snapshot.version,
            inventory_revision=reservations.inventory_revision,
            loadout_revision=reservations.revision,
            character_id=character_id,
            slot_index=slot_index,
        )
        if any(data.get(key) != value for key, value in expected.items()):
            raise ComputeFailure('request_identity', '替换返回了其他快照或角色的证据')
        role = next((r for r in source.request.roles if r.character_id == character_id), None)
        if role is None or data.get('modal_id') != role.modal_id:
            raise ComputeFailure('request_identity', '替换返回的玩法身份不一致')
        job.finish(counters={'candidates': len(data['candidates'])})
        return data, source, snapshot
    except (CancelledError, OperationCancelled, AllocationCancelled) as error:
        job.finish('cancelled')
        raise OperationCancelled('替换计算已取消') from error
    except ComputeFailure as error:
        if error.kind in ('cancelled', 'closed'):
            job.finish('cancelled')
            raise OperationCancelled('替换计算已取消') from error
        job.finish('failed', error=error)
        if error.kind in (
            'invalid_request',
            'invalid_record',
            'invalid_rule',
            'invalid_allocation',
            'invalid_evidence',
            'invalid_numeric',
            'numeric_limit',
            'input_limit',
            'retired_policy',
        ):
            raise ValueError(str(error)) from error
        raise
    except Exception as error:
        job.finish('failed', error=error)
        raise


def replacement_candidates(
    payload, reservations, character_id, slot_index, *, plan_evaluator=None, cancel_event=None
):
    data, source, snapshot = _request(
        payload,
        reservations,
        character_id,
        slot_index,
        'replacement_candidates',
        plan_evaluator,
        cancel_event,
    )
    evidence = {row['item_id']: EchoRecord.from_dict(row) for row in payload['records']}
    records = {row.item_id: row for row in reservations.records}
    old_rating = rating_result(data['old_rating'])
    candidates = []
    for row in data['candidates']:
        checkpoint(cancel_event)
        candidates.append(
            ReplacementCandidate(
                **{
                    **row,
                    'item_rating': rating_result(row['item_rating']),
                    'total_rating': rating_result(row['total_rating']),
                    'old_rating': old_rating,
                }
            )
        )
    checkpoint(cancel_event)
    return ReplacementOptions(
        data['profile_id'],
        data['source_plan_id'],
        data['character_id'],
        data['modal_id'],
        data['slot_index'],
        data['inventory_revision'],
        data['loadout_revision'],
        data['weight_version'],
        data['label'],
        evidence[data['old_id']],
        data['old_cents'],
        old_rating,
        data['source_total_cents'],
        rating_result(data['source_total_rating']),
        tuple(records[item_id] for item_id in data['record_ids']),
        tuple(candidates),
        canonical_json(payload),
    )


def replacement_preview(options, item_id, reservations, *, plan_evaluator=None, cancel_event=None):
    if not isinstance(options, ReplacementOptions):
        raise ValueError('请重新打开单槽替换候选')
    if (reservations.profile_id, reservations.inventory_revision, reservations.revision) != (
        options.profile_id,
        options.inventory_revision,
        options.loadout_revision,
    ):
        raise ValueError('库存或当前配装已变化，请重新打开替换')
    candidate = next((row for row in options.candidates if row.item_id == item_id), None)
    if candidate is None or not candidate.available:
        raise ValueError(candidate.reason if candidate else '请选择有效候选声骸')
    payload = json.loads(options.source_json)
    if (
        payload['plan_id'] != options.source_plan_id
        or payload['result']['weight_version'] != options.weight_version
    ):
        raise ValueError('替换来源已变化，请重新打开候选')
    data, source, snapshot = _request(
        payload,
        reservations,
        options.character_id,
        options.slot_index,
        'replace_preview',
        plan_evaluator,
        cancel_event,
        item_id=item_id,
    )
    request = subset_request(source.request, (options.character_id,))
    expected_policy = PRIORITY_POLICY_VERSION if request.priority_groups else POLICY_VERSION
    if data['policy_version'] != expected_policy:
        raise ComputeFailure('request_identity', '替换返回了其他目标政策')
    row = dict(data['row'])
    for field in ('item_ids', 'scores', 'reasons'):
        row[field] = tuple(row[field])
    result = replace(
        source,
        request=request,
        roles=(RoleAllocation(**row),),
        objective=tuple(data['objective']),
        inventory_revision=reservations.inventory_revision,
        policy_version=data['policy_version'],
        exhaustive=False,
        stop_reason='manual_preview',
        nodes=0,
        elapsed=0,
    )
    assigned = set(result.roles[0].item_ids)
    clone = clone_verified_payload(
        payload,
        result,
        records=[r.to_dict() for r in reservations.records if r.item_id in assigned],
    )
    checkpoint(cancel_event)
    return ReplacementPreview(
        result_from_payload(clone),
        tuple(EchoRecord.from_dict(r) for r in clone['records']),
        snapshot,
        source.plan_id,
        options.character_id,
        options.slot_index,
        item_id,
        reservations.inventory_revision,
        reservations.revision,
    )
