# 映射 Rust 配装差异结果，不在 Python 重算分差、评级或展示配对。
from concurrent.futures import CancelledError
from dataclasses import dataclass

from echo_workshop.application.compute_bridge import (
    plain,
    rating_result,
    record_payload,
    require_background,
)
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled, checkpoint


@dataclass(frozen=True)
class ChangeItem:
    item_id: str
    record: object
    score_cents: int | None
    rating: object = None


@dataclass(frozen=True)
class ChangePair:
    kind: str
    old: ChangeItem | None
    new: ChangeItem | None
    delta_cents: int | None = None


@dataclass(frozen=True)
class RoleLoadoutChange:
    key: tuple[str, str]
    label: str
    state: str
    notice: str
    old_plan_id: str
    old_items: tuple[ChangeItem, ...]
    new_items: tuple[ChangeItem, ...]
    pairs: tuple[ChangePair, ...]
    old_total_cents: int | None
    new_total_cents: int | None
    delta_cents: int | None
    old_rating: object
    new_rating: object
    snapshot: object
    inventory_revision: int
    retained_count: int
    evaluated_records: int = 0


def plan_changes(result, inventory, weights, service, cancel_event=None):
    require_background()
    checkpoint(cancel_event)
    if (
        result.weight_version != weights.version
        or result.inventory_revision != inventory.inventory_revision
    ):
        raise ValueError('变动输入与本次配装快照不一致')
    job = begin_job(
        'loadout-diff',
        {'inventory_revision': inventory.inventory_revision, 'weight_version': weights.version},
    )

    def build():
        checkpoint(cancel_event)
        characters = {row.character_id for row in result.roles}
        current = [row for row in inventory.current if row['character_id'] in characters]
        wanted = {value for row in result.roles for value in row.item_ids}
        wanted.update(value for row in current for value in row['item_ids'])
        selected_records = []
        for record in inventory.records:
            checkpoint(cancel_event)
            if record.item_id in wanted:
                selected_records.append(record_payload(record))
        return {
            'profile_id': inventory.profile_id,
            'inventory_revision': inventory.inventory_revision,
            'weight_version': weights.version,
            'result': plain(result.to_dict()),
            'records': selected_records,
            'current': [
                {
                    field: plain(row[field])
                    for field in ('character_id', 'modal_id', 'plan_id', 'item_ids')
                }
                | {'issues': plain(row.get('issues', ()))}
                for row in current
            ],
            'templates': {
                f'{row.character_id}/{row.modal_id}': plain(
                    weights.scoring_template(row.character_id, row.modal_id)
                )
                for row in result.roles
            },
        }

    try:
        response = service.compute.submit(
            'loadout_diff', build, timeout=10, cancel_event=cancel_event
        ).result(timeout=12)
        checkpoint(cancel_event)
        values = response.get('roles')
        if not isinstance(values, list) or len(values) != len(result.roles):
            raise ComputeFailure('protocol', '变动返回角色数量错误')
        by_id = {record.item_id: record for record in inventory.records}
        output = []
        for data, allocation in zip(values, result.roles):
            checkpoint(cancel_event)
            key = allocation.character_id, allocation.modal_id
            if tuple(data.get('key', ())) != key:
                raise ComputeFailure('request_identity', '变动返回角色身份错误')

            def items(rows):
                return tuple(
                    ChangeItem(
                        row['item_id'],
                        by_id.get(row['item_id']),
                        row['score_cents'],
                        rating_result(row['rating']),
                    )
                    for row in rows
                )

            old_items, new_items = items(data['old_items']), items(data['new_items'])
            before = {item.item_id: item for item in old_items}
            after = {item.item_id: item for item in new_items}
            pairs = tuple(
                ChangePair(
                    row['kind'],
                    before.get(row['old_id']),
                    after.get(row['new_id']),
                    row['delta_cents'],
                )
                for row in data['pairs']
            )
            output.append(
                RoleLoadoutChange(
                    key,
                    data['label'],
                    data['state'],
                    data['notice'],
                    data['old_plan_id'],
                    old_items,
                    new_items,
                    pairs,
                    data['old_total_cents'],
                    data['new_total_cents'],
                    data['delta_cents'],
                    rating_result(data['old_rating']),
                    rating_result(data['new_rating']),
                    weights,
                    inventory.inventory_revision,
                    data['retained_count'],
                    data['evaluated_records'],
                )
            )
        checkpoint(cancel_event)
        job.finish(counters={'roles': len(output)})
        return tuple(output)
    except (OperationCancelled, CancelledError) as error:
        job.finish('cancelled')
        raise OperationCancelled('配装差异计算已取消') from error
    except ComputeFailure as error:
        if error.kind in ('cancelled', 'closed'):
            job.finish('cancelled')
            raise OperationCancelled('配装差异计算已取消') from error
        job.finish('failed', error=error)
        if error.kind in ('invalid_request', 'invalid_record'):
            raise ValueError(str(error)) from error
        raise
    except Exception as error:
        job.finish('failed', error=error)
        raise
