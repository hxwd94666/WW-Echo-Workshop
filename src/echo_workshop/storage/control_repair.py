# 核查并修复工作区控制记录。
"""Explicit, hash-guarded control-record repair; never edit inventory or Config."""

from dataclasses import dataclass
import hashlib
import os
import secrets
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from echo_workshop.core.models import canonical_json
from echo_workshop.application.operations import checkpoint
from echo_workshop.storage.locks import plan_file_lock
from echo_workshop.storage.preferences import PreferenceRecovery, LOCK_ID
from echo_workshop.storage.inventory import EchoRepository
from echo_workshop.storage.workspaces import WorkspaceManager, SELECTOR_LOCK, MAX_SELECTOR_BYTES


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RepairPreview:
    kind: str
    source_hash: str
    source_bytes: int
    reason: str
    target: str
    target_files: tuple
    revision: int
    replacement: bytes


@dataclass(frozen=True)
class RepairResult:
    archive: str
    source_hash: str
    replacement_hash: str
    message: str


class ControlRepair:
    def __init__(self, config_file, *, plan_evaluator=None):
        self.config_file = Path(config_file).resolve()
        # Same host-relative namespace, without constructing a runtime/DB.
        from echo_workshop.application.runtime import data_dir_for_config

        self.manager = WorkspaceManager(
            data_dir_for_config(self.config_file), plan_evaluator=plan_evaluator
        )

    def _path(self, kind):
        if kind not in ('selection', 'preferences'):
            raise ValueError('请选择工作区选择记录或设置恢复记录')
        path = self.manager.control / (
            'selection.json' if kind == 'selection' else 'preferences-recovery.json'
        )
        if self.manager.control.is_symlink() or path.is_symlink():
            raise ValueError('控制记录应为实际文件；符号链接不参与修复')
        return path

    def _raw(self, kind):
        path = self._path(kind)
        if not path.is_file():
            raise ValueError('控制记录不存在；没有要隔离修复的文件')
        if path.stat().st_size > MAX_SELECTOR_BYTES:
            raise ValueError('控制记录超过 32 MiB；请保留文件并人工核对')
        raw = path.read_bytes()
        if len(raw) > MAX_SELECTOR_BYTES:
            raise ValueError('读取期间控制记录超过限制')
        return raw

    def selection_issue(self):
        # Permission/locking errors are not classified as corrupt JSON.
        self._path('selection')
        try:
            state = self.manager.state
            if state['active']:
                with EchoRepository(
                    self.manager.target_path(state['active']),
                    read_only=True,
                    plan_evaluator=self.manager.plan_evaluator,
                ) as repository:
                    repository.profiles()
        except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
            return str(error)
        return None

    def _issue(self, kind):
        if kind == 'selection':
            return self.selection_issue()
        try:
            PreferenceRecovery(self.manager, self.config_file).state
        except (ValueError, KeyError, TypeError) as error:
            return str(error)
        return None

    def preview(self, kind, target='', *, cancel_event=None, _revision=None):
        checkpoint(cancel_event)
        raw = self._raw(kind)
        issue = self._issue(kind)
        if issue is None:
            raise ValueError('记录可正常读取；不通过故障修复清除有效预约或历史')
        files = ()
        # Fresh repair epoch even for repeated identical corruption. Preserve
        # this preview's epoch during confirmation rechecks, never regenerate
        # or derive it from the damaged bytes. Leave room for later increments.
        revision = secrets.randbelow(2**62 - 1) + 1 if _revision is None else _revision
        if type(revision) is not int or not 0 < revision < 2**62:
            raise ValueError('修复代次格式错误')
        if kind == 'selection':
            # Full read-only validation, including DB/history/plans/PNG. An
            # absent original DB is an error, never an empty replacement.
            candidate = self.manager.inspect(target, cancel_event=cancel_event)
            files = candidate.files
            replacement = dict(
                schema_version=1, revision=revision, active=target, pending=None, profiles={}
            )
        else:
            if target:
                raise ValueError('设置恢复记录修复不选择工作区')
            replacement = dict(schema_version=1, revision=revision, pending=None, last=None)
        if self._raw(kind) != raw:
            raise ValueError('预览期间控制记录已变化，请重新预览')
        checkpoint(cancel_event)
        return RepairPreview(
            kind,
            digest(raw),
            len(raw),
            issue,
            target,
            files,
            revision,
            canonical_json(replacement).encode('utf-8'),
        )

    @staticmethod
    def _exclusive(path, raw):
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _atomic(path, raw):
        fd, temporary = tempfile.mkstemp(prefix='.control-repair-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def apply(self, preview, *, cancel_event=None):
        if not isinstance(preview, RepairPreview):
            raise ValueError('请先预览并明确确认修复')
        # Same lock order for both kinds. Cooperating writers use these exact
        # existing locks; age/existence of lock files is not ownership.
        with (
            plan_file_lock(self.manager.control, SELECTOR_LOCK),
            plan_file_lock(self.manager.control, LOCK_ID),
        ):
            fresh = self.preview(
                preview.kind, preview.target, cancel_event=cancel_event, _revision=preview.revision
            )
            if fresh != preview:
                raise ValueError('确认后控制记录或目标内容已变化，请重新预览')
            raw = self._raw(preview.kind)
            parent = self.manager.control / 'repairs'
            if parent.is_symlink():
                raise ValueError('隔离证据目录应为实际目录')
            parent.mkdir(exist_ok=True)
            archive = parent / str(uuid4())
            archive.mkdir(exist_ok=False)
            # Durable evidence first. If publication fails/interruption occurs,
            # original file remains and prepared evidence is retained.
            self._exclusive(archive / 'original.bin', raw)
            metadata = dict(
                schema_version=1,
                state='prepared',
                kind=preview.kind,
                source_hash=preview.source_hash,
                source_bytes=preview.source_bytes,
                replacement_hash=digest(preview.replacement),
                reason=preview.reason,
                target=preview.target,
                target_files=dict(preview.target_files),
                revision=preview.revision,
                replacement_record=preview.replacement.decode('utf-8'),
            )
            self._exclusive(archive / 'repair.json', canonical_json(metadata).encode('utf-8'))
            if digest((archive / 'original.bin').read_bytes()) != preview.source_hash:
                raise ValueError('隔离证据校验失败；原控制记录保留')
            if digest(self._raw(preview.kind)) != preview.source_hash:
                raise ValueError('发布前控制记录已变化；隔离证据保留，请重新预览')
            if preview.kind == 'selection':
                directory = self.manager.target_path(preview.target)
                self.manager._check_journal(directory)
                if self.manager._hashes(
                    directory, dict(preview.target_files), cancel_event
                ) != dict(preview.target_files) or not self.manager._plan_set_matches(
                    directory, dict(preview.target_files)
                ):
                    raise ValueError('发布前目标工作区已变化；原控制记录保留')
            checkpoint(cancel_event)
            self._atomic(self._path(preview.kind), preview.replacement)
            # Commit has happened: late cancellation/receipt failure must not
            # claim the original file is still active.
            metadata['state'] = 'applied'
            message = '控制记录已修复；重启后生效。库存/截图/方案/Config均未替换，收录未启动。'
            try:
                self._atomic(archive / 'repair.json', canonical_json(metadata).encode('utf-8'))
            except OSError as error:
                message += ' 完成记录未更新，请按前后哈希核对：' + str(error)
            return RepairResult(
                str(archive), preview.source_hash, digest(preview.replacement), message
            )
