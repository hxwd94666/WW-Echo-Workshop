# 调度库存和档案导入导出作业。
"""Runtime-owned, single-flight backup worker; no Qt or game input."""

from dataclasses import dataclass, replace
from pathlib import Path
import threading

from echo_workshop.storage.archives import export_archive, import_archive
from echo_workshop.application.operations import OperationCancelled
from echo_workshop.application.diagnostics import begin_job


@dataclass(frozen=True)
class BackupStatus:
    job_id: int = 0
    kind: str = ''
    state: str = 'idle'
    message: str = '尚无备份作业；旧库存 ZIP 不含方案；完整业务快照另含方案和插件设置'
    done: int = 0
    total: int = 0
    result: str = ''
    payload: object = None

    @property
    def busy(self):
        return self.state in ('running', 'cancelling')


class _Cancellation:
    def __init__(self, local, runtime):
        self.local, self.runtime = local, runtime

    def is_set(self):
        return self.local.is_set() or self.runtime.is_set()


class BackupService:
    def __init__(
        self,
        repository,
        stop_event,
        *,
        before_restore=None,
        plans=None,
        settings_reader=None,
        workspaces=None,
        requirements_checks=None,
    ):
        self.repository, self.stop_event = repository, stop_event
        self._lock = threading.RLock()
        self._status = BackupStatus()
        self._thread = None
        self._cancel = threading.Event()
        self._closed = False
        self.before_restore = before_restore
        self.plans, self.settings_reader = plans, settings_reader
        self.workspaces = workspaces
        self.requirements_checks = requirements_checks
        self._diagnostic_job = None

    @property
    def status(self):
        with self._lock:
            return self._status

    @property
    def restoring(self):
        status = self.status
        return status.busy and status.kind in (
            'import',
            'profile-import',
            'profile-delete',
        )

    def start(self, kind, path, *, destination=None, workspace_request=None):
        if kind not in (
            'profile-export',
            'profile-preview',
            'profile-import',
            'profile-delete',
            'export',
            'import',
        ):
            raise ValueError('请选择导出或恢复作业')
        path = Path(path).resolve()
        with self._lock:
            if self._closed or self.stop_event.is_set():
                raise ValueError('备份服务已关闭')
            if self._status.busy or (self._thread is not None and self._thread.is_alive()):
                raise ValueError('已有备份作业；请等待完成或取消')
            preferences = None
            if kind.startswith('profile-'):
                preferences = dict(workspace_request or {})
                if kind in ('profile-export', 'profile-delete'):
                    if self.plans is None or self.settings_reader is None:
                        raise ValueError('档案导出服务尚未连接。')
                    preferences['preferences'] = self.settings_reader()
                if kind == 'profile-delete':
                    if (
                        self.workspaces is None
                        or path != (self.workspaces.root / 'deleted-profiles').resolve()
                    ):
                        raise ValueError('删除恢复包须保存在插件专用数据目录中。')
            if (
                kind
                in (
                    'import',
                    'profile-import',
                    'profile-delete',
                )
                and self.before_restore is not None
            ):
                self.before_restore()
            revision = self.repository.revision if kind == 'import' else None
            self._cancel = threading.Event()
            event = _Cancellation(self._cancel, self.stop_event)
            self._status = BackupStatus(self._status.job_id + 1, kind, 'running', '后台作业启动中')
            self._diagnostic_job = begin_job('backup-' + kind)
            self._thread = threading.Thread(
                target=self._run,
                args=(kind, path, revision, event, preferences, destination),
                name='echo-inventory-backup',
                daemon=True,
            )
            self._thread.start()
            return self._status.job_id

    def _progress(self, stage, done, total):
        with self._lock:
            self._status = replace(self._status, message=stage, done=done, total=total)
            if self._diagnostic_job is not None:
                label = (
                    'publishing'
                    if '发布' in stage or '提交' in stage
                    else 'verifying'
                    if '校验' in stage or '复查' in stage
                    else 'writing'
                    if '写入' in stage or '保存' in stage
                    else 'reading'
                    if '读取' in stage or '检查' in stage
                    else 'processing'
                )
                self._diagnostic_job.stage(label, {'done': done, 'total': total})

    def _write_profile_delete_receipt(self, profile_id, archive):
        """Keep scoped Config cleanup resumable after a committed DB removal."""
        import os
        import tempfile
        from echo_workshop.core.models import canonical_json

        folder = self.repository.data_dir / 'profile-delete-receipts'
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or folder.resolve().parent != self.repository.data_dir:
            raise ValueError('档案删除回执目录异常。')
        target = folder / (profile_id + '.json')
        body = canonical_json(
            dict(schema_version=1, deleted_profile=profile_id, archive=str(archive))
        ).encode('utf8')
        fd, temporary = tempfile.mkstemp(prefix='.profile-delete-', dir=folder)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _run(self, kind, path, revision, event, preferences=None, destination=None):
        payload = None
        diagnostic = self._diagnostic_job or begin_job('backup-' + kind)
        failure = None
        diagnostic.stage('processing')
        try:
            if kind.startswith('profile-'):
                from echo_workshop.storage.profile_transfer import (
                    export_profile_archive,
                    inspect_profile_archive,
                    import_profile_archive,
                )

                if kind == 'profile-preview':
                    payload = inspect_profile_archive(
                        path,
                        plan_evaluator=self.repository.plan_evaluator,
                        cancel_event=event,
                        progress=self._progress,
                    )
                    result = path
                    message = '档案已检查；确认后合并，原数据和全局设置保留。'
                elif kind == 'profile-export':
                    result = export_profile_archive(
                        self.repository,
                        self.plans,
                        preferences['preferences'],
                        preferences['profile_id'],
                        path,
                        cancel_event=event,
                        progress=self._progress,
                    )
                    message = '档案已导出（不含截图及全局设置）：' + str(result)
                elif kind == 'profile-import':
                    payload = import_profile_archive(
                        self.repository,
                        self.plans,
                        path,
                        expected_revision=preferences['expected_revision'],
                        expected_archive_sha256=preferences['expected_archive_sha256'],
                        cancel_event=event,
                        progress=self._progress,
                        requirements_checks=self.requirements_checks,
                    )
                    # The UI alone mutates Config. Leave a durable receipt so a
                    # window closed after the DB commit can resume scoped config.
                    try:
                        import os
                        import tempfile
                        from echo_workshop.core.models import canonical_json

                        folder = self.repository.data_dir / 'profile-import-receipts'
                        folder.mkdir(exist_ok=True)
                        if (
                            folder.is_symlink()
                            or folder.resolve().parent != self.repository.data_dir
                        ):
                            raise ValueError('档案配置回执目录异常。')
                        receipt = folder / (preferences['expected_archive_sha256'] + '.json')
                        body = canonical_json(
                            dict(
                                schema_version=1,
                                profiles=[key for key, _ in payload.profiles],
                                settings_json=list(payload.settings_json),
                            )
                        ).encode('utf8')
                        fd, temporary = tempfile.mkstemp(prefix='.profile-receipt-', dir=folder)
                        try:
                            with os.fdopen(fd, 'wb') as stream:
                                stream.write(body)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(temporary, receipt)
                        finally:
                            if os.path.exists(temporary):
                                os.unlink(temporary)
                        message = f'已导入档案，新增 {payload.added} 件；已有配置和方案进度保留。'
                    except (OSError, ValueError):
                        message = f'声骸已导入，新增 {payload.added} 件；匹配配置请保持窗口开启或重新导入。'
                    result = path
                else:
                    from uuid import uuid4
                    from echo_workshop.application.operations import checkpoint

                    path.mkdir(parents=True, exist_ok=True)
                    archive = path / (preferences['profile_id'] + '-' + uuid4().hex + '.zip')
                    export_profile_archive(
                        self.repository,
                        self.plans,
                        preferences['preferences'],
                        preferences['profile_id'],
                        archive,
                        cancel_event=event,
                        progress=self._progress,
                    )
                    checkpoint(event)
                    counts = self.repository.delete_profile(
                        preferences['profile_id'],
                        expected_revision=preferences['expected_revision'],
                    )
                    payload = dict(
                        profile_id=preferences['profile_id'], counts=counts, archive=str(archive)
                    )
                    result = archive
                    message = '档案已移除；恢复包可用“导入档案”找回：' + str(archive)
                    try:
                        self._write_profile_delete_receipt(preferences['profile_id'], archive)
                    except (OSError, ValueError):
                        message += '；匹配配置清理请保持窗口开启。'
            elif kind == 'export':
                result = export_archive(
                    self.repository, path, cancel_event=event, progress=self._progress
                )
                message = '库存备份已完成：' + str(result) + '（不含图片/宿主配置，可跨设备恢复）'
            elif kind == 'import':
                added = import_archive(
                    self.repository,
                    path,
                    expected_revision=revision,
                    cancel_event=event,
                    progress=self._progress,
                )
                result = added
                message = f'库存恢复已提交，新增 {added} 件；收录保持停止，请核对后明确开启'
            # Do not label a successfully committed operation as cancelled just
            # because a late cancellation request arrived after its boundary.
            state = 'completed'
        except OperationCancelled as error:
            state, message, result = 'cancelled', str(error), ''
        except Exception as error:
            failure = error
            state, message, result = (
                'failed',
                '数据作业未完成（' + type(error).__name__ + '）；请检查文件或导出诊断报告',
                '',
            )
        with self._lock:
            self._status = replace(
                self._status, state=state, message=message, result=str(result), payload=payload
            )
            counts = {'done': self._status.done, 'total': self._status.total}
            if state == 'completed' and kind == 'import' and type(result) is int:
                counts['added'] = result
            diagnostic.finish(state, summary_code='backup_' + state, counters=counts, error=failure)

    def cancel(self):
        with self._lock:
            if self._status.busy:
                self._cancel.set()
                self._status = replace(
                    self._status, state='cancelling', message='已请求取消；等待当前 I/O 检查点'
                )

    def wait(self):
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def cancel_and_wait(self):
        self.cancel()
        self.wait()

    def close(self):
        with self._lock:
            self._closed = True
        self.cancel_and_wait()
