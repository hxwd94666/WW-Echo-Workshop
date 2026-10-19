# 校验并导入导出库存归档。
"""Versioned JSON + immutable screenshots in a portable ZIP, not game import."""

import hashlib
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import zipfile

from echo_workshop.storage.evidence import EvidenceStore, MAX_IMAGE_BYTES, validate_png
from echo_workshop.core.models import EchoRecord, canonical_json
from echo_workshop.storage.inventory import EchoRepository
from echo_workshop.core.cancellation import checkpoint, report_progress
from echo_workshop.resources.http import strict_json
from echo_workshop.storage.plans import AllocationStore, MAX_PLAN_BYTES, validate_plan
from echo_workshop.storage.loadouts import validate_current_plan_references
from echo_workshop.core.models import validate_id
from echo_workshop.storage.locks import plan_file_lock

# Keep per-image/JSON bounds; 189 full-resolution PNGs can exceed 256 MiB.
MAX_BACKUP_BYTES = 8 * 1024 * 1024 * 1024
MAX_RECORDS = 10000


def _current_plan_ids(backup):
    rows = backup.get('current_loadouts', ())
    if not isinstance(rows, (tuple, list)) or len(rows) > MAX_RECORDS:
        raise ValueError('当前配装关联集合无效')
    values = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('plan_id'), str):
            raise ValueError('当前配装方案引用无效')
        validate_id(row['plan_id'])
        values.add(row['plan_id'])
    return sorted(values)


def _current_plans(repository, backup, cancel_event):
    members = {}
    ids = _current_plan_ids(backup)
    if not ids:
        return members
    store = AllocationStore(repository.data_dir, plan_evaluator=repository.plan_evaluator)
    for plan_id in ids:
        checkpoint(cancel_event)
        with store._operation_lock(cancel_event), plan_file_lock(store.folder, plan_id):
            path = store._path(plan_id)
            if path.is_symlink() or not path.is_file():
                raise ValueError('当前配装引用的历史方案缺失或路径异常：' + plan_id)
            payload = store._load(plan_id, cancel_event=cancel_event)
            members['allocation-plans/' + plan_id + '.json'] = (path.read_bytes(), payload)
    validate_current_plan_references(
        backup.get('current_loadouts', ()),
        {value['plan_id']: value for _, value in members.values()},
        cancel_event=cancel_event,
        plan_evaluator=repository.plan_evaluator,
    )
    return members


def _archive_current_plans(archive, backup, cancel_event, *, plan_evaluator=None):
    members = {}
    names = set(archive.namelist())
    for plan_id in _current_plan_ids(backup):
        name = 'allocation-plans/' + plan_id + '.json'
        if name not in names or archive.getinfo(name).file_size > MAX_PLAN_BYTES:
            raise ValueError('当前配装引用的历史方案缺失或过大：' + plan_id)
        raw = _read_member(archive, name, cancel_event)
        envelope = strict_json(raw)
        if not isinstance(envelope, dict) or set(envelope) != {'sha256', 'payload'}:
            raise ValueError('当前配装历史方案封装格式错误')
        payload = validate_plan(
            envelope['payload'], cancel_event=cancel_event, plan_evaluator=plan_evaluator
        )
        if (
            payload['plan_id'] != plan_id
            or hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()
            != envelope['sha256']
        ):
            raise ValueError('当前配装历史方案哈希/UUID 错误')
        members[name] = (raw, payload)
    validate_current_plan_references(
        backup.get('current_loadouts', ()),
        {value['plan_id']: value for _, value in members.values()},
        cancel_event=cancel_event,
        plan_evaluator=plan_evaluator,
    )
    return members


def _restore_plan(store, payload, cancel_event, *, publish):
    """Never overwrite an existing plan, including its manual progress."""
    plan_id = payload['plan_id']
    with store._operation_lock(cancel_event), plan_file_lock(store.folder, plan_id):
        path = store._path(plan_id)
        if path.is_symlink():
            raise ValueError('现有历史方案路径异常；未覆盖')
        if path.exists():
            existing = store._load(plan_id, cancel_event=cancel_event)
            if canonical_json(existing) != canonical_json(payload) and canonical_json(
                without_images(existing)
            ) != canonical_json(without_images(payload)):
                raise ValueError('当前配装历史方案与现有记录冲突；未覆盖原方案或进度')
        elif publish:
            store._write(payload, cancel_event=cancel_event)


def _records(backup, cancel_event=None):
    if (
        not isinstance(backup, dict)
        or not isinstance(backup.get('items'), list)
        or len(backup['items']) > MAX_RECORDS
    ):
        raise ValueError('Invalid backup record collection')
    result = []
    for rows, nested in (
        (backup['items'], False),
        (backup.get('history', []), True),
        (backup.get('pending_captures', []), True),
    ):
        for row in rows:
            checkpoint(cancel_event)
            result.append(EchoRecord.from_dict(row['record'] if nested else row))
    return result


def _read_member(archive, name, cancel_event):
    parts = []
    with archive.open(name) as stream:
        while True:
            checkpoint(cancel_event)
            part = stream.read(1024 * 1024)
            if not part:
                break
            parts.append(part)
    return b''.join(parts)


def _write_member(archive, name, data, cancel_event):
    with archive.open(name, 'w') as stream:
        for offset in range(0, len(data), 1024 * 1024):
            checkpoint(cancel_event)
            stream.write(data[offset : offset + 1024 * 1024])


def _verify_archive(path, cancel_event, progress):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for index, name in enumerate(names):
            report_progress(progress, '复查备份 CRC', index, len(names), cancel_event)
            # Reading to EOF verifies CRC without buffering the full entry.
            with archive.open(name) as stream:
                while True:
                    checkpoint(cancel_event)
                    if not stream.read(1024 * 1024):
                        break


def without_images(payload, exists=None):
    """Detach evidence only in the exported copy; never mutate live records."""
    result = deepcopy(payload)
    for row in result.get('items', []):
        if exists is None or (row.get('screenshot_ref') and not exists(row['screenshot_ref'])):
            row['screenshot_ref'] = ''
    for key in ('history', 'pending_captures'):
        for row in result.get(key, []):
            record = row['record']
            if exists is None or (
                record.get('screenshot_ref') and not exists(record['screenshot_ref'])
            ):
                record['screenshot_ref'] = ''
    for row in result.get('records', []):
        if exists is None or (row.get('screenshot_ref') and not exists(row['screenshot_ref'])):
            row['screenshot_ref'] = ''
    return result


def plan_without_images(payload, exists=None):
    value = without_images(payload, exists)
    return canonical_json(
        dict(
            payload=value, sha256=hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()
        )
    ).encode('utf-8')


def export_archive(
    repository, destination, *, cancel_event=None, progress=None, include_images=False
):
    with EvidenceStore(repository.data_dir).operation():
        return _export_archive(
            repository,
            destination,
            cancel_event=cancel_event,
            progress=progress,
            include_images=include_images,
        )


def _export_archive(repository, destination, *, cancel_event, progress, include_images):
    report_progress(progress, '读取一致库存快照', 0, 0, cancel_event)
    destination = Path(destination).resolve()
    if destination.suffix != '.zip':
        raise ValueError('Backup output must be ZIP')
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = repository.export_backup(cancel_event=cancel_event)
    plans = _current_plans(repository, backup, cancel_event)
    evidence = EvidenceStore(repository.data_dir)
    exists = evidence.exists if include_images else None
    backup = without_images(backup, exists)
    records = _records(backup, cancel_event)
    for _, plan in plans.values():
        records.extend(EchoRecord.from_dict(row) for row in without_images(plan, exists)['records'])
    evidence = EvidenceStore(repository.data_dir)
    references = sorted({record.screenshot_ref for record in records if record.screenshot_ref})
    payload = canonical_json(backup).encode('utf-8')
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError('Inventory backup JSON is too large')
    total = len(payload) + sum(len(raw) for raw, _ in plans.values())
    if total > MAX_BACKUP_BYTES:
        raise ValueError('备份总量超过 8 GiB；请分档案备份，已有文件保留')
    fd, temporary = tempfile.mkstemp(prefix='.echo-backup-', suffix='.zip', dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            _write_member(archive, 'inventory.json', payload, cancel_event)
            for name, (raw, plan) in plans.items():
                _write_member(
                    archive,
                    name,
                    plan_without_images(plan, exists),
                    cancel_event,
                )
            for index, reference in enumerate(references):
                report_progress(progress, '写入截图', index, len(references), cancel_event)
                data = evidence.read(reference)
                total += len(data)
                if total > MAX_BACKUP_BYTES:
                    raise ValueError('备份总量超过 8 GiB；请分档案备份，已有文件保留')
                _write_member(archive, reference, data, cancel_event)
        # Reopen the artifact before replacing the destination.
        _verify_archive(temporary, cancel_event, progress)
        with open(temporary, 'rb+') as stream:
            os.fsync(stream.fileno())
        report_progress(progress, '发布备份文件', len(references), len(references), cancel_event)
        if repository.revision != backup['revision'] or any(
            raw != plans[name][0]
            for name, (raw, _) in _current_plans(repository, backup, cancel_event).items()
        ):
            raise ValueError('备份期间库存或当前方案进度已变化；原备份文件保留，请重试')
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def import_archive(repository, source, *, expected_revision=None, cancel_event=None, progress=None):
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 0
    ):
        raise ValueError('Invalid expected inventory revision')
    evidence = EvidenceStore(repository.data_dir)
    report_progress(progress, '检查备份目录', 0, 0, cancel_event)
    with (
        evidence.operation(),
        tempfile.TemporaryDirectory(prefix='echo-backup-validate-') as temp,
        zipfile.ZipFile(source) as archive,
    ):
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (
            len(names) != len(set(names))
            or 'inventory.json' not in names
            or len(names) > MAX_RECORDS * 11 + 1
        ):
            raise ValueError('Invalid archive members')
        if (
            sum(info.file_size for info in infos) > MAX_BACKUP_BYTES
            or archive.getinfo('inventory.json').file_size > 16 * 1024 * 1024
        ):
            raise ValueError('备份总量超过 8 GiB；请分档案备份，已有文件保留')
        backup = strict_json(_read_member(archive, 'inventory.json', cancel_event))
        records = _records(backup, cancel_event)
        plans = _archive_current_plans(
            archive, backup, cancel_event, plan_evaluator=repository.plan_evaluator
        )
        for _, plan in plans.values():
            records.extend(EchoRecord.from_dict(row) for row in plan['records'])
        references = {record.screenshot_ref for record in records if record.screenshot_ref}
        if set(names) != references | {'inventory.json'} | set(plans):
            raise ValueError('Backup screenshots are missing or unexpected')
        staged = Path(temp) / 'images'
        staged.mkdir()
        for index, reference in enumerate(sorted(references)):
            report_progress(progress, '校验并暂存截图', index, len(references), cancel_event)
            _, expected = evidence._path(reference)
            if archive.getinfo(reference).file_size > MAX_IMAGE_BYTES:
                raise ValueError('Screenshot exceeds size limit')
            data = _read_member(archive, reference, cancel_event)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('Backup screenshot hash mismatch')
            validate_png(data)
            (staged / Path(reference).name).write_bytes(data)
        # Validate identities/schema on an isolated DB before writing evidence.
        report_progress(progress, '校验库存模型', 0, len(records), cancel_event)
        with EchoRepository(
            Path(temp) / 'database', plan_evaluator=repository.plan_evaluator
        ) as validator:
            validator.import_backup(backup, cancel_event=cancel_event)
        if expected_revision is not None and repository.revision != expected_revision:
            raise ValueError('库存在恢复期间已变化；未导入，请核对后重新开始')
        store = (
            AllocationStore(repository.data_dir, plan_evaluator=repository.plan_evaluator)
            if plans
            else None
        )
        # Preflight every conflict before publishing even the first PNG/plan.
        for _, plan in plans.values():
            _restore_plan(store, plan, cancel_event, publish=False)
        for index, reference in enumerate(sorted(references)):
            report_progress(progress, '保存不可变截图', index, len(references), cancel_event)
            data = (staged / Path(reference).name).read_bytes()
            item_id = Path(reference).name[:36]
            if evidence.put(item_id, data) != reference:
                raise ValueError('Backup evidence reference mismatch')
        for _, plan in plans.values():
            _restore_plan(store, plan, cancel_event, publish=True)
        # Conflicts/cancellation can leave unused images/history alternatives,
        # but not partial current relationships or overwritten user plans.
        # Inventory and reservation changes commit together; SQL rechecks CAS.
        return repository.import_backup(
            backup,
            expected_revision=expected_revision,
            cancel_event=cancel_event,
            progress=progress,
        )
