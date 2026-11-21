# 将识别结果关联并提交声骸记录。
"""Explicit, fresh-frame collection and conservative OCR metadata extraction."""

from dataclasses import dataclass, field, replace
import math
import threading
import time


@dataclass(frozen=True)
class CaptureStatus:
    state: str = 'stopped'
    message: str = '未开启收录；浏览声骸不会写入库存'
    added: int = 0
    pending: int = 0
    profile_id: str = ''
    mode: str = 'manual'
    needs_review: int = 0
    duplicates: int = 0
    upgrades: int = 0


@dataclass(frozen=True)
class SingleCaptureRequest:
    profile_id: str
    created_at: float
    deadline: float
    after_token: object
    weights: object = None
    sonata: object = None
    source_identity: object = None
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)


class CaptureSession:
    def __init__(
        self,
        repository,
        evidence,
        *,
        manual_stable_frames=2,
        manual_stable_seconds=0.3,
        max_frame_gap=5.0,
        clock=time.monotonic,
        quality=None,
        cancel_event=None,
        job_factory=None,
    ):
        if type(manual_stable_frames) is not int or manual_stable_frames < 2:
            raise ValueError('Manual capture requires multiple fresh frames')
        if (
            isinstance(manual_stable_seconds, bool)
            or not math.isfinite(manual_stable_seconds)
            or manual_stable_seconds < 0
        ):
            raise ValueError('Manual capture requires a finite duration')
        if (
            isinstance(max_frame_gap, bool)
            or not math.isfinite(max_frame_gap)
            or max_frame_gap <= 0
        ):
            raise ValueError('Capture requires a finite and positive frame gap')
        self.repository, self.evidence = repository, evidence
        self.clock = clock
        self.manual_stable_frames = manual_stable_frames
        self.manual_stable_seconds = manual_stable_seconds
        self.max_frame_gap = max_frame_gap
        self._quality = quality
        self._cancel_event = cancel_event
        self._job_factory = job_factory
        self._lock = threading.RLock()
        self._status = CaptureStatus()
        self._reset_diagnostics()
        self._single_request = None
        self._manual_frozen = None
        self._manual_job = None
        self._manual_armed = False
        self._shortcut_owner = None

    def _reset_diagnostics(self):
        self._diagnostics = dict(frames=0, records=0)

    @property
    def status(self):
        with self._lock:
            # UI polling must also expire a one-shot request while the host
            # executor is paused/blocked and therefore not calling run().
            self.expire_once()
            return self._status

    @property
    def single_request(self):
        with self._lock:
            return self._single_request

    def shortcut_snapshot(self):
        # The key reader must not wait behind a pending inventory commit.
        # A mismatched snapshot is rejected by identity on the GUI thread.
        owner, status = self._shortcut_owner, self._status
        return (None if status.state == 'stopped' else owner), status

    def arm_manual(self, profile_id):
        """Enable shortcut-only intake without observing or saving any frame."""
        with self._lock:
            if self._status.state != 'stopped':
                raise ValueError('请先结束当前收录')
            if profile_id not in self.repository.profiles():
                raise ValueError('请先选择有效本地档案')
            self._reset_diagnostics()
            self._manual_armed = True
            self._shortcut_owner = object()
            self._status = CaptureStatus(
                'armed',
                '等待收录快捷键；浏览声骸不会自动保存',
                profile_id=profile_id,
                mode='manual',
            )

    def request_once(
        self,
        profile_id,
        *,
        after_token=None,
        weights=None,
        sonata=None,
        timeout=20,
        source_identity=None,
        expected_status=None,
    ):
        with self._lock:
            if expected_status is not None and self._status is not expected_status:
                return False
            if self._single_request is not None:
                return False
            if self._status.state not in ('stopped', 'armed', 'single'):
                return False
            if self._manual_armed and profile_id != self._status.profile_id:
                raise ValueError('档案已变，请先停止再开启收录')
            if profile_id not in self.repository.profiles():
                raise ValueError('请先选择有效本地档案')
            now = self.clock()
            if (
                not math.isfinite(now)
                or type(timeout) not in (int, float)
                or not math.isfinite(timeout)
                or not 1 <= timeout <= 60
            ):
                raise ValueError('Invalid single capture time window')
            self._reset_diagnostics()
            if not self._manual_armed:
                self._shortcut_owner = object()
            self._single_request = SingleCaptureRequest(
                profile_id,
                now,
                now + timeout,
                after_token,
                weights,
                sonata,
                source_identity,
            )
            self._status = (
                replace(self._status, state='single', message='正在收录当前声骸')
                if self._manual_armed
                else CaptureStatus(
                    'single', '正在收录当前声骸', profile_id=profile_id, mode='manual'
                )
            )
            if self._job_factory is None:
                raise ValueError('手动收录缺少诊断作业工厂')
            self._manual_job = self._job_factory(
                'manual-capture', metadata={'weight_version': getattr(weights, 'version', '')}
            )
            self._manual_job.stage('waiting_frame', {'timeout_seconds': timeout})
            return True

    def expire_once(self, request=None):
        with self._lock:
            if request is not None and self._single_request is not request:
                return False
            if self._single_request is not None and self.clock() >= self._single_request.deadline:
                counts = self._diagnostics
                reason = (
                    '冻结画面识别超时，请稍后重试'
                    if self._manual_job is not None
                    and self._manual_job.current_stage
                    in ('recognizing', 'capturing_evidence', 'committing')
                    else '未获取到新的游戏画面，请检查 OKWW 截图连接'
                    if not counts['frames']
                    else '稳定画面数量不足，请保持选中声骸后重试'
                )
                self.cancel_once(
                    '画面未稳定：' + reason,
                    diagnostic_state='failed',
                    summary_code='capture_unstable',
                )
                return True
            return False

    def mark_manual_frozen(self, request):
        with self._lock:
            if self._single_request is not request or self.expire_once(request):
                return False
            self._manual_frozen = request
            return True

    def is_manual_frozen(self, request):
        with self._lock:
            return request is not None and self._manual_frozen is request

    def _clear_manual_request(self, *, cancel):
        request, self._single_request = self._single_request, None
        self._manual_frozen = None
        if cancel and request is not None:
            request.cancelled.set()

    def _finish_manual(self, state, summary_code):
        if self._manual_job is not None:
            counts = dict(
                self._diagnostics,
                added=self._status.added,
                pending=self._status.pending,
                needs_review=self._status.needs_review,
                duplicates=self._status.duplicates,
                upgrades=self._status.upgrades,
            )
            self._manual_job.finish(state, summary_code, counters=counts)
            self._manual_job = None

    def cancel_once(
        self,
        message='本次收录已取消',
        *,
        diagnostic_state='cancelled',
        summary_code='capture_cancelled',
        request=None,
    ):
        with self._lock:
            if self._single_request is not None and (
                request is None or self._single_request is request
            ):
                # Bounded counters only; no item names, attributes, images or
                # local profile identifiers are written into application logs.
                self._finish_manual(diagnostic_state, summary_code)
                self._clear_manual_request(cancel=True)
                self._status = replace(
                    self._status,
                    state='armed' if self._manual_armed else 'stopped',
                    message=message,
                )
                self._reset_diagnostics()

    def fail_manual(
        self,
        stage,
        error,
        *,
        request=None,
        message='后台收录解析失败，请重试',
        summary_code='capture_failed',
    ):
        """Close one failed manual request so the shortcut is immediately usable."""
        with self._lock:
            if (
                self._single_request is None
                or self._manual_job is None
                or (request is not None and self._single_request is not request)
            ):
                return False
            # Preserve the real traceback in the bounded diagnostic job; never
            # report a normal cancellation as a failure here.
            self._manual_job.exception(stage, error, summary_code)
            self._finish_manual('failed', summary_code)
            self._clear_manual_request(cancel=True)
            self._status = replace(
                self._status,
                state='armed' if self._manual_armed else 'stopped',
                message=message,
            )
            self._reset_diagnostics()
            return True

    def start_scan(self, profile_id, generation):
        with self._lock:
            if self._status.state != 'stopped':
                raise ValueError('请先结束当前收录')
            if profile_id not in self.repository.profiles():
                raise ValueError('请先选择有效本地档案')
            self._scan_generation = generation
            self._scan_started_at = self.clock()
            self._shortcut_owner = object()
            self._status = CaptureStatus(
                'active', '准备自动扫描', profile_id=profile_id, mode='automatic'
            )

    def _matches_scan(self, generation):
        return self._status.mode == 'automatic' and self._scan_generation == generation

    def scan_status(self, generation):
        with self._lock:
            return self._status if self._matches_scan(generation) else None

    def pause(self, *, generation=None):
        with self._lock:
            if generation is not None and not self._matches_scan(generation):
                return
            if self._single_request is not None:
                self.cancel_once('游戏画面不可用，本次收录已取消')
                return
            if self._status.state == 'active':
                self._status = replace(self._status, state='paused', message='收录已暂停')

    def resume(self, *, generation=None):
        with self._lock:
            if generation is not None and not self._matches_scan(generation):
                return
            if self._status.state == 'paused':
                self._status = replace(self._status, state='active', message='已继续收录')

    def stop(self, *, generation=None):
        # Wake a native quality check even if it still holds the commit lock.
        if generation is None:
            request = self._single_request
            if request is not None:
                request.cancelled.set()
        with self._lock:
            if generation is not None and not self._matches_scan(generation):
                return
            self._manual_armed = False
            self._shortcut_owner = None
            self._finish_manual('cancelled', 'capture_cancelled')
            self._clear_manual_request(cancel=True)
            self._status = replace(
                self._status, state='stopped', message='收录已停止，已保存记录保留'
            )
            self._reset_diagnostics()

    def _upgrade_candidates(self, record):
        if not record.name or not record.sonata or record.level is None:
            return []
        candidates = []
        for old in self.repository.list(record.profile_id):
            if (
                old.name != record.name
                or old.sonata != record.sonata
                or old.cost != record.cost
                or old.level is None
            ):
                continue
            if tuple(s.stat_name for s in old.main_stats) != tuple(
                s.stat_name for s in record.main_stats
            ):
                continue
            old_sub = {(s.stat_name, s.value) for s in old.sub_stats}
            new_sub = {(s.stat_name, s.value) for s in record.sub_stats}
            if (
                record.level >= old.level
                and old_sub <= new_sub
                and (record.level > old.level or old_sub < new_sub)
            ):
                candidates.append(old.item_id)
        return candidates

    def single_progress(self, request, stage, message, **counters):
        with self._lock:
            if self._single_request is not request or self.expire_once():
                return False
            self._diagnostics.update({k: v for k, v in counters.items() if k in self._diagnostics})
            self._status = replace(self._status, message=message)
            if self._manual_job is not None and self._manual_job.current_stage != stage:
                self._manual_job.stage(stage)
            return True

    def commit_single_snapshot(self, record, image, proof, *, request, commit_guard):
        from ww_echo_workshop.capture.manual_snapshot import record_fields

        with self._lock:
            if (
                self._single_request is not request
                or self._manual_frozen is not request
                or self.expire_once(request)
                or self._status.state != 'single'
                or record.profile_id != request.profile_id
                or not proof.valid(
                    request,
                    stable_frames=self.manual_stable_frames,
                    stable_seconds=self.manual_stable_seconds,
                    max_frame_gap=self.max_frame_gap,
                )
                or proof.fields != record_fields(record)
                or not commit_guard()
            ):
                return False
            self._diagnostics.update(frames=len(proof.tokens), records=1)
            return bool(
                self._store_record(record, image, commit_guard=commit_guard, request=request)
            )

    def commit_scan_snapshot(
        self, record, image, proof, *, generation, commit_guard, cancel_event=None
    ):
        """Commit a verified snapshot and return 'added', 'pending', or None.

        Queued positions differ from the current clicked tile. The traversal's
        submitted-position set and generation guard authorize each exactly once.
        """
        with self._lock:
            if (
                self._status.mode != 'automatic'
                or self._status.state != 'active'
                or generation != self._scan_generation
                or proof.generation != generation
                or not proof.valid()
                or proof.times[0] < self._scan_started_at
                or record.profile_id != self._status.profile_id
                or record.level != proof.level
                or not commit_guard()
            ):
                return None
            return self._store_record(
                record, image, commit_guard=commit_guard, cancel_event=cancel_event
            )

    def _store_record(self, record, image, *, commit_guard, cancel_event=None, request=None):
        with self.evidence.operation():
            return self._store_with_evidence(record, image, commit_guard, cancel_event, request)

    def _store_with_evidence(self, record, image, commit_guard, cancel_event, request):
        # Shared persistence path; callers validate their respective proofs.
        if request is not None and self._manual_job is not None:
            self._manual_job.stage('committing')
        if self._quality is None:
            raise ValueError('收录缺少原生质量服务')
        # Remove warnings already resolved by catalog/icon/field resolution
        # before the final native quality decision.
        from ww_echo_workshop.capture.metadata import settle_observation_issues

        record = replace(
            record,
            issues=tuple(dict.fromkeys(settle_observation_issues(record, record.issues))),
        )
        if request is not None:
            cancel_event = request.cancelled
        elif cancel_event is None:
            cancel_event = self._cancel_event
        facts = self._quality((record,), cancel_event=cancel_event)
        if len(facts) != 1 or facts[0].record is not record:
            raise ValueError('收录质量结论与当前记录不一致')
        if not commit_guard():
            return None
        issues = tuple(facts[0].issues)
        duplicate = [item.item_id for item in self.repository.duplicate_candidates(record)]
        upgraded = self._upgrade_candidates(record) if not duplicate else []
        # Inventory queries can outlast the window, focus or session that
        # authorized them. Recheck before writing either evidence or a row.
        if not commit_guard():
            return None
        candidates = duplicate or upgraded
        with self.evidence.staged_put(
            record.item_id, image, publish_guard=commit_guard
        ) as reference:
            if reference is None:
                return None
            record = replace(
                record,
                screenshot_ref=reference,
                review='pending' if candidates or issues else 'confirmed',
            )
            if candidates:
                reason = 'duplicate' if duplicate else 'upgrade'
                self.repository.stage_capture(record, reason, candidates)
            else:
                self.repository.add(record)

        if candidates:
            message = (
                '疑似重复：待确认区可选择“另一件”'
                if duplicate
                else '疑似升级：请明确选择更新目标或另存'
            )
            self._status = replace(
                self._status,
                pending=self._status.pending + 1,
                needs_review=self._status.needs_review + 1,
                duplicates=self._status.duplicates + bool(duplicate),
                upgrades=self._status.upgrades + bool(upgraded),
                message=message,
            )
        else:
            message = '已收录' if not issues else '已保存，请核对识别有疑问的字段'
            self._status = replace(
                self._status,
                added=self._status.added + 1,
                needs_review=self._status.needs_review + bool(issues),
                message=message,
            )
        if request is not None:
            self._finish_manual(
                'completed', 'capture_pending' if duplicate or upgraded else 'capture_completed'
            )
            self._clear_manual_request(cancel=False)
            self._status = replace(
                self._status,
                state='armed' if self._manual_armed else 'stopped',
                message=self._status.message
                + ('；等待下次收录快捷键' if self._manual_armed else ''),
            )
        return 'pending' if candidates else 'added'
