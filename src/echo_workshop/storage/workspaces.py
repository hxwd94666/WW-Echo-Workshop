# 维护数据工作区和恢复控制状态。
"""Explicit next-start workspace selection. No live runtime or Config mutation.

Selectors are local cooperating-process CAS records, not a cross-service
transaction. Recovered copies remain editable after activation. Settings in
preferences.json remain a preview, never an instruction to enable shortcuts.
"""

from dataclasses import dataclass
import hashlib
import os
import re
import sqlite3
from pathlib import Path
import tempfile

from echo_workshop.storage.archives import MAX_BACKUP_BYTES, MAX_RECORDS, _records
from echo_workshop.storage.evidence import EvidenceStore
from echo_workshop.core.models import EchoRecord, canonical_json, validate_id
from echo_workshop.core.cancellation import checkpoint, report_progress
from echo_workshop.storage.locks import plan_file_lock
from echo_workshop.storage.inventory import EchoRepository
from echo_workshop.resources.http import strict_json
from echo_workshop.storage.workspace_bundle import (
    KIND,
    MAX_JSON,
    _plan,
    _plan_paths,
    MAX_PLAN_BYTES,
    validate_preferences,
)
from echo_workshop.storage.plans import AllocationStore
from echo_workshop.storage.loadouts import validate_current_plan_references

SELECTOR_LOCK = 'fb8d39e9-a85c-4722-9cd7-6a4e4bf471bd'
MAX_SELECTOR_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class WorkspacePreview:
    target: str
    profiles: tuple
    items: int
    pending: int
    plans: int
    source_revision: int
    preferences_available: bool
    files: tuple


class WorkspaceManager:
    def __init__(self, root, *, plan_evaluator=None):
        self.plan_evaluator = plan_evaluator
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.control = self.root / 'workspace-control'
        self.control.mkdir(exist_ok=True)
        if self.control.is_symlink():
            raise ValueError('工作区控制目录应为实际目录')
        self.path = self.control / 'selection.json'
        self.message = ''

    def target_path(self, target):
        if target == '':
            return self.root
        validate_id(target)
        parent = self.root / 'recovered-workspaces'
        path = parent / target
        if parent.is_symlink() or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('恢复副本应为插件根目录中的实际 UUID 目录')
        return path

    def _read(self):
        if self.control.is_symlink() or self.path.is_symlink():
            raise ValueError('工作区选择记录应位于实际控制目录')
        if not self.path.exists():
            return dict(schema_version=1, revision=0, active='', pending=None, profiles={})
        if self.path.is_symlink() or self.path.stat().st_size > MAX_SELECTOR_BYTES:
            raise ValueError('工作区选择记录路径/大小错误')
        state = strict_json(self.path.read_bytes())
        if (
            not isinstance(state, dict)
            or set(state) != {'schema_version', 'revision', 'active', 'pending', 'profiles'}
            or type(state['schema_version']) is not int
            or state['schema_version'] != 1
            or type(state['revision']) is not int
            or not 0 <= state['revision'] < 2**63
            or not isinstance(state['profiles'], dict)
        ):
            raise ValueError('工作区选择记录格式错误；保留现有数据')
        self.target_path(state['active'])
        for target, profile in state['profiles'].items():
            self.target_path(target)
            if profile:
                validate_id(profile)
        pending = state['pending']
        if pending is not None:
            if (
                not isinstance(pending, dict)
                or set(pending) != {'target', 'files', 'profile'}
                or not isinstance(pending['files'], dict)
            ):
                raise ValueError('待切换记录格式错误')
            self.target_path(pending['target'])
            if pending['profile']:
                validate_id(pending['profile'])
            if len(pending['files']) > MAX_RECORDS * 10 + 1005:
                raise ValueError('待切换文件清单超过限制')
            for name, digest in pending['files'].items():
                self._file(self.target_path(pending['target']), name)
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)
                ):
                    raise ValueError('待切换文件哈希格式错误')
            if 'inventory.sqlite3' not in pending['files']:
                raise ValueError('待切换库存校验缺失')
        return state

    @property
    def state(self):
        return self._read()

    def candidates(self):
        parent = self.root / 'recovered-workspaces'
        if parent.is_symlink():
            raise ValueError('恢复副本目录应为实际目录')
        result = ['']
        if parent.exists():
            for entry in sorted(parent.iterdir()):
                if not entry.is_dir() or entry.name.startswith('.'):
                    continue
                try:
                    self.target_path(entry.name)
                except ValueError:
                    continue
                result.append(entry.name)
                if len(result) > 1001:
                    raise ValueError('恢复副本超过 1000；请整理后重试，未截断列表')
        return tuple(result)

    def _file(self, directory, name):
        if not isinstance(name, str):
            raise ValueError('工作区校验路径类型错误')
        if name not in (
            'inventory.sqlite3',
            'preferences.json',
            'bundle.json',
            'weight-settings.json',
            'weights/current.json',
        ):
            if name.startswith('allocation-plans/') and name.endswith('.json'):
                validate_id(name[len('allocation-plans/') : -5])
            elif name.startswith('weights/') and re.fullmatch(r'weights/[0-9a-f]{64}\.json', name):
                pass
            elif name.startswith('screenshots/'):
                self._evidence(directory)._path(name)
            else:
                raise ValueError('工作区校验路径错误')
        path = directory / name
        for part in (path, *path.parents):
            if part == directory:
                break
            if part.is_symlink():
                raise ValueError('工作区文件不接受符号链接')
        if directory.resolve() not in path.resolve().parents:
            raise ValueError('工作区校验路径越界')
        return path

    @staticmethod
    def _evidence(directory):
        evidence = EvidenceStore.__new__(EvidenceStore)
        evidence.data_dir = directory
        evidence.folder = directory / 'screenshots'
        return evidence

    def _hashes(self, directory, names, cancel_event=None, progress=None):
        result = {}
        total_bytes = 0
        for index, name in enumerate(sorted(names)):
            report_progress(progress, '复查工作区文件', index, len(names), cancel_event)
            path = self._file(directory, name)
            total_bytes += path.stat().st_size
            if total_bytes > MAX_BACKUP_BYTES:
                raise ValueError('工作区校验数据超过 256 MiB')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while True:
                    checkpoint(cancel_event)
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            result[name] = digest.hexdigest()
        return result

    @staticmethod
    def _check_journal(directory):
        for suffix in ('-wal', '-journal'):
            extra = directory / ('inventory.sqlite3' + suffix)
            if extra.exists() and extra.stat().st_size:
                raise ValueError('工作区数据库尚有活动日志，请关闭使用它的实例后重试')

    def inspect(self, target, *, cancel_event=None, progress=None):
        directory = self.target_path(target)
        if not directory.is_dir():
            raise ValueError('恢复副本目录已不存在；原工作区保留')
        names = {'inventory.sqlite3'}
        observed = {}
        self._file(directory, 'inventory.sqlite3')
        if target:
            marker = self._file(directory, 'bundle.json')
            if marker.stat().st_size > MAX_JSON:
                raise ValueError('恢复副本标记过大')
            raw = marker.read_bytes()
            manifest = strict_json(raw)
            observed['bundle.json'] = hashlib.sha256(raw).hexdigest()
            if (
                not isinstance(manifest, dict)
                or manifest.get('kind') != KIND
                or type(manifest.get('schema_version')) is not int
                or manifest['schema_version'] != 1
            ):
                raise ValueError('请选择由完整快照生成的恢复副本')
            names.add('bundle.json')
        # A hot WAL/journal is not a sealed next-start snapshot.
        self._check_journal(directory)
        report_progress(progress, '只读校验工作区库存', 0, 1, cancel_event)
        initial_db_hash = self._hashes(directory, {'inventory.sqlite3'}, cancel_event)[
            'inventory.sqlite3'
        ]
        with EchoRepository(
            directory, read_only=True, plan_evaluator=self.plan_evaluator
        ) as repository:
            backup = repository.export_backup(cancel_event=cancel_event)
        records = list(_records(backup, cancel_event))
        with tempfile.TemporaryDirectory(prefix='echo-workspace-check-') as temp:
            with EchoRepository(temp, plan_evaluator=self.plan_evaluator) as clone:
                clone.restore_new_snapshot(backup, cancel_event=cancel_event, progress=progress)
        references = {row.screenshot_ref for row in records if row.screenshot_ref}
        preferences = None
        if target:
            path = self._file(directory, 'preferences.json')
            if path.stat().st_size > MAX_JSON:
                raise ValueError('副本设置文件过大')
            raw = path.read_bytes()
            wrapper = strict_json(raw)
            observed['preferences.json'] = hashlib.sha256(raw).hexdigest()
            if (
                not isinstance(wrapper, dict)
                or set(wrapper) != {'schema_version', 'values'}
                or type(wrapper['schema_version']) is not int
                or wrapper['schema_version'] != 1
            ):
                raise ValueError('副本设置格式错误')
            preferences = validate_preferences(wrapper['values'])
            names.add('preferences.json')
        if (directory / 'weight-settings.json').exists():
            from echo_workshop.resources.weights import WeightSnapshot, WeightProvider

            path = self._file(directory, 'weight-settings.json')
            if path.stat().st_size > MAX_JSON:
                raise ValueError('自定义权重备份文件过大')
            raw = path.read_bytes()
            WeightSnapshot.from_dict(strict_json(raw))
            observed['weight-settings.json'] = hashlib.sha256(raw).hexdigest()
            names.add('weight-settings.json')
            provider = WeightProvider(directory)
            if provider.last_error:
                raise ValueError('副本权重保存损坏：' + provider.last_error)
            for name in ('weights/current.json', 'weights/' + provider.active.version + '.json'):
                path = self._file(directory, name)
                if path.exists():
                    names.add(name)
        # AllocationStore creates its folder. Avoid that during read-only inspection.
        store = AllocationStore.__new__(AllocationStore)
        store.folder = directory / 'allocation-plans'
        paths = _plan_paths(store, cancel_event)
        plan_payloads = {}
        for index, path in enumerate(paths):
            report_progress(progress, '校验工作区方案', index, len(paths), cancel_event)
            name = 'allocation-plans/' + path.name
            path = self._file(directory, name)
            if path.stat().st_size > MAX_PLAN_BYTES:
                raise ValueError('工作区方案超过 4 MiB')
            raw = path.read_bytes()
            payload = _plan(raw, name, cancel_event, plan_evaluator=self.plan_evaluator)
            plan_payloads[payload['plan_id']] = payload
            observed[name] = hashlib.sha256(raw).hexdigest()
            if payload['request']['profile_id'] not in backup['profiles']:
                raise ValueError('方案档案不在工作区库存中')
            references.update(
                row.screenshot_ref
                for row in (EchoRecord.from_dict(x) for x in payload['records'])
                if row.screenshot_ref
            )
            names.add(name)
        validate_current_plan_references(
            backup.get('current_loadouts', ()),
            plan_payloads,
            plan_evaluator=self.plan_evaluator,
            cancel_event=cancel_event,
        )
        evidence = self._evidence(directory)
        for index, name in enumerate(sorted(references)):
            report_progress(progress, '校验工作区截图', index, len(references), cancel_event)
            self._file(directory, name)
            raw = evidence.read(name)
            names.add(name)
            observed[name] = hashlib.sha256(raw).hexdigest()
        hashes = self._hashes(directory, names, cancel_event, progress)
        if (
            hashes['inventory.sqlite3'] != initial_db_hash
            or any(hashes[name] != value for name, value in observed.items())
            or tuple(path.name for path in _plan_paths(store, cancel_event))
            != tuple(path.name for path in paths)
        ):
            raise ValueError('校验期间工作区已变化，请重新预览')
        return WorkspacePreview(
            target,
            tuple(backup['profiles'].items()),
            len(backup['items']),
            len(backup['pending_captures']),
            len(paths),
            backup['revision'],
            preferences is not None,
            tuple(hashes.items()),
        )

    def _write(self, state):
        if self.control.is_symlink() or self.path.is_symlink():
            raise ValueError('工作区选择记录应位于实际控制目录')
        raw = canonical_json(state).encode('utf-8')
        if len(raw) > MAX_SELECTOR_BYTES:
            raise ValueError('工作区选择记录超过限制')
        fd, temporary = tempfile.mkstemp(prefix='.selection-', dir=self.control)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def cancel_pending(self, expected_revision):
        with plan_file_lock(self.control, SELECTOR_LOCK):
            state = self._read()
            if state['revision'] != expected_revision:
                raise ValueError('工作区选择已变化，请刷新')
            state['pending'] = None
            state['revision'] += 1
            self._write(state)
            self.message = ''

    @staticmethod
    def _plan_set_matches(directory, files):
        store = AllocationStore.__new__(AllocationStore)
        store.folder = directory / 'allocation-plans'
        actual = {'allocation-plans/' + path.name for path in _plan_paths(store)}
        return actual == {name for name in files if name.startswith('allocation-plans/')}

    def startup(self):
        with plan_file_lock(self.control, SELECTOR_LOCK):
            state = self._read()
            pending = state['pending']
            activated = False
            if pending is not None:
                try:
                    directory = self.target_path(pending['target'])
                    self._check_journal(directory)
                    if self._hashes(directory, pending['files']) != pending[
                        'files'
                    ] or not self._plan_set_matches(directory, pending['files']):
                        raise ValueError('副本文件已变化，请重新预览')
                    # Ensure a missing/unsupported DB is not turned into an empty inventory.
                    with EchoRepository(
                        directory, read_only=True, plan_evaluator=self.plan_evaluator
                    ) as repository:
                        repository.profiles()
                except (ValueError, OSError, sqlite3.Error) as error:
                    self.message = '预约切换未应用，保持原工作区：' + str(error)
                else:
                    state['active'] = pending['target']
                    state['pending'] = None
                    state['profiles'][state['active']] = pending['profile']
                    state['revision'] += 1
                    self._write(state)
                    self.message = '已应用预约的工作区；收录保持停止，恢复设置须另行明确确认。'
                    activated = True
            target = state['active']
            directory = self.target_path(target)
            if target and not (directory / 'inventory.sqlite3').is_file():
                raise ValueError('活动副本库存缺失；数据保留，请修复选择记录或副本后重启')
            return target, directory, state['profiles'].get(target, '') if activated else ''
