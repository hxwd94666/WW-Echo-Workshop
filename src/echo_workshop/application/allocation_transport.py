# 构造明确配装输入并映射 Rust 结果，不在 Python 求值约束和目标。
from uuid import uuid4

from echo_workshop.application.compute_bridge import plain, record_payload
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.allocation.models import (
    AllocationRequest,
    AllocationResult,
    RoleAllocation,
    POLICY_VERSION,
    PRIORITY_POLICY_VERSION,
)
from echo_workshop.core.scoring import ALGORITHM_VERSION
from echo_workshop.resources.weights import WeightSnapshot


def allocation_payload(
    records,
    request,
    *,
    inventory_revision,
    snapshot,
    seconds=10,
    max_nodes=200000,
    reservations=None,
    edit_plans=None,
    replace_plans=None,
    cancel_event=None,
):
    if not isinstance(request, AllocationRequest) or not isinstance(snapshot, WeightSnapshot):
        raise ValueError('配装需要明确请求和冻结资料快照')
    if type(inventory_revision) is not int or inventory_revision < 0:
        raise ValueError('库存修订无效')
    from echo_workshop.core.allocation.models import AllocationCancelled

    def checkpoint():
        if cancel_event is not None and cancel_event.is_set():
            raise AllocationCancelled('配装已取消')

    checkpoint()
    wire_records = []
    for record in records:
        checkpoint()
        wire_records.append(record_payload(record))
        if len(wire_records) > 20000:
            raise ValueError('配装库存超过 20000 件')
    checkpoint()
    data = plain(request.to_dict())
    templates = {
        f'{role.character_id}/{role.modal_id}': plain(snapshot.scoring_template(*role.key))
        for role in request.roles
    }
    protection = None
    if reservations is not None:
        try:
            protection = {
                'profile_id': reservations.profile_id,
                'inventory_revision': reservations.inventory_revision,
                'claims': plain(tuple(reservations.owners.items())),
                'current': [
                    {field: row[field] for field in ('character_id', 'modal_id', 'plan_id')}
                    | {'issues': plain(row.get('issues', ()))}
                    for row in reservations.current
                ],
            }
        except (AttributeError, KeyError, TypeError) as error:
            raise ValueError('配装保护快照字段不完整') from error
    checkpoint()
    return dict(
        request=data,
        records=wire_records,
        templates=templates,
        inventory_revision=inventory_revision,
        reservations=protection,
        edit_plans=plain(dict(edit_plans or {})),
        replace_plans=plain(dict(replace_plans or {})),
        seconds=seconds,
        max_nodes=max_nodes,
    )


def allocation_result(data, request, inventory_revision, snapshot):
    rows = data.get('roles')
    if not isinstance(rows, list) or len(rows) != len(request.roles):
        raise ComputeFailure('protocol', '配装返回角色数量错误')
    roles = []
    for row, role in zip(rows, request.roles):
        if (row.get('character_id'), row.get('modal_id')) != role.key:
            raise ComputeFailure('request_identity', '配装返回角色身份或顺序错误')
        roles.append(
            RoleAllocation(
                row['character_id'],
                row['modal_id'],
                row['label'],
                tuple(row['item_ids']),
                tuple(row['scores']),
                row['total_cents'],
                row['target_cents'],
                row['moves'],
                tuple(row['reasons']),
                row.get('rating'),
                tuple(row.get('item_ratings') or ()),
            )
        )
    policy = PRIORITY_POLICY_VERSION if request.priority_groups else POLICY_VERSION
    if data.get('policy_version') != policy:
        raise ComputeFailure('protocol', '配装返回非当前目标政策')
    if not isinstance(data.get('objective'), list) or len(data['objective']) != 6:
        raise ComputeFailure('protocol', '配装目标证据结构错误')
    if type(data.get('exhaustive')) is not bool or data.get('stop_reason') not in (
        '',
        'node_limit',
        'time_limit',
    ):
        raise ComputeFailure('protocol', '配装终止状态错误')
    if data['exhaustive'] != (data['stop_reason'] == ''):
        raise ComputeFailure('protocol', '配装完备性与终止状态矛盾')
    return AllocationResult(
        str(uuid4()),
        request,
        inventory_revision,
        snapshot.version,
        ALGORITHM_VERSION,
        policy,
        tuple(roles),
        data['exhaustive'],
        data['stop_reason'],
        data['nodes'],
        data['elapsed'],
        tuple(data['objective']),
    )


def precheck_result(data, request):
    if data.get('profile_id') != request.profile_id or len(data.get('roles', ())) != len(
        request.roles
    ):
        raise ComputeFailure('request_identity', '配装预检身份或数量错误')
    rows = []
    for row, role in zip(data['roles'], request.roles):
        if tuple(row.get('key', ())) != role.key:
            raise ComputeFailure('request_identity', '配装预检角色顺序错误')
        rows.append(
            {
                **row,
                'key': tuple(row['key']),
                'costs': tuple(row['costs']),
                'sets': tuple(tuple(value) for value in row['sets']),
                'slot_ids': tuple(tuple(values) for values in row['slot_ids']),
            }
        )
    return {**data, 'roles': tuple(rows), 'missing_pins': tuple(data['missing_pins'])}
