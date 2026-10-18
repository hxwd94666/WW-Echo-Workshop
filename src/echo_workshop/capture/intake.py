# 将识别结果关联并提交声骸记录。
"""Explicit, fresh-frame collection and conservative OCR metadata extraction."""

from dataclasses import dataclass, replace
import math
import threading
import time

from echo_workshop.application.diagnostics import begin_job


@dataclass(frozen=True)
class CaptureStatus:
    state: str = 'stopped'
    message: str = '未开启收录；浏览声骸不会写入库存'
    added: int = 0
    pending: int = 0
    profile_id: str = ''
    marked: bool = False
    mode: str = 'continuous'
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
    background_identity: object = None
    source_identity: object = None


class CaptureSession:
    def __init__(
        self,
        repository,
        evidence,
        *,
        stable_frames=3,
        stable_seconds=0.6,
        max_frame_gap=5.0,
        clock=time.monotonic,
    ):
        if type(stable_frames) is not int or stable_frames < 2:
            raise ValueError('Capture requires multiple fresh frames')
        if (
            isinstance(stable_seconds, bool)
            or not math.isfinite(stable_seconds)
            or stable_seconds < 0
            or isinstance(max_frame_gap, bool)
            or not math.isfinite(max_frame_gap)
            or max_frame_gap <= 0
        ):
            raise ValueError('Capture requires finite durations and a positive frame gap')
        self.repository, self.evidence = repository, evidence
        self.stable_frames, self.stable_seconds, self.clock = stable_frames, stable_seconds, clock
        self.max_frame_gap = max_frame_gap
        self._lock = threading.RLock()
        self._status = CaptureStatus()
        self._reset_detection()
        self._single_request = None
        self._manual_job = None
        self._manual_armed = False

    def _reset_detection(self):
        self._token = None
        self._reset_stability()
        self._committed = None
        self._missing = 0
        self._diagnostics = dict(frames=0, records=0, changes=0, gaps=0, interruptions=0)

    def _reset_stability(self):
        self._key = None
        self._count = 0
        self._since = 0
        self._last_observed = None

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

    def arm_manual(self, profile_id):
        """Enable shortcut-only intake without observing or saving any frame."""
        with self._lock:
            if self._status.state != 'stopped':
                raise ValueError('请先结束当前收录')
            if profile_id not in self.repository.profiles():
                raise ValueError('请先选择有效本地档案')
            self._reset_detection()
            self._manual_armed = True
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
        background_identity=None,
        source_identity=None,
    ):
        with self._lock:
            if self._status.state not in ('stopped', 'armed'):
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
            self._reset_detection()
            self._single_request = SingleCaptureRequest(
                profile_id,
                now,
                now + timeout,
                after_token,
                weights,
                sonata,
                background_identity,
                source_identity,
            )
            self._status = (
                replace(self._status, state='single', message='正在收录当前声骸')
                if self._manual_armed
                else CaptureStatus(
                    'single', '正在收录当前声骸', profile_id=profile_id, mode='manual'
                )
            )
            self._manual_job = begin_job(
                'manual-capture', metadata={'weight_version': getattr(weights, 'version', '')}
            )
            self._manual_job.stage('waiting_frame', {'timeout_seconds': timeout})
            return True

    def expire_once(self):
        with self._lock:
            if self._single_request is not None and self.clock() >= self._single_request.deadline:
                counts = self._diagnostics
                reason = (
                    '冻结画面识别超时，请稍后重试'
                    if self._manual_job is not None
                    and self._manual_job.current_stage == 'recognizing'
                    else '未获取到新的游戏画面，请检查 OKWW 截图连接'
                    if not counts['frames']
                    else '词条未识别，请在背包或展示页重试'
                    if not counts['records']
                    else '识别间隔过长，请暂停其他耗时任务后重试'
                    if counts['gaps']
                    else '识别结果变化，请保持选中声骸后重试'
                    if counts['changes']
                    else '稳定画面数量不足，请保持选中声骸后重试'
                )
                self.cancel_once(
                    '画面未稳定：' + reason,
                    diagnostic_state='failed',
                    summary_code='capture_unstable',
                )
                return True
            return False

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
    ):
        with self._lock:
            if self._single_request is not None:
                # Bounded counters only; no item names, attributes, images or
                # local profile identifiers are written into application logs.
                self._finish_manual(diagnostic_state, summary_code)
                self._single_request = None
                self._status = replace(
                    self._status,
                    state='armed' if self._manual_armed else 'stopped',
                    message=message,
                )
                self._reset_detection()

    def start(self, profile_id):
        with self._lock:
            if self._status.mode == 'automatic' and self._status.state != 'stopped':
                raise ValueError('请先停止自动扫描')
            if self._single_request is not None:
                raise ValueError('正在收录当前声骸，请等待或停止')
            if profile_id not in self.repository.profiles():
                raise ValueError('Choose an existing local profile')
            self._reset_detection()
            self._manual_armed = False
            self._last_action = None
            self._status = CaptureStatus(
                'active', '收录中：请逐个打开声骸并稍作停留', profile_id=profile_id
            )

    def start_scan(self, profile_id, generation):
        with self._lock:
            if self._status.state != 'stopped':
                raise ValueError('请先结束当前收录')
            self.start(profile_id)
            self._scan_generation = generation
            self._scan_position = None
            self._scan_started_at = self.clock()
            self._status = replace(self._status, mode='automatic', message='准备自动扫描')

    def rearm_scan(self, position, generation, after_token):
        with self._lock:
            if (
                self._status.mode != 'automatic'
                or self._status.state != 'active'
                or generation != self._scan_generation
            ):
                return False
            if position != self._scan_position:
                self._committed = None
                self._reset_stability()
                self._scan_position = position
            self._scan_after_token = after_token
            self._scan_created_at = self.clock()
            return True

    def pause(self):
        with self._lock:
            if self._single_request is not None:
                self.cancel_once('游戏画面不可用，本次收录已取消')
                return
            if self._status.state == 'active':
                self._status = replace(self._status, state='paused', message='收录已暂停')
                self._reset_stability()

    def resume(self):
        with self._lock:
            if self._status.state == 'paused':
                self._status = replace(self._status, state='active', message='已继续收录')

    def stop(self):
        with self._lock:
            self._manual_armed = False
            self._finish_manual('cancelled', 'capture_cancelled')
            self._single_request = None
            self._status = replace(
                self._status, state='stopped', message='收录已停止，已保存记录保留'
            )
            self._reset_detection()

    def interrupt(self):
        """Break a pending run without treating a capture error as page exit."""
        with self._lock:
            if self._single_request is not None:
                self._diagnostics['interruptions'] += 1
            self._reset_stability()
            self._missing = 0

    def mark_current(self):
        with self._lock:
            self._status = replace(
                self._status, marked=True, message='当前项已标记，下次稳定识别将带备注'
            )
            # Rearm only by an explicit user gesture, not OCR oscillation.
            self._committed = None
            self._reset_stability()

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

    def observe(
        self,
        record,
        frame_token,
        png_supplier,
        *,
        context=(),
        captured_at=None,
        request=None,
        scan_position=None,
        scan_generation=None,
        before_commit=None,
        commit_guard=None,
    ):
        try:
            return self._observe(
                record,
                frame_token,
                png_supplier,
                context=context,
                captured_at=captured_at,
                request=request,
                scan_position=scan_position,
                scan_generation=scan_generation,
                before_commit=before_commit,
                commit_guard=commit_guard,
            )
        except Exception as error:
            with self._lock:
                if self._single_request is not None and self._manual_job is not None:
                    self._manual_job.exception(
                        self._manual_job.current_stage, error, 'capture_failed'
                    )
            raise

    def _observe(
        self,
        record,
        frame_token,
        png_supplier,
        *,
        context=(),
        captured_at=None,
        request=None,
        scan_position=None,
        scan_generation=None,
        before_commit=None,
        commit_guard=None,
    ):
        with self._lock:
            if request is not None and self._single_request is not request:
                return False
            if self._status.state not in ('active', 'single'):
                return False
            # All production callers provide scene context. Legacy continuous
            # intake must not write enhancement details, while scoring remains
            # independent of this persistence gate.
            if context and context[0] not in ('bag', 'display'):
                self._reset_stability()
                if self._single_request is not None:
                    self.cancel_once(
                        '请在背包或角色声骸展示页收录',
                        diagnostic_state='failed',
                        summary_code='capture_wrong_page',
                    )
                else:
                    self._status = replace(self._status, message='请在背包或角色声骸展示页收录')
                return False
            if self._status.mode == 'automatic':
                if (
                    scan_generation != self._scan_generation
                    or scan_position is None
                    or scan_position != self._scan_position
                    or not context
                    or context[0] != 'bag'
                    or type(captured_at) not in (int, float)
                    or not math.isfinite(captured_at)
                    or captured_at < self._scan_created_at
                    or frame_token == self._scan_after_token
                ):
                    return False
            if self._single_request is not None:
                request = self._single_request
                if self.expire_once():
                    return False
                if (
                    type(captured_at) not in (int, float)
                    or not math.isfinite(captured_at)
                    or captured_at < request.created_at
                    or frame_token == request.after_token
                ):
                    return False
                if record is not None and (not context or context[0] not in ('bag', 'display')):
                    self.cancel_once(
                        '请在背包或角色声骸展示页收录',
                        diagnostic_state='failed',
                        summary_code='capture_wrong_page',
                    )
                    return False
            if frame_token is None:
                self.interrupt()
                return False
            if frame_token == self._token:
                return False
            self._token = frame_token
            if self._single_request is not None:
                self._diagnostics['frames'] += 1
            if record is None:
                self._missing += 1
                self._reset_stability()
                # A genuine page exit rearms, one dropped OCR frame does not.
                if self._missing >= 2:
                    self._committed = None
                return False
            if record.profile_id != self._status.profile_id:
                self.interrupt()
                raise ValueError('Captured record belongs to a different profile')
            self._missing = 0
            if self._single_request is not None:
                self._diagnostics['records'] += 1
            game_flags = (record.game_equipped,)
            committed_key = (record.fingerprint, record.validation_issues, game_flags)
            if committed_key == self._committed:
                # Returning to A interrupts B's run even when A was already
                # saved. Otherwise B/A/B/A/B incorrectly adds three B frames.
                self._reset_stability()
                return False
            now = self.clock()
            if not math.isfinite(now):
                self._reset_stability()
                raise ValueError('Capture clock must be finite')
            # The duplicate fingerprint deliberately ignores owner and substat
            # order. Stability must not: the final evidence/owner/order need to
            # describe one uninterrupted observation, not a sorted mixture.
            key = (
                committed_key,
                record.owner,
                record.owner_state,
                record.owner_character_id,
                tuple((s.stat_name, s.value) for s in record.main_stats),
                tuple((s.stat_name, s.value) for s in record.sub_stats),
                context,
            )
            if self._last_observed is not None and (
                now < self._last_observed or now - self._last_observed > self.max_frame_gap
            ):
                if self._single_request is not None:
                    self._diagnostics['gaps'] += 1
                self._reset_stability()
            self._last_observed = now
            if key != self._key:
                if self._single_request is not None and self._key is not None:
                    self._diagnostics['changes'] += 1
                self._key, self._count, self._since = key, 1, now
                if self._single_request is not None:
                    self._status = replace(
                        self._status, message=f'正在核对词条（1/{self.stable_frames}）'
                    )
                return False
            self._count += 1
            if self._single_request is not None:
                self._status = replace(
                    self._status,
                    message=f'正在核对词条（{min(self._count, self.stable_frames)}/{self.stable_frames}）',
                )
            if self._count < self.stable_frames or now - self._since < self.stable_seconds:
                return False
            if before_commit is not None and not before_commit():
                return False
            # Screenshots and OCR came from the same frame. Encode/save only
            # once stability is reached; a failed write does not mark committed.
            if self._single_request is not None and self._manual_job is not None:
                self._manual_job.stage('capturing_evidence')
            image = png_supplier()
            if self._single_request is not None and self.expire_once():
                return False
            if commit_guard is not None and not commit_guard():
                return False
            return self._store_record(record, image)

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
        from echo_workshop.capture.manual_snapshot import record_fields

        with self._lock:
            if (
                self._single_request is not request
                or self.expire_once()
                or self._status.state != 'single'
                or record.profile_id != request.profile_id
                or not proof.valid(
                    request,
                    stable_frames=self.stable_frames,
                    stable_seconds=self.stable_seconds,
                    max_frame_gap=self.max_frame_gap,
                )
                or proof.fields != record_fields(record)
                or not commit_guard()
            ):
                return False
            self._diagnostics.update(frames=len(proof.tokens), records=1)
            return self._store_record(record, image)

    def commit_scan_snapshot(self, record, image, proof, *, generation, commit_guard):
        """Single OCR of a visually verified immutable bag snapshot, task thread only.

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
                return False
            return self._store_record(record, image)

    def _store_record(self, record, image):
        with self.evidence.operation():
            return self._store_with_evidence(record, image)

    def _store_with_evidence(self, record, image):
        # Shared persistence path: manual stability and scan snapshot proof differ,
        # but evidence, duplicate/upgrade staging and undo are identical.
        if self._single_request is not None and self._manual_job is not None:
            self._manual_job.stage('committing')
        reference = self.evidence.put(record.item_id, image)
        record = replace(
            record,
            screenshot_ref=reference,
            review='pending',
            note='用户标记' if self._status.marked else record.note,
        )
        duplicate = [item.item_id for item in self.repository.duplicate_candidates(record)]
        upgraded = self._upgrade_candidates(record) if not duplicate else []
        if duplicate or upgraded:
            reason = 'duplicate' if duplicate else 'upgrade'
            self.repository.stage_capture(record, reason, duplicate or upgraded)
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
                marked=False,
            )
        else:
            # Fresh equal observations or a verified frozen multi-frame proof
            # already established this item's visual stability.
            # Only actual missing/conflicting/invalid data require review.
            if not record.validation_issues:
                record = replace(record, review='confirmed')
            self.repository.add(record)
            message = '已收录' if not record.validation_issues else '已保存，请核对识别有疑问的字段'
            self._status = replace(
                self._status,
                added=self._status.added + 1,
                needs_review=self._status.needs_review + (not record.usable),
                message=message,
                marked=False,
            )
        self._committed = (record.fingerprint, record.validation_issues, (record.game_equipped,))
        if self._single_request is not None:
            self._finish_manual(
                'completed', 'capture_pending' if duplicate or upgraded else 'capture_completed'
            )
            self._single_request = None
            self._status = replace(
                self._status,
                state='armed' if self._manual_armed else 'stopped',
                message=self._status.message
                + ('；等待下次收录快捷键' if self._manual_armed else ''),
            )
        return True
