# 无损迁移档案设置和历史方案。
"""Scoped, UUID-preserving portable profile transfer; no runtime activation.

SQLite commits inventory/relationships together. Plan files are independent:
ordinary failure/cancellation removes newly published files before returning;
an interrupted process can leave unreferenced valid plans, never overwrite old
plans/progress. Application of returned scoped settings belongs to the caller.
"""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
import zipfile

from echo_workshop.storage.archives import (
    MAX_BACKUP_BYTES,
    MAX_RECORDS,
    _read_member,
    _write_member,
    plan_without_images,
    without_images,
)
from echo_workshop.storage.loadouts import validate_current_plan_references
from echo_workshop.core.models import canonical_json, validate_id
from echo_workshop.core.requirements import requirements_from_rows
from echo_workshop.application.operations import checkpoint, report_progress
from echo_workshop.storage.locks import plan_file_lock
from echo_workshop.storage.preferences import validate_application
from echo_workshop.storage.inventory import EchoRepository
from echo_workshop.resources.http import strict_json
from echo_workshop.storage.workspace_bundle import (
    BOOL_KEYS,
    LEGACY_TEXT_KEYS,
    MAX_JSON,
    MAX_PLANS,
    _plan,
    _plan_bytes,
    _plan_paths,
)

KIND = 'okww-echo-workshop-profile'
SETTING_KEYS = ('ComparisonSettingsJSON', 'AllocationSettingsJSON')


@dataclass(frozen=True)
class ProfileArchivePreview:
    source_sha256: str
    format: str
    profiles: tuple
    items: int
    history: int
    pending: int
    loadouts: int
    plans: int
    settings_json: tuple
    source_revision: int
    warnings: tuple = ()


@dataclass(frozen=True)
class ProfileImportReport:
    added: int
    profiles: tuple
    new_profiles: tuple
    preserved_names: int
    plans_added: int
    plans_preserved: int
    settings_json: tuple
    revision: int
    warnings: tuple = ()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _settings(values, profiles):
    """Validate just the two allowed fields with the existing app validator."""
    if not isinstance(values, dict) or set(values) != set(SETTING_KEYS):
        raise ValueError('档案备份设置只接受匹配与配装两项。')
    defaults = {key: False for key in BOOL_KEYS}
    defaults.update({key: '' for key in LEGACY_TEXT_KEYS})
    defaults.update(InventoryProfileID=next(iter(profiles), ''), **values)
    validate_application(defaults, profiles)
    return tuple((key, values[key]) for key in SETTING_KEYS)


def _scoped_settings(preferences, profiles):
    if not isinstance(preferences, dict):
        raise ValueError('档案匹配或配装设置应为对象。')
    values = {}
    for key in SETTING_KEYS:
        raw = preferences.get(key, '')
        if not raw:
            values[key] = ''
            continue
        data = strict_json(raw)
        if (
            not isinstance(data, dict)
            or set(data) != {'schema_version', 'profiles'}
            or type(data['schema_version']) is not int
            or data['schema_version'] != 1
            or not isinstance(data['profiles'], dict)
        ):
            raise ValueError('档案匹配或配装设置格式错误。')
        scoped = {
            identity: row for identity, row in data['profiles'].items() if identity in profiles
        }
        values[key] = canonical_json(dict(schema_version=1, profiles=scoped))
    return _settings(values, profiles)


def merge_profile_settings(preferences, settings_json, profiles):
    """Return only scoped JSON; existing local UUID rows always win.

    No Config setter, global shortcut, weight, active profile or workspace is
    touched. The caller may persist the two returned fields explicitly.
    """
    incoming = dict(_settings(dict(settings_json), profiles))
    output = {}
    for key in SETTING_KEYS:
        raw = preferences.get(key, '')
        if raw:
            current = strict_json(raw)
            if (
                not isinstance(current, dict)
                or set(current) != {'schema_version', 'profiles'}
                or current['schema_version'] != 1
                or not isinstance(current['profiles'], dict)
            ):
                raise ValueError('本机档案设置格式错误；原设置保留。')
        else:
            current = dict(schema_version=1, profiles={})
        current = deepcopy(current)
        if incoming[key]:
            for identity, row in strict_json(incoming[key])['profiles'].items():
                current['profiles'].setdefault(identity, row)
        output[key] = canonical_json(current) if raw or incoming[key] else ''
    # This also checks local rows; never silently discard malformed settings.
    return dict(_settings(output, profiles))


def _profile_backup(backup, profile):
    validate_id(profile)
    if profile not in backup['profiles']:
        raise ValueError('请选择仍存在的档案。')
    result = deepcopy(backup)
    result['profiles'] = {profile: backup['profiles'][profile]}
    result['items'] = [row for row in result['items'] if row['profile_id'] == profile]
    identities = {row['item_id'] for row in result['items']}
    result['history'] = [row for row in result.get('history', ()) if row['item_id'] in identities]
    result['pending_captures'] = [
        row for row in result.get('pending_captures', ()) if row['record']['profile_id'] == profile
    ]
    result['current_loadouts'] = [
        row for row in result.get('current_loadouts', ()) if row['profile_id'] == profile
    ]
    return without_images(result)


def _selected_plans(store, profile, cancel_event):
    result = {}
    for path in _plan_paths(store, cancel_event):
        raw = _plan_bytes(store, path, cancel_event)
        payload = _plan(
            raw, 'allocation-plans/' + path.name, cancel_event, plan_evaluator=store.plan_evaluator
        )
        if payload['request']['profile_id'] == profile:
            result[path.name] = (raw, payload)
    return result


def _headers(archive, cancel_event):
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)) or not 1 <= len(names) <= MAX_RECORDS * 11 + MAX_PLANS + 4:
        raise ValueError('档案备份条目重复或数量异常。')
    if sum(info.file_size for info in infos) > MAX_BACKUP_BYTES:
        raise ValueError('档案备份总量超过大小限制。')
    for info in infos:
        checkpoint(cancel_event)
        if (
            info.flag_bits & 1
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or (info.external_attr >> 16) & 0o170000 == 0o120000
            or info.is_dir()
            or '\\' in info.filename
            or info.filename.startswith('/')
            or any(part in ('', '.', '..') for part in info.filename.split('/'))
        ):
            raise ValueError('档案备份路径或压缩类型异常。')
    return names


@contextmanager
def _pinned_archive(source, cancel_event):
    source = Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BACKUP_BYTES:
        raise ValueError('请选择有效的 ZIP 档案备份。')
    with tempfile.TemporaryDirectory(prefix='echo-profile-check-') as folder:
        root = Path(folder)
        pinned = root / 'source.zip'
        digest = hashlib.sha256()
        count = 0
        with source.open('rb') as stream, pinned.open('wb') as output:
            while True:
                checkpoint(cancel_event)
                part = stream.read(1024 * 1024)
                if not part:
                    break
                count += len(part)
                if count > MAX_BACKUP_BYTES:
                    raise ValueError('档案备份总量超过大小限制。')
                digest.update(part)
                output.write(part)
        checkpoint(cancel_event)
        yield pinned, digest.hexdigest(), root


def _read_contents(pinned, root, cancel_event, progress, *, plan_evaluator=None):
    """Read/validate each format in owned temporary storage only."""
    warnings = ()
    with zipfile.ZipFile(pinned) as archive:
        names = _headers(archive, cancel_event)
        if 'profile.json' in names:
            kind = 'profile'
            if archive.getinfo('profile.json').file_size > MAX_JSON:
                raise ValueError('档案备份清单过大。')
            meta = strict_json(_read_member(archive, 'profile.json', cancel_event))
            fields = {
                'kind',
                'schema_version',
                'source_profile',
                'source_inventory_revision',
                'settings',
                'members',
            }
            if (
                not isinstance(meta, dict)
                or set(meta) != fields
                or meta['kind'] != KIND
                or type(meta['schema_version']) is not int
                or meta['schema_version'] != 1
                or type(meta['source_inventory_revision']) is not int
                or not 0 <= meta['source_inventory_revision'] < 2**63
                or not isinstance(meta['members'], dict)
            ):
                raise ValueError('档案备份清单格式不兼容。')
            validate_id(meta['source_profile'])
            members = meta['members']
            if (
                set(names) != set(members) | {'profile.json'}
                or 'inventory.json' not in members
                or len(members) > MAX_PLANS + 1
            ):
                raise ValueError('档案备份清单与文件集合不一致。')
            plans = {}
            backup = None
            for index, (name, entry) in enumerate(members.items()):
                report_progress(progress, '校验档案文件', index, len(members), cancel_event)
                if name != 'inventory.json':
                    if not name.startswith('allocation-plans/') or not name.endswith('.json'):
                        raise ValueError('档案备份包含非档案文件。')
                    validate_id(name[len('allocation-plans/') : -5])
                if (
                    not isinstance(entry, dict)
                    or set(entry) != {'bytes', 'sha256'}
                    or type(entry['bytes']) is not int
                    or entry['bytes'] < 0
                    or entry['bytes'] != archive.getinfo(name).file_size
                    or not isinstance(entry['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                    or entry['bytes'] > (MAX_JSON if name == 'inventory.json' else 4 * 1024 * 1024)
                ):
                    raise ValueError('档案文件元数据错误或文件过大。')
                raw = _read_member(archive, name, cancel_event)
                if len(raw) != entry['bytes'] or _sha(raw) != entry['sha256']:
                    raise ValueError('档案文件哈希错误。')
                if name == 'inventory.json':
                    backup = strict_json(raw)
                else:
                    plan = _plan(raw, name, cancel_event, plan_evaluator=plan_evaluator)
                    plans[plan['plan_id']] = plan
            if (
                not isinstance(backup, dict)
                or backup.get('profiles', {}).keys() != {meta['source_profile']}
                or backup.get('revision') != meta['source_inventory_revision']
            ):
                raise ValueError('档案备份身份或修订不一致。')
            settings = _settings(meta['settings'], backup['profiles'])
        elif 'bundle.json' in names or ('inventory.json' in names and 'profile.json' not in names):
            # 旧版声骸库/完整备份不再支持：只接受当前档案包格式。
            raise ValueError('旧版声骸库或完整备份不再支持；请使用当前档案导出包。')
        else:
            raise ValueError('请选择当前格式的档案备份。')
    checkpoint(cancel_event)
    if not backup.get('profiles'):
        raise ValueError('备份中没有档案。')
    if not all(plan['request']['profile_id'] in backup['profiles'] for plan in plans.values()):
        raise ValueError('历史方案引用缺失的档案。')
    validate_current_plan_references(
        backup.get('current_loadouts', ()),
        plans,
        cancel_event=cancel_event,
        plan_evaluator=plan_evaluator,
    )
    # New format is intentionally image-free, including historical evidence.
    if kind == 'profile' and (
        without_images(backup) != backup
        or any(without_images(plan) != plan for plan in plans.values())
    ):
        raise ValueError('档案备份应不包含截图引用。')
    backup = without_images(backup)
    plans = {identity: without_images(plan) for identity, plan in plans.items()}
    with EchoRepository(root / 'model-validation', plan_evaluator=plan_evaluator) as validator:
        validator.import_backup(backup, cancel_event=cancel_event)
    return kind, backup, plans, settings, warnings


def _contents(pinned, root, cancel_event, progress, *, plan_evaluator=None):
    try:
        return _read_contents(pinned, root, cancel_event, progress, plan_evaluator=plan_evaluator)
    except (KeyError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError('档案备份字段或结构错误；本机数据保留。') from error


def _bound_store(repository, store):
    if store.folder != repository.data_dir / 'allocation-plans':
        raise ValueError('档案方案存储应与当前声骸库在同一数据目录。')


def _preview(digest, contents):
    kind, backup, plans, settings, warnings = contents
    return ProfileArchivePreview(
        digest,
        kind,
        tuple(sorted(backup['profiles'].items())),
        len(backup['items']),
        len(backup.get('history', ())),
        len(backup.get('pending_captures', ())),
        len(backup.get('current_loadouts', ())),
        len(plans),
        settings,
        backup.get('revision', 0),
        warnings,
    )


def inspect_profile_archive(source, *, plan_evaluator=None, cancel_event=None, progress=None):
    with _pinned_archive(source, cancel_event) as (pinned, digest, root):
        return _preview(
            digest, _contents(pinned, root, cancel_event, progress, plan_evaluator=plan_evaluator)
        )


def export_profile_archive(
    repository, store, preferences, source_profile, destination, *, cancel_event=None, progress=None
):
    _bound_store(repository, store)
    validate_id(source_profile)
    destination = Path(destination).resolve()
    if destination.suffix.lower() != '.zip':
        raise ValueError('档案备份输出应为 ZIP 文件。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    full = repository.export_backup(cancel_event=cancel_event)
    backup = _profile_backup(full, source_profile)
    settings = dict(_scoped_settings(preferences, backup['profiles']))
    selected = _selected_plans(store, source_profile, cancel_event)
    validate_current_plan_references(
        backup.get('current_loadouts', ()),
        {value['plan_id']: value for _, value in selected.values()},
        cancel_event=cancel_event,
        plan_evaluator=repository.plan_evaluator,
    )
    raw_members = {'inventory.json': canonical_json(backup).encode('utf8')}
    raw_members.update(
        {
            'allocation-plans/' + name: plan_without_images(plan)
            for name, (_, plan) in selected.items()
        }
    )
    if (
        len(raw_members['inventory.json']) > MAX_JSON
        or sum(map(len, raw_members.values())) > MAX_BACKUP_BYTES
    ):
        raise ValueError('档案备份超过大小限制；原文件保留。')
    meta = dict(
        kind=KIND,
        schema_version=1,
        source_profile=source_profile,
        source_inventory_revision=backup['revision'],
        settings=settings,
        members={name: dict(bytes=len(raw), sha256=_sha(raw)) for name, raw in raw_members.items()},
    )
    manifest = canonical_json(meta).encode('utf8')
    if len(manifest) > MAX_JSON:
        raise ValueError('档案设置或清单超过大小限制。')
    fd, temporary = tempfile.mkstemp(prefix='.echo-profile-', suffix='.zip', dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            _write_member(archive, 'profile.json', manifest, cancel_event)
            for index, (name, raw) in enumerate(raw_members.items()):
                report_progress(progress, '写入档案备份', index, len(raw_members), cancel_event)
                _write_member(archive, name, raw, cancel_event)
        inspect_profile_archive(
            temporary,
            cancel_event=cancel_event,
            progress=progress,
            plan_evaluator=repository.plan_evaluator,
        )
        with repository._loadout_lock(cancel_event), store._operation_lock(cancel_event):
            if repository.revision != full['revision']:
                raise ValueError('导出期间声骸库已变化；请重新备份。')
            current = _selected_plans(store, source_profile, cancel_event)
            if {name: _sha(raw) for name, (raw, _) in current.items()} != {
                name: _sha(raw) for name, (raw, _) in selected.items()
            }:
                raise ValueError('导出期间档案方案或进度已变化；请重新备份。')
            with open(temporary, 'rb+') as stream:
                os.fsync(stream.fileno())
            report_progress(
                progress, '发布档案备份', len(raw_members), len(raw_members), cancel_event
            )
            os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _preserve_evidence(backup, existing):
    """Equal image-free content retains the local immutable evidence pointer."""
    adjusted = deepcopy(backup)
    local_items = {row['item_id']: row for row in existing['items']}
    local_pending = {
        row['record']['item_id']: row['record'] for row in existing.get('pending_captures', ())
    }
    for row in adjusted['items']:
        local = local_items.get(row['item_id'])
        if local is not None and dict(row, screenshot_ref='') == dict(local, screenshot_ref=''):
            row['screenshot_ref'] = local['screenshot_ref']
    for row in adjusted.get('pending_captures', ()):
        value = row['record']
        local = local_pending.get(value['item_id'])
        if local is not None and dict(value, screenshot_ref='') == dict(local, screenshot_ref=''):
            value['screenshot_ref'] = local['screenshot_ref']
    return adjusted


def _frozen_plan(payload):
    value = without_images(payload)
    value.pop('completed', None)
    value.pop('progress_revision', None)
    return canonical_json(value)


def _validate_imported_requirements(settings, requirements_checks, cancel_event):
    """Reject imported role requirements the native terminal would reject.

    The caller keeps the existing Python constructor checks as a transition
    guard; this pass adds the shared native verdict before any file is written.
    """
    if requirements_checks is None:
        return
    for key, raw in settings:
        if key != 'ComparisonSettingsJSON' or not raw:
            continue
        data = strict_json(raw)
        for profile_id, row in data.get('profiles', {}).items():
            calculation = row.get('calculation') or {}
            specs = requirements_from_rows(calculation.get('role_requirements', []))
            if not specs:
                continue
            request = requirements_checks.start(specs, cancel_event=cancel_event)
            request.future.result(timeout=12)
            errors = requirements_checks.response(request)
            rejected = next((message for _, message in errors if message), None)
            if rejected:
                raise ValueError(f'档案 {profile_id} 的角色要求无效：{rejected}')


def import_profile_archive(
    repository,
    store,
    source,
    *,
    expected_revision,
    expected_archive_sha256,
    cancel_event=None,
    progress=None,
    requirements_checks=None,
):
    _bound_store(repository, store)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('请先预览并确认当前声骸库修订。')
    if not isinstance(expected_archive_sha256, str) or not re.fullmatch(
        '[0-9a-f]{64}', expected_archive_sha256
    ):
        raise ValueError('请先预览并确认备份文件。')
    with _pinned_archive(source, cancel_event) as (pinned, digest, root):
        if digest != expected_archive_sha256:
            raise ValueError('备份文件在预览后已变化；请重新预览。')
        _, backup, plans, settings, warnings = _contents(
            pinned, root, cancel_event, progress, plan_evaluator=repository.plan_evaluator
        )
        _validate_imported_requirements(settings, requirements_checks, cancel_event)
        created = []
        with (
            repository._loadout_lock(cancel_event),
            store._operation_lock(cancel_event),
            ExitStack() as locks,
        ):
            if repository.revision != expected_revision:
                raise ValueError('声骸库在预览后已变化；未导入，请重新确认。')
            existing = repository.export_backup(cancel_event=cancel_event)
            adjusted = _preserve_evidence(backup, existing)
            # Detect all inventory/pending/current conflicts before any plan
            # file is published. Keep the repository lock through final CAS.
            with EchoRepository(
                root / 'merge-preflight', plan_evaluator=repository.plan_evaluator
            ) as validator:
                validator.restore_new_snapshot(existing, cancel_event=cancel_event)
                validator.import_backup(
                    adjusted, expected_revision=expected_revision, cancel_event=cancel_event
                )
            preserve = 0
            missing = []
            for identity, payload in sorted(plans.items()):
                locks.enter_context(plan_file_lock(store.folder, identity))
                path = store._path(identity)
                if path.is_symlink():
                    raise ValueError('现有方案路径异常；原数据保留。')
                if path.exists():
                    local = store._load(identity, cancel_event=cancel_event)
                    if _frozen_plan(local) != _frozen_plan(payload):
                        raise ValueError('同 UUID 方案的冻结内容冲突；未覆盖方案或进度。')
                    preserve += 1
                else:
                    missing.append(payload)
            checkpoint(cancel_event)
            try:
                for payload in missing:
                    checkpoint(cancel_event)
                    store._write(payload, cancel_event=cancel_event)
                    created.append(store._path(payload['plan_id']))
                added = repository.import_backup(
                    adjusted,
                    expected_revision=expected_revision,
                    cancel_event=cancel_event,
                    progress=progress,
                )
            except BaseException:
                # Locks remain held; only paths created by this call are removed.
                for path in reversed(created):
                    path.unlink(missing_ok=True)
                raise
            local_profiles = repository.profiles()
            new_profiles = tuple(
                sorted(
                    identity
                    for identity in backup['profiles']
                    if identity not in existing['profiles']
                )
            )
            preserved_names = sum(
                identity in existing['profiles'] and existing['profiles'][identity] != name
                for identity, name in backup['profiles'].items()
            )
            return ProfileImportReport(
                added,
                tuple(
                    (identity, local_profiles[identity]) for identity in sorted(backup['profiles'])
                ),
                new_profiles,
                preserved_names,
                len(created),
                preserve,
                settings,
                repository.revision,
                warnings,
            )
