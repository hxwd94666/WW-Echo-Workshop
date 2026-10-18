# 记录关键作业阶段和有界诊断信息。
"""Bounded operation summaries; stdlib host logging, no background file writer.

Only aggregate numbers and a small metadata allowlist enter exports. Exceptions
keep their real traceback in the host log, never in a portable diagnostic file.
The state is owned by one canonical application module per process.
"""

from collections import deque
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4


MAX_JOBS = 32
MAX_EVENTS = 32
MAX_EXPORT_BYTES = 128 * 1024
_TOKEN = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')
_VERSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+@:-]{0,95}$')
# OKWW's file/queue handlers belong to "ok", not the root logger. A child
# inherits that existing sink without importing the host or adding handlers.
_LOGGER = logging.getLogger('ok.Echo Workshop.Jobs')
_STATE = SimpleNamespace(lock=threading.RLock(), jobs=deque(maxlen=MAX_JOBS))

_MESSAGES = {
    'completed': '作业已完成',
    'cancelled': '作业已取消；已提交的数据保留',
    'failed': '作业未完成；请结合阶段和错误类型检查宿主日志',
    'scan_completed': '扫描已完成',
    'scan_threshold': '已到最低等级边界，扫描完成',
    'scan_cancelled': '扫描已停止；已收录声骸保留',
    'scan_failed': '扫描中断；已收录声骸保留，请检查宿主日志',
    'scan_start_failed': '扫描未启动；请将游戏切至前台并打开声骸背包',
    'scan_not_top': '请将声骸背包拉至顶部，再开始扫描',
    'scan_parse_failed': '声骸解析失败；其他已收录数据保留',
    'scan_levels_failed': '本批等级块识别失败；请检查截图方式与画面',
    'backup_completed': '数据作业已完成',
    'backup_cancelled': '数据作业已取消；原文件和已提交数据保留',
    'backup_failed': '数据作业未完成；请检查文件或宿主日志',
    'comparison_completed': '评分已完成',
    'comparison_cancelled': '评分已取消，原结果保留',
    'comparison_failed': '评分未完成；请检查角色要求、数据与宿主日志',
    'capture_completed': '手动收录已完成',
    'capture_pending': '已保存到待确认区，请明确选择重复或升级处理',
    'capture_cancelled': '本次手动收录已取消；已有记录保留',
    'capture_unstable': '画面未稳定，手动收录未完成；请查看阶段与计数',
    'capture_wrong_page': '请在背包或角色声骸展示页收录',
    'capture_failed': '手动收录遇到错误；请检查宿主日志，已有记录保留',
    'resource_updated': '资源检查已完成',
    'resource_partial': '部分资源检查未完成，保留已有资料',
    'resource_cancelled': '资源检查已取消，保留已有资料',
    'resource_failed': '资源检查未完成，保留已有资料，请检查宿主日志',
}
_METADATA_TOKENS = {
    'plugin_version',
    'weight_version',
    'algorithm_version',
    'version',
    'capture_method',
    'click_method',
    'wheel_method',
    'channel',
    'source',
    'character_id',
    'modal_id',
}
_METADATA_NUMBERS = {'inventory_revision', 'minimum_level', 'capacity'}
_METADATA_FLAGS = {'auto_enabled', 'auto_weight_updates', 'cache_hit', 'fallback'}


def _token(value, fallback='processing'):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else fallback


def _number(value):
    return type(value) in (int, float) and abs(value) <= 10**15 and math.isfinite(value)


def _metadata(values):
    result = {}
    for key, value in values.items() if isinstance(values, dict) else ():
        if key in _METADATA_TOKENS and isinstance(value, str) and _VERSION.fullmatch(value):
            # Reject Windows drive-like strings even when a caller supplies a token field.
            if not re.match(r'^[A-Za-z]:', value):
                result[key] = value
        elif key in _METADATA_NUMBERS and _number(value):
            result[key] = value
        elif key in _METADATA_FLAGS and type(value) is bool:
            result[key] = value
        elif key == 'resolution' and isinstance(value, (tuple, list)) and len(value) == 2:
            if all(type(v) is int and 0 < v <= 32768 for v in value):
                result[key] = list(value)
    return result


def _counters(values):
    return (
        {
            key: value
            for key, value in list(values.items())[:32]
            if isinstance(key, str) and _TOKEN.fullmatch(key) and _number(value)
        }
        if isinstance(values, dict)
        else {}
    )


def _log(level, message, *, error=None):
    try:
        info = (type(error), error, error.__traceback__) if error is not None else None
        _LOGGER.log(level, message, exc_info=info)
    except Exception:
        # Diagnostics must never change a score, commit, capture or cancellation.
        pass


class DiagnosticJob:
    def __init__(self, record):
        self._record = record
        self.operation_id = record['operation_id']
        self._clock = time.monotonic
        self._began = self._phase_at = self._clock()
        self._last_logs = {}
        self._terminal = False

    def _elapsed(self):
        return round(max(0.0, self._clock() - self._began), 3)

    @property
    def current_stage(self):
        with _STATE.lock:
            return self._record['stage']

    def _event(self, event):
        self._record['events'].append(
            dict(event=event, stage=self._record['stage'], elapsed_seconds=self._elapsed())
        )

    def count(self, name, amount=1):
        name = _token(name, '')
        if not name or not _number(amount):
            return
        with _STATE.lock:
            values = self._record['counters']
            if not self._terminal and (name in values or len(values) < 32):
                values[name] = values.get(name, 0) + amount

    def maximum(self, name, value):
        name = _token(name, '')
        if not name or not _number(value):
            return
        with _STATE.lock:
            values = self._record['counters']
            if not self._terminal and (name in values or len(values) < 32):
                values[name] = max(values.get(name, value), value)

    def stage(self, stage, counters=None):
        stage = _token(stage)
        with _STATE.lock:
            if self._terminal:
                return
            self._record['counters'].update(_counters(counters))
            self._record['counters'] = dict(list(self._record['counters'].items())[:32])
            if stage == self._record['stage']:
                return
            self._end_phase()
            self._record['stage'] = stage
            self._event('stage')
            # Stage updates remain in memory. At most one stage log per second.
            now = self._clock()
            if now - self._last_logs.get('stage', -math.inf) >= 1.0:
                self._last_logs['stage'] = now
                _log(
                    logging.INFO,
                    f'echo.job.stage op={self.operation_id} kind={self._record["kind"]} stage={stage}',
                )

    def _end_phase(self):
        now = self._clock()
        values = self._record['stage_seconds']
        key = self._record['stage']
        if key in values or len(values) < 32:
            values[key] = round(values.get(key, 0.0) + max(0.0, now - self._phase_at), 3)
        self._phase_at = now

    def metadata(self, values):
        with _STATE.lock:
            if not self._terminal:
                self._record['metadata'].update(_metadata(values))

    def part(self, name, state='completed', metadata=None, counters=None):
        """Small per-resource summaries; unlike metadata(), previous channels remain."""
        name = _token(name, '')
        if not name:
            return
        state = (
            state
            if state in ('running', 'completed', 'cached', 'skipped', 'failed', 'cancelled')
            else 'failed'
        )
        with _STATE.lock:
            if self._terminal:
                return
            values = self._record.setdefault('parts', {})
            if name in values or len(values) < 16:
                values[name] = dict(
                    state=state, metadata=_metadata(metadata), counters=_counters(counters)
                )

    def performance(self, values):
        """Keep only the existing scanner's bounded numeric timing aggregates."""
        if not isinstance(values, dict):
            return
        metrics = {
            'calls',
            'sample_count',
            'total_ms',
            'first_call_ms',
            'median_ms',
            'p95_ms',
            'max_ms',
        }
        stages = {}
        source = values.get('stages', {})
        for name, row in list(source.items())[:24] if isinstance(source, dict) else ():
            if isinstance(name, str) and _TOKEN.fullmatch(name) and isinstance(row, dict):
                stages[name] = {
                    key: val for key, val in row.items() if key in metrics and _number(val)
                }
        with _STATE.lock:
            if not self._terminal:
                self._record['performance'] = dict(
                    stages=stages,
                    counters=_counters(values.get('counters')),
                    stage_totals_are_overlapping=True,
                )

    def exception(self, stage, error, summary_code=None):
        """Return true when logged; repeated identical errors are counted, not spammed."""
        stage = _token(stage)
        error_type = _token(type(error).__name__, 'Exception')
        code = summary_code if summary_code in _MESSAGES else 'failed'
        with _STATE.lock:
            if self._terminal:
                return False
            self.count('errors')
            self._record['error_type'] = error_type
            self._record['error_stage'] = stage
            self._record['summary'] = _MESSAGES[code]
            key = (stage, error_type, code)
            now = self._clock()
            if now - self._last_logs.get(key, -math.inf) < 2.0:
                self.count('suppressed_errors')
                return False
            if len(self._last_logs) >= 32:
                self._last_logs.clear()
            self._last_logs[key] = now
            self._event('error')
        _log(
            logging.ERROR,
            f'echo.job.error op={self.operation_id} kind={self._record["kind"]} '
            f'stage={stage} error_type={error_type} summary={_MESSAGES[code]}',
            error=error,
        )
        return True

    def finish(self, state='completed', summary_code=None, counters=None, error=None):
        state = state if state in ('completed', 'cancelled', 'failed') else 'failed'
        if error is not None:
            self.exception(self._record['stage'], error, summary_code)
        with _STATE.lock:
            if self._terminal:
                return
            self._end_phase()
            self._record['counters'].update(_counters(counters))
            self._record['counters'] = dict(list(self._record['counters'].items())[:32])
            self._record.update(
                state=state,
                elapsed_seconds=self._elapsed(),
                summary=_MESSAGES.get(summary_code, _MESSAGES[state]),
            )
            self._event('finished')
            self._terminal = True
            count_text = json.dumps(
                self._record['counters'], ensure_ascii=True, separators=(',', ':')
            )
        _log(
            logging.INFO,
            f'echo.job.finished op={self.operation_id} kind={self._record["kind"]} '
            f'state={state} stage={self._record["stage"]} elapsed_seconds={self._record["elapsed_seconds"]} '
            f'counters={count_text} summary={self._record["summary"]}',
        )


def begin_job(kind, metadata=None):
    record = dict(
        operation_id='op-' + uuid4().hex[:12],
        kind=_token(kind, 'operation'),
        state='running',
        stage='starting',
        started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        elapsed_seconds=None,
        summary='作业进行中',
        counters={},
        metadata=_metadata(metadata),
        stage_seconds={},
        events=deque(maxlen=MAX_EVENTS),
    )
    with _STATE.lock:
        _STATE.jobs.append(record)
    job = DiagnosticJob(record)
    with _STATE.lock:
        job._event('started')
    _log(logging.INFO, f'echo.job.started op={job.operation_id} kind={record["kind"]}')
    return job


def snapshot(context=None):
    """No records, screenshots, OCR, paths, names, accounts or exception messages."""
    with _STATE.lock:
        jobs = [dict(record, events=list(record['events'])) for record in _STATE.jobs]
        # Copy nested counters while holding the lock; consumers get detached data.
        jobs = json.loads(json.dumps(jobs, ensure_ascii=False, allow_nan=False))
    payload = dict(
        schema='echo-diagnostics/1',
        generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        context=_metadata(context),
        jobs=jobs,
        omitted_jobs=0,
        privacy='仅作业阶段、数量、耗时与错误类型；不含库存、截图、OCR、账号、路径或真实栈',
    )
    while (
        len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        > MAX_EXPORT_BYTES
        and payload['jobs']
    ):
        payload['jobs'].pop(0)
        payload['omitted_jobs'] += 1
    return payload


def export_diagnostics(path, context=None):
    """Write one small atomic JSON on explicit request. Does not export host logs."""
    path = Path(path)
    data = json.dumps(snapshot(context), ensure_ascii=False, allow_nan=False, indent=2).encode(
        'utf-8'
    )
    # Pretty formatting may exceed the compact budget. Keep the same limit.
    if len(data) > MAX_EXPORT_BYTES:
        data = json.dumps(
            snapshot(context), ensure_ascii=False, allow_nan=False, separators=(',', ':')
        ).encode('utf-8')
    fd, temporary = tempfile.mkstemp(prefix='.echo-diagnostics-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
