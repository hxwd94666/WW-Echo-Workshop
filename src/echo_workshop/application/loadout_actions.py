# 分割冻结配装证据并执行历史备份或明确的当前配装保存。
from dataclasses import asdict, replace
import json
from uuid import uuid4

from echo_workshop.core.allocation.models import (
    AllocationRequest,
    AllocationResult,
    POLICY_VERSION,
    PRIORITY_POLICY_VERSION,
    RoleAllocation,
)
from echo_workshop.core.cancellation import checkpoint
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.costs import read_cost_layout
from echo_workshop.core.models import EchoRecord, canonical_json, utc_now
from echo_workshop.resources.weights import WeightSnapshot
from echo_workshop.storage.plans import validate_plan


class CurrentLoadoutRejected(ValueError):
    """History exists, but the atomic current-loadout transaction did not commit."""

    def __init__(self, payload, source, cause):
        super().__init__(str(cause))
        self.payload, self.source, self.cause = payload, source, cause


def result_from_payload(payload):
    """Restore the exact frozen request and result, without today's resources."""
    data = dict(payload['result'])
    data['request'] = AllocationRequest.from_dict(payload['request'])
    data['roles'] = tuple(
        RoleAllocation(
            **{
                **row,
                'item_ids': tuple(row['item_ids']),
                'scores': tuple(row['scores']),
                'reasons': tuple(row['reasons']),
                'item_ratings': tuple(row.get('item_ratings') or ()),
            }
        )
        for row in data['roles']
    )
    data['objective'] = tuple(data['objective'])
    return AllocationResult(**data)


def source_payload(arguments, *, plan_evaluator=None, cancel_event=None):
    """Accept one explicit source; never guess from an active UI or snapshot."""
    if 'payload' in arguments:
        if any(key in arguments for key in ('result', 'records', 'snapshot')):
            raise ValueError('请只提供一份冻结配装来源')
        payload = json.loads(canonical_json(arguments['payload']))
    else:
        result, records, snapshot = (
            arguments['result'],
            arguments['records'],
            arguments['snapshot'],
        )
        if not isinstance(result, AllocationResult) or not isinstance(snapshot, WeightSnapshot):
            raise ValueError('请提供有效配装结果和当次权重快照')
        assigned = {item for role in result.roles for item in role.item_ids}
        payload = json.loads(
            canonical_json(
                dict(
                    schema_version=1,
                    plan_id=result.plan_id,
                    created_at=utc_now(),
                    request=result.request.to_dict(),
                    result=result.to_dict(),
                    weight_snapshot=snapshot.to_dict(),
                    records=[record.to_dict() for record in records if record.item_id in assigned],
                    completed=[],
                    progress_revision=0,
                    removed_roles=[],
                )
            )
        )
    validate_plan(payload, cancel_event=cancel_event, plan_evaluator=plan_evaluator)
    if payload['request']['profile_id'] != arguments['profile_id']:
        raise ValueError('此方案属于其他档案')
    return payload


def select_characters(payload, characters=None):
    available = tuple(row['character_id'] for row in payload['result']['roles'])
    if characters is None:
        return available
    if (
        not isinstance(characters, (list, tuple))
        or not characters
        or any(not isinstance(value, str) for value in characters)
        or len(set(characters)) != len(characters)
        or not set(characters) <= set(available)
    ):
        raise ValueError('请选择此方案中的不同角色')
    return tuple(value for value in available if value in characters)


def clone_loadout_payload(payload, characters=None, *, plan_evaluator=None, cancel_event=None):
    """Create an independent history UUID, including a real subset request."""
    validate_plan(payload, cancel_event=cancel_event, plan_evaluator=plan_evaluator)
    wanted = set(select_characters(payload, characters))
    original = result_from_payload(payload)
    request = subset_request(original.request, wanted)
    rows = tuple(row for row in original.roles if row.character_id in wanted)
    assigned = {item for row in rows for item in row.item_ids}
    records = tuple(
        EchoRecord.from_dict(row) for row in payload['records'] if row['item_id'] in assigned
    )
    evidence = plan_evaluator.validate(
        records,
        request,
        allocations=[asdict(row) for row in rows],
        snapshot=WeightSnapshot.from_dict(payload['weight_snapshot']),
        inventory_revision=original.inventory_revision,
        completed=[value for value in payload['completed'] if value in assigned],
        cancel_event=cancel_event,
    )
    if 'model_objective' not in evidence:
        raise ComputeFailure('protocol', '随包计算程序缺少子集目标证据，请使用完整一致的插件包')
    result = replace(
        original,
        request=request,
        policy_version=PRIORITY_POLICY_VERSION if request.priority_groups else POLICY_VERSION,
        roles=rows,
        objective=tuple(evidence['model_objective']),
    )
    clone = clone_verified_payload(payload, result)
    checkpoint(cancel_event)
    return clone


def subset_request(request, characters):
    roles = tuple(role for role in request.roles if role.character_id in characters)
    keys = {role.key for role in roles}
    groups = tuple(
        group
        for source in request.priority_groups
        if (group := tuple(key for key in source if key in keys))
    )
    return replace(request, roles=roles, priority_groups=groups)


def clone_verified_payload(payload, result, *, records=None):
    """Freeze an already native-verified result; no business values are recomputed."""
    result = replace(result, plan_id=str(uuid4()))
    rows = result.roles
    assigned = {item for row in rows for item in row.item_ids}
    clone = dict(
        payload,
        plan_id=result.plan_id,
        created_at=utc_now(),
        request=result.request.to_dict(),
        result=result.to_dict(),
        records=[
            row
            for row in (payload['records'] if records is None else records)
            if row['item_id'] in assigned
        ],
        completed=[value for value in payload['completed'] if value in assigned],
        removed_roles=[],
    )
    return json.loads(canonical_json(clone))


def persist_loadout(store, repository, kind, arguments, *, cancel_event=None, before_commit=None):
    """History publication is independent; all ownership changes share one DB commit."""
    if kind not in ('backup-loadout', 'save-loadout'):
        raise ValueError('未知配装保存操作')
    source = source_payload(
        arguments, cancel_event=cancel_event, plan_evaluator=store.plan_evaluator
    )
    selected = select_characters(source, arguments.get('characters'))
    clone = clone_loadout_payload(
        source, selected, cancel_event=cancel_event, plan_evaluator=store.plan_evaluator
    )
    clone['publication'] = dict(
        kind='backup' if kind == 'backup-loadout' else 'current', source_plan_id=source['plan_id']
    )
    clone['removed_roles'] = []
    if kind == 'save-loadout':
        if repository is None:
            raise ValueError('保存当前配装需要库存')
        if any(len(row['item_ids']) != 5 for row in clone['result']['roles']):
            raise ValueError('仅完整五件配装可保存为当前配装；未配齐结果可备份')
        for role in AllocationRequest.from_dict(clone['request']).roles:
            read_cost_layout(tuple(slot.cost for slot in role.slots))
        # Reject stale/cancelled requests before publishing even an alternative.
        snapshot = repository.loadout_snapshot(arguments['profile_id'], cancel_event=cancel_event)
        if (
            type(arguments.get('expected_revision')) is not int
            or type(arguments.get('expected_loadout_revision')) is not int
            or snapshot.inventory_revision != arguments['expected_revision']
            or snapshot.revision != arguments['expected_loadout_revision']
        ):
            raise ValueError('库存或当前配装已变化，请刷新后保存')
    checkpoint(cancel_event)
    # A later DB conflict leaves a validated historical alternative, never
    # partial inventory/current ownership. Existing histories remain immutable.
    stored = store.save_frozen(
        clone,
        cancel_event=cancel_event,
        before_commit=before_commit if kind == 'backup-loadout' else None,
    )
    if kind == 'save-loadout':
        try:
            repository.commit_current_loadouts(
                stored,
                expected_revision=arguments['expected_revision'],
                expected_loadout_revision=arguments['expected_loadout_revision'],
                replace_plans=arguments.get('replace_plans'),
                characters=selected,
                update_owners=True,
                cancel_event=cancel_event,
                before_commit=before_commit,
            )
        except Exception as error:
            raise CurrentLoadoutRejected(stored, source, error) from error
    return stored, source
