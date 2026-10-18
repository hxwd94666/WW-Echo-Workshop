# 保存冻结配装方案和保护信息。
"""Atomic, integrity-checked plan snapshots and manual progress, outside install."""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading

from echo_workshop.core.allocation.models import (
    AllocationRequest,
    POLICY_VERSION,
    PRIORITY_POLICY_VERSION,
    RoleAllocation,
)
from echo_workshop.core.models import EchoRecord, canonical_json, utc_now, validate_id
from echo_workshop.storage.locks import plan_file_lock
from echo_workshop.application.operations import OperationCancelled, checkpoint, report_progress
from echo_workshop.resources.http import strict_json
from echo_workshop.resources.weights import ALGORITHM_VERSION, WeightSnapshot

MAX_PLAN_BYTES = 4 * 1024 * 1024


def _role_summary(native):
    """Map already computed native evidence into display-only metadata."""
    from echo_workshop.application.compute_bridge import rating_result

    summary = {
        key: native[key]
        for key in (
            'character_id',
            'modal_id',
            'label',
            'total_cents',
            'target_cents',
            'item_count',
        )
    }
    rating = rating_result(native['rating'])
    if rating is not None:
        summary.update(rating_label=rating.label, rating_tooltip=rating.tooltip)
    return summary


def validate_plan(payload, *, plan_evaluator=None, cancel_event=None, evidence_sink=None):
    checkpoint(cancel_event)
    if (
        not isinstance(payload, dict)
        or type(payload.get('schema_version')) is not int
        or payload['schema_version'] != 1
    ):
        raise ValueError('配装方案格式不兼容；原文件保留')
    validate_id(payload['plan_id'])
    request = AllocationRequest.from_dict(payload['request'])
    snapshot = WeightSnapshot.from_dict(payload['weight_snapshot'])
    result = payload['result']
    if (
        result['plan_id'] != payload['plan_id']
        or canonical_json(result['request']) != canonical_json(request.to_dict())
        or result['weight_version'] != snapshot.version
        or result['algorithm_version'] != ALGORITHM_VERSION
        or result['policy_version']
        != (PRIORITY_POLICY_VERSION if request.priority_groups else POLICY_VERSION)
        or type(result['inventory_revision']) is not int
        or result['inventory_revision'] < 0
    ):
        raise ValueError('方案版本/请求与快照不一致')
    if (
        type(result['exhaustive']) is not bool
        or type(result['nodes']) is not int
        or result['nodes'] < 0
        or type(result['elapsed']) not in (int, float)
        or not math.isfinite(result['elapsed'])
        or result['elapsed'] < 0
        or result['stop_reason'] not in ('', 'time_limit', 'node_limit', 'manual_preview')
        or result['exhaustive'] != (result['stop_reason'] == '')
    ):
        raise ValueError('搜索证明状态无效')
    records = []
    for row in payload['records']:
        checkpoint(cancel_event)
        records.append(EchoRecord.from_dict(row))
    records = tuple(records)
    by_id = {record.item_id: record for record in records}
    if len(records) != len(by_id) or any(
        record.profile_id != request.profile_id for record in records
    ):
        raise ValueError('方案记录含重复 UUID/跨档案数据')
    if len(result['roles']) != len(request.roles):
        raise ValueError('参与角色与结果数量不一致')
    for data in result['roles']:
        checkpoint(cancel_event)
        data = dict(data)
        for key in ('item_ids', 'scores', 'reasons'):
            data[key] = tuple(data[key])
        data['item_ratings'] = tuple(data.get('item_ratings') or ())
        row = RoleAllocation(**data)
        if any(
            type(value) is not int or value < 0
            for value in row.scores + (row.total_cents, row.target_cents, row.moves)
        ) or not all(isinstance(text, str) for text in row.reasons):
            raise ValueError('方案分数/原因类型无效')
    completed = payload['completed']
    if not isinstance(completed, list) or any(not isinstance(value, str) for value in completed):
        raise ValueError('换装进度结构无效')
    if type(payload['progress_revision']) is not int or payload['progress_revision'] < 0:
        raise ValueError('换装进度版本无效')
    publication = payload.get('publication')
    if publication is not None:
        if (
            not isinstance(publication, dict)
            or set(publication) != {'kind', 'source_plan_id'}
            or publication['kind'] not in ('backup', 'current', 'generated')
        ):
            raise ValueError('配装快照来源无效')
        validate_id(publication['source_plan_id'])
    removed = payload.get('removed_roles', [])
    available = {(row['character_id'], row['modal_id']) for row in result['roles']}
    if (
        not isinstance(removed, list)
        or any(
            not isinstance(key, list)
            or len(key) != 2
            or any(not isinstance(v, str) for v in key)
            or tuple(key) not in available
            for key in removed
        )
        or len({tuple(key) for key in removed}) != len(removed)
    ):
        raise ValueError('备份移除记录无效')
    if plan_evaluator is None:
        raise ValueError('方案业务重验需要已装配的 Rust 计算服务')
    evidence = plan_evaluator.validate(
        records,
        request,
        snapshot=snapshot,
        inventory_revision=result['inventory_revision'],
        allocations=result['roles'],
        completed=completed,
        cancel_event=cancel_event,
    )
    if tuple(result['objective']) != tuple(evidence['objective']):
        raise ValueError('方案证据或目标值不一致')
    checkpoint(cancel_event)
    if evidence_sink is not None:
        evidence_sink.update(evidence)
    return payload


class AllocationStore:
    def __init__(self, data_dir, *, plan_evaluator=None):
        self.plan_evaluator = plan_evaluator
        self.folder = Path(data_dir).resolve() / 'allocation-plans'
        self._lock = threading.RLock()

    def _path(self, plan_id):
        validate_id(plan_id)
        return self.folder / (plan_id + '.json')

    @contextmanager
    def _operation_lock(self, cancel_event):
        while not self._lock.acquire(timeout=0.05):
            checkpoint(cancel_event)
        try:
            checkpoint(cancel_event)
            yield
        finally:
            self._lock.release()

    def _write(self, payload, *, cancel_event=None, before_commit=None):
        validate_plan(payload, plan_evaluator=self.plan_evaluator, cancel_event=cancel_event)
        encoded = canonical_json(payload)
        envelope = canonical_json(
            dict(sha256=hashlib.sha256(encoded.encode('utf-8')).hexdigest(), payload=payload)
        )
        if len(envelope.encode('utf-8')) > MAX_PLAN_BYTES:
            raise ValueError('方案快照超过 4 MiB，原方案保留')
        self.folder.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.plan-', dir=self.folder)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            checkpoint(cancel_event)
            if before_commit is not None:
                before_commit()
            checkpoint(cancel_event)
            os.replace(temporary, self._path(payload['plan_id']))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self, result, records, snapshot, *, cancel_event=None, before_commit=None):
        with self._operation_lock(cancel_event), plan_file_lock(self.folder, result.plan_id):
            if self._path(result.plan_id).exists():
                raise ValueError('此方案已保存；重新生成会创建独立方案')
            assigned = {item_id for row in result.roles for item_id in row.item_ids}
            payload = dict(
                schema_version=1,
                plan_id=result.plan_id,
                created_at=utc_now(),
                request=result.request.to_dict(),
                result=result.to_dict(),
                weight_snapshot=snapshot.to_dict(),
                records=[record.to_dict() for record in records if record.item_id in assigned],
                completed=[],
                progress_revision=0,
            )
            # Normalize before validation: serialized lists are the on-disk contract.
            payload = json.loads(canonical_json(payload))
            if cancel_event is None and before_commit is None:
                self._write(payload)  # Preserve existing instrumented callers.
            else:
                self._write(payload, cancel_event=cancel_event, before_commit=before_commit)
            # Already normalized/validated before publication. No post-commit cancellation
            # or second read that could report an actually saved plan as a failed operation.
            return payload

    def save_frozen(self, payload, *, cancel_event=None, before_commit=None):
        """Publish a validated independent historical copy, never overwrite."""
        payload = json.loads(canonical_json(payload))
        validate_plan(payload, plan_evaluator=self.plan_evaluator, cancel_event=cancel_event)
        with self._operation_lock(cancel_event), plan_file_lock(self.folder, payload['plan_id']):
            if self._path(payload['plan_id']).exists():
                raise ValueError('此历史方案已存在；请创建独立备份')
            self._write(payload, cancel_event=cancel_event, before_commit=before_commit)
            return payload

    def load(self, plan_id, *, cancel_event=None, evidence_sink=None):
        with self._operation_lock(cancel_event), plan_file_lock(self.folder, plan_id):
            return self._load(plan_id, cancel_event=cancel_event, evidence_sink=evidence_sink)

    def remove_backup_role(
        self,
        profile_id,
        plan_id,
        character_id,
        modal_id,
        *,
        expected_hash,
        cancel_event=None,
        before_commit=None,
    ):
        """Archive one list entry, retaining every frozen score and live reference."""
        validate_id(profile_id)
        with self._operation_lock(cancel_event), plan_file_lock(self.folder, plan_id):
            payload = self._load(plan_id, cancel_event=cancel_event)
            actual = hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()
            if actual != expected_hash or payload['request']['profile_id'] != profile_id:
                raise ValueError('备份已变化或属于其他档案，请重新读取')
            key = [character_id, modal_id]
            if not any(
                [row['character_id'], row['modal_id']] == key for row in payload['result']['roles']
            ):
                raise ValueError('此备份没有选中的角色配装')
            removed = payload.get('removed_roles', [])
            if key in removed:
                raise ValueError('此角色备份已移除，请刷新列表')
            payload['removed_roles'] = [*removed, key]
            self._write(payload, cancel_event=cancel_event, before_commit=before_commit)
            return payload

    def _load(self, plan_id, *, cancel_event=None, evidence_sink=None):
        # Caller holds both the in-process lock and per-plan OS lock.
        with self._lock:
            path = self._path(plan_id)
            if path.stat().st_size > MAX_PLAN_BYTES:
                raise ValueError('方案文件超过大小限制')
            try:
                checkpoint(cancel_event)
                envelope = strict_json(path.read_text(encoding='utf-8'))
                checkpoint(cancel_event)
                payload = envelope['payload']
                if (
                    hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()
                    != envelope['sha256']
                ):
                    raise ValueError('方案内容哈希校验失败；原文件保留')
                if payload['plan_id'] != plan_id:
                    raise ValueError('方案 ID 与文件名不一致')
                return validate_plan(
                    payload,
                    plan_evaluator=self.plan_evaluator,
                    cancel_event=cancel_event,
                    evidence_sink=evidence_sink,
                )
            except (KeyError, TypeError, RecursionError) as error:
                raise ValueError('方案数据格式错误；原文件保留') from error

    def list(self, profile_id, *, cancel_event=None, progress=None, summary_sink=None):
        validate_id(profile_id)
        checkpoint(cancel_event)
        if summary_sink is not None and not isinstance(summary_sink, dict):
            raise ValueError('历史方案摘要接收器须为字典')
        result, errors, summaries = [], [], {}
        paths = sorted(self.folder.glob('*.json'))
        checkpoint(cancel_event)
        report_progress(progress, '逐项校验历史方案', 0, min(len(paths), 1000), cancel_event)
        for index, path in enumerate(paths):
            checkpoint(cancel_event)
            if index >= 1000:
                errors.append('方案数超过 1000，请保留备份并整理目录')
                break
            try:
                evidence = {}
                payload = self.load(path.stem, cancel_event=cancel_event, evidence_sink=evidence)
                if payload['request']['profile_id'] == profile_id:
                    result.append((payload['plan_id'], payload['created_at']))
                    if summary_sink is not None:
                        summaries[payload['plan_id']] = dict(
                            plan_id=payload['plan_id'],
                            created_at=payload['created_at'],
                            publication=payload.get('publication'),
                            removed_roles=payload.get('removed_roles', []),
                            roles=tuple(_role_summary(row) for row in evidence['summaries']),
                            completed_count=evidence['completed_count'],
                            item_count=evidence['item_count'],
                        )
            except OperationCancelled:
                raise
            except (ValueError, OSError, KeyError, TypeError) as error:
                checkpoint(cancel_event)
                errors.append(path.name + ': ' + str(error))
            report_progress(
                progress, '逐项校验历史方案', index + 1, min(len(paths), 1000), cancel_event
            )
        checkpoint(cancel_event)
        ordered = tuple(sorted(result, key=lambda row: row[1], reverse=True))
        # Publish summaries only after the existing final cancellation check;
        # no partial read result escapes on cancellation or a failed sort.
        if summary_sink is not None:
            summary_sink.update(summaries)
        return ordered, tuple(errors)

    def mark(
        self,
        plan_id,
        item_id,
        completed,
        *,
        inventory_revision,
        weight_version,
        progress_revision,
        cancel_event=None,
        before_commit=None,
    ):
        with self._operation_lock(cancel_event), plan_file_lock(self.folder, plan_id):
            payload = self._load(plan_id, cancel_event=cancel_event)
            result = payload['result']
            if (
                result['inventory_revision'] != inventory_revision
                or result['weight_version'] != weight_version
            ):
                raise ValueError('库存/权重已变化，请重新生成方案；旧方案和进度保留')
            if payload['progress_revision'] != progress_revision:
                raise ValueError('换装进度已变化，请重新加载')
            assigned = {value for row in result['roles'] for value in row['item_ids']}
            if item_id not in assigned or type(completed) is not bool:
                raise ValueError('请选择方案内具体实例和明确进度')
            values = set(payload['completed'])
            if completed:
                values.add(item_id)
            else:
                values.discard(item_id)
            payload['completed'] = sorted(values)
            payload['progress_revision'] += 1
            if cancel_event is None and before_commit is None:
                self._write(payload)
            else:
                self._write(payload, cancel_event=cancel_event, before_commit=before_commit)
            return payload
