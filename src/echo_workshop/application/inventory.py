# 读取有界库存快照并处理修订。
"""Full review rows, single active read and one latest request, with no Qt IO."""

from dataclasses import dataclass
import threading

from echo_workshop.application.operations import checkpoint, OperationCancelled
from echo_workshop.application.diagnostics import begin_job


@dataclass(frozen=True)
class ReviewResult:
    generation: int
    profile_id: str
    mode: int
    revision: int
    rows: tuple
    claims: tuple = ()
    pending_count: int = 0
    quality: tuple = ()


@dataclass(frozen=True)
class ReaderStatus:
    generation: int = 0
    state: str = 'idle'
    result: object = None
    message: str = ''


class _Cancel:
    def __init__(self, local, runtime):
        self.local, self.runtime = local, runtime

    def is_set(self):
        return self.local.is_set() or (self.runtime is not None and self.runtime.is_set())


class InventoryReader:
    def __init__(self, repository, stop_event=None, *, queries):
        self.repository, self.stop_event = repository, stop_event
        self.queries = queries
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._thread = None
        self._pending = None
        self._cancel = None
        self._closed = False
        self._status = ReaderStatus()

    @property
    def status(self):
        with self._lock:
            return self._status

    def request(self, profile, mode):
        if not isinstance(profile, str) or type(mode) is not int or mode not in range(3):
            raise ValueError('库存读取参数无效')
        with self._lock:
            if self._closed:
                raise ValueError('库存读取窗口已关闭')
            generation = self._status.generation + 1
            if self._cancel is not None:
                self._cancel.set()
            self._pending = generation, profile, mode
            self._status = ReaderStatus(generation, 'running', message='后台读取完整候选与待判定区')
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name='echo-inventory-reader', daemon=True
                )
                self._thread.start()
            return generation

    def _run(self):
        while True:
            with self._lock:
                if self._closed or self._pending is None:
                    self._thread = None
                    return
                generation, profile, mode = self._pending
                self._pending = None
                local = threading.Event()
                self._cancel = local
            event = _Cancel(local, self.stop_event)
            result = None
            try:
                checkpoint(event)
                revision, rows, claims = self.repository.review_snapshot(
                    profile, cancel_event=event, include_loadouts=True
                )
                rows, counts, quality = self.queries.review(rows, mode=mode, cancel_event=event)
                pending_count = counts['pending']
                result = ReviewResult(
                    generation,
                    profile,
                    mode,
                    revision,
                    rows,
                    tuple(claims.items()),
                    pending_count,
                    quality,
                )
                state, message = 'ready', '已读取完整候选；仅所选筛选，不代表整个游戏背包'
            except OperationCancelled:
                state, message = 'cancelled', '库存读取已取消；未发布部分结果'
            except Exception as error:
                state, message = 'failed', '库存读取失败，旧视图保留：' + str(error)
            with self._lock:
                self._cancel = None
                if not self._closed and generation == self._status.generation:
                    self._status = ReaderStatus(generation, state, result, message)

    def cancel(self):
        with self._lock:
            if self._closed:
                return
            if self._cancel is not None:
                self._cancel.set()
            self._pending = None
            self._status = ReaderStatus(
                self._status.generation + 1,
                'cancelled',
                message='库存读取已取消；旧视图不冒充最新，点重新读取继续',
            )

    def close(self):
        # No Qt calls. Serialize repeated unload/close until the worker exits.
        with self._stop_lock:
            with self._lock:
                self._closed = True
                self._pending = None
                if self._cancel is not None:
                    self._cancel.set()
                thread = self._thread
                self._status = ReaderStatus(self._status.generation + 1, 'closed')
            if thread is not None:
                thread.join()


def game_status_labels(record):
    from echo_workshop.capture.game_status import status_evidence

    evidence = status_evidence(record)
    fields = evidence.get('fields', {})
    fields = fields if isinstance(fields, dict) else {}
    labels = []
    for key, text in (('equipped', '已装备'),):
        if getattr(record, 'game_' + key) is True:
            details = fields.get('game_' + key, {})
            retained = isinstance(details, dict) and details.get('retained') is True
            labels.append(text + ('（旧）' if retained else ''))
    return tuple(labels)


class InventoryService:
    """库存查询与显式修改用例；SQL、CAS及保护事务仍由存储实现。"""

    def __init__(self, repository, stop_event):
        self._repository, self._stop_event = repository, stop_event
        self.after_mutation = lambda: None

    @property
    def revision(self):
        return self._repository.revision

    def try_revision(self):
        return self._repository.try_revision()

    def profiles(self):
        return self._repository.profiles()

    def get(self, identity):
        return self._repository.get(identity)

    def list(self, profile):
        return self._repository.list(profile)

    def history(self, identity):
        return self._repository.history(identity)

    def duplicate_candidates(self, record):
        return self._repository.duplicate_candidates(record)

    def pending_captures(self, profile):
        return self._repository.pending_captures(profile)

    def inventory_snapshot(self, profile, *, cancel_event=None, include_loadouts=False):
        return self._repository.inventory_snapshot(
            profile, cancel_event=cancel_event, include_loadouts=include_loadouts
        )

    def review_snapshot(self, profile, *, cancel_event=None, include_loadouts=False):
        return self._repository.review_snapshot(
            profile, cancel_event=cancel_event, include_loadouts=include_loadouts
        )

    def loadout_snapshot(self, profile, *, cancel_event=None):
        return self._repository.loadout_snapshot(profile, cancel_event=cancel_event)

    def profile_counts(self, profile, *, expected_revision):
        return self._repository.profile_counts(profile, expected_revision=expected_revision)

    def selection_impact(self, profile, identities, *, expected_revision):
        return self._repository.selection_impact(
            profile, identities, expected_revision=expected_revision
        )

    def _mutate(self, action, function):
        job = begin_job(
            'inventory-mutation', metadata={'inventory_revision': self.try_revision() or 0}
        )
        job.stage(action)
        try:
            checkpoint(self._stop_event)
            result = function()
        except OperationCancelled as error:
            job.finish('cancelled', error=error)
            raise
        except Exception as error:
            job.finish('failed', error=error)
            raise
        # 已提交动作不再检查取消，避免把成功提交报告成取消。
        job.finish('completed', counters={'committed': 1})
        self.after_mutation()
        return result

    def create_profile(self, name):
        return self._mutate('create-profile', lambda: self._repository.create_profile(name))

    def rename_profile(self, profile, name, *, expected_revision):
        return self._mutate(
            'rename-profile',
            lambda: self._repository.rename_profile(
                profile, name, expected_revision=expected_revision
            ),
        )

    def delete_profile(self, profile, *, expected_revision):
        return self._mutate(
            'delete-profile',
            lambda: self._repository.delete_profile(profile, expected_revision=expected_revision),
        )

    def clear_profile(self, profile, *, expected_revision):
        return self._mutate(
            'clear-profile',
            lambda: self._repository.clear_profile(profile, expected_revision=expected_revision),
        )

    def update(self, record, *, expected_revision):
        return self._mutate(
            'edit', lambda: self._repository.update(record, expected_revision=expected_revision)
        )

    def confirm(self, identity, *, expected_revision):
        return self._mutate(
            'confirm',
            lambda: self._repository.confirm(identity, expected_revision=expected_revision),
        )

    def resolve_capture(
        self, identity, action, *, expected_revision, corrected=None, target_id=None
    ):
        return self._mutate(
            'resolve-capture',
            lambda: self._repository.resolve_capture(
                identity,
                action,
                expected_revision=expected_revision,
                corrected=corrected,
                target_id=target_id,
            ),
        )

    def mutate_selection(
        self, profile, identities, action, *, expected_revision, expected_loadout_revision
    ):
        return self._mutate(
            'selection',
            lambda: self._repository.mutate_selection(
                profile,
                identities,
                action,
                expected_revision=expected_revision,
                expected_loadout_revision=expected_loadout_revision,
            ),
        )
