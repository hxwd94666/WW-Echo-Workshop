# 解析冻结帧并协调共享识别锁。
"""One bounded frozen-frame parser. No capture, input or inventory IO here."""

from dataclasses import dataclass
from collections import deque
from contextlib import contextmanager
import hashlib
import math
import queue
import re
import threading
import time

from echo_workshop.capture.viewport import EchoViewport
from echo_workshop.core.cancellation import OperationCancelled

# 包模块只加载一次，解析和等级线程共用同一把识别锁。
OCR_LOCK = threading.RLock()


class ScanTimings:
    """Bounded per-run diagnostics; never contains OCR, paths or item IDs.

    Stage durations overlap between producer/consumer. They are NOT a total
    wall time. Percentiles cover the latest `limit` observations, explicitly
    separate from all-call totals and the first observation.
    """

    def __init__(self, *, limit=4096, clock=time.perf_counter):
        if type(limit) is not int or limit < 1:
            raise ValueError('Positive timing sample limit required')
        self.limit, self.clock = limit, clock
        self._lock, self._stages, self._counters = threading.Lock(), {}, {}

    def record(self, name, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Finite nonnegative duration required')
        with self._lock:
            entry = self._stages.setdefault(
                name,
                dict(
                    count=0, total=0.0, first=seconds, maximum=0.0, samples=deque(maxlen=self.limit)
                ),
            )
            entry['count'] += 1
            entry['total'] += seconds
            entry['maximum'] = max(entry['maximum'], seconds)
            entry['samples'].append(seconds)

    @contextmanager
    def stage(self, name):
        start = self.clock()
        try:
            yield
        finally:
            self.record(name, max(0.0, self.clock() - start))

    def increment(self, name, amount=1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def maximum(self, name, value):
        with self._lock:
            self._counters[name] = max(value, self._counters.get(name, 0))

    def snapshot(self):
        with self._lock:
            stages = {}
            for name, entry in self._stages.items():
                values = sorted(entry['samples'])

                def percentile(p):
                    return round(values[max(0, math.ceil(len(values) * p) - 1)] * 1000, 3)

                stages[name] = dict(
                    calls=entry['count'],
                    sample_count=len(values),
                    total_ms=round(entry['total'] * 1000, 3),
                    first_call_ms=round(entry['first'] * 1000, 3),
                    median_ms=percentile(0.5),
                    p95_ms=percentile(0.95),
                    max_ms=round(entry['maximum'] * 1000, 3),
                )
            return dict(
                schema=1,
                sample_limit=self.limit,
                percentile_scope='latest_samples',
                stages=stages,
                counters=dict(self._counters),
                stage_totals_are_overlapping=True,
            )


def bag_region(frame, viewport, rect, side='right'):
    area = EchoViewport.checked(viewport, frame.shape[1], frame.shape[0])
    dx, dy = area.offset(side)
    x1, y1, x2, y2 = rect
    # Wide captures can have a fractional reference origin. Round the final
    # captured-pixel endpoints, not just the relative offset: array slices and
    # packed image sizes must remain integers at every supported resolution.
    x, y = round(dx + area.reference_width * x1), round(dy + area.height * y1)
    return (x, y, round(dx + area.reference_width * x2) - x, round(dy + area.height * y2) - y)


def detail_signature(frame, viewport):
    # Bind the proof to the final frozen panel, not to equality across frames.
    # Bright particles can still change these masks while the text stays fixed.
    import numpy as np
    import cv2

    digest = hashlib.sha256()
    for rect in (
        (0.685, 0.105, 0.965, 0.15),
        (0.685, 0.175, 0.80, 0.265),
        (0.68, 0.419, 0.985, 0.715),
        (0.685, 0.79, 0.94, 0.86),
        (0.69, 0.32, 0.805, 0.373),
    ):
        x, y, w, h = bag_region(frame, viewport, rect)
        crop = frame[y : y + h, x : x + w, :3]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        mask = gray > 185  # include gold names, ignore dark animated background
        digest.update(np.packbits(mask).tobytes())
    return digest.hexdigest()


def _detail_observation(frame, viewport):
    """Small text/value patches, not the animated model or complete panel.

    Each value row is independent: changing a small digit must not be diluted
    by the whole stat panel. Low background light does not count as text.
    """
    import cv2
    import numpy as np

    rects = ((0.685, 0.105, 0.965, 0.15), (0.685, 0.79, 0.94, 0.86)) + tuple(
        (0.87, 0.419 + i * 0.042, 0.979, 0.419 + (i + 1) * 0.042) for i in range(7)
    )
    result = []
    for rect in rects:
        x, y, w, h = bag_region(frame, viewport, rect)
        crop = frame[y : y + h, x : x + w, :3]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        mask = (gray > 185).astype(np.uint8)
        # Tiny isolated sparkle/dot components are not a text response. This
        # does not alter the OCR image or final signature/evidence in any way.
        _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        keep = stats[:, cv2.CC_STAT_AREA] >= 12
        keep[0] = False
        result.append(keep[labels])
    return result


def _same_detail_observation(before, after):
    import numpy as np

    for left, right in zip(before, after):
        if left.shape != right.shape:
            return False
        changed = int(np.count_nonzero(left != right))
        # Subpixel antialiasing/noise is tolerated per patch, not as a broad
        # whole-panel percentage. Significant text response restarts evidence.
        tolerance = max(6, round(min(int(left.sum()), int(right.sum())) * 0.025))
        if changed > tolerance:
            return False
    return True


def observed_level(boxes):
    """Read explicit +N; join neighboring same-line OCR fragments, never guess N."""
    rows = []
    for box in sorted(boxes, key=lambda b: (b.y, b.x)):
        text = str(box.name).replace('＋', '+').strip()
        center = box.y + box.height / 2
        group = next(
            (row for row in rows if abs(row[0] - center) <= max(row[1], box.height) * 0.35), None
        )
        if group is None:
            rows.append([center, box.height, [(box, text)]])
        else:
            group[2].append((box, text))
    segments = []
    for _, _, parts in rows:
        text, previous = '', None
        for box, value in sorted(parts, key=lambda pair: pair[0].x):
            if previous is not None and box.x - (previous.x + previous.width) > max(
                previous.height, box.height
            ):
                segments.append(text)
                text = ''
            text += value
            previous = box
        segments.append(text)
    levels = [
        int(m.group(1))
        for text in segments
        for m in re.finditer(r'(?<![\d.])\+\s*(\d{1,2})(?![\d.A-Za-z])', text)
    ]
    # Multiple occurrences remain ambiguous, including repeated boxes.
    return levels[0] if len(levels) == 1 and 0 <= levels[0] <= 25 else None


@dataclass(frozen=True)
class ScanProof:
    position: tuple
    generation: int
    level: int
    tokens: tuple
    times: tuple
    signature: str
    level_tokens: tuple = ()
    detail_tokens: tuple = ()
    grade_page: tuple = ()  # (generation, batch, source token), automatic fixed batches only.

    def valid(self):
        if self.grade_page:
            return (
                len(self.grade_page) == 3
                and self.grade_page[0] == self.generation
                and self.grade_page[1] == self.position[0] // 3
                and self.grade_page[2] is not None
                and type(self.level) is int
                and 0 <= self.level <= 25
                and len(self.tokens) == len(self.times) == 2
                and all(t is not None for t in self.tokens)
                and len(set(self.tokens)) == 2
                and self.level_tokens == (self.grade_page[2],)
                and self.detail_tokens == self.tokens
                and all(type(t) in (int, float) and math.isfinite(t) for t in self.times)
                and 0 < self.times[1] - self.times[0] <= 5
                and bool(self.signature)
            )
        return (
            type(self.level) is int
            and 0 <= self.level <= 25
            and len(self.tokens) == len(self.times) == 3
            and all(t is not None for t in self.tokens)
            and len(set(self.tokens)) == 3
            and self.level_tokens == (self.tokens[0], self.tokens[-1])
            and self.detail_tokens == self.tokens[-2:]
            and all(type(t) in (int, float) and math.isfinite(t) for t in self.times)
            and all(0 < b - a <= 5 for a, b in zip(self.times, self.times[1:]))
            and self.times[-1] - self.times[0] >= 0.3 - 1e-6
            and bool(self.signature)
        )


class ScanFrameGate:
    def __init__(self):
        self.reset()

    def reset(self):
        self.key, self.samples, self.level, self.detail = None, [], None, None

    def needs_level(self, token, acquired_at, position, generation, viewport):
        """OCR only the first and final confirming frame, never cached evidence."""
        key = (position, generation, tuple(viewport))
        if (
            key != self.key
            or not self.samples
            or token is None
            or type(acquired_at) not in (int, float)
            or not math.isfinite(acquired_at)
            or not 0 < acquired_at - self.samples[-1][1] <= 5
            or acquired_at - self.samples[0][1] > 5
        ):
            return True
        return len(self.samples) >= 2 and acquired_at - self.samples[0][1] >= 0.3 - 1e-6

    def observe(
        self,
        frame,
        viewport,
        token,
        acquired_at,
        position,
        generation,
        level,
        *,
        level_observed=True,
    ):
        if token is None or type(acquired_at) not in (int, float) or not math.isfinite(acquired_at):
            self.reset()
            return None
        # Selection is separately verified by ScanTraversal. Check local text
        # response without demanding exact equality of the animated panel.
        # All actual fields are still parsed ONCE from the final frozen image.
        key = (position, generation, tuple(viewport))
        if any(token == t for t, _ in self.samples):
            return None
        if key != self.key or (
            self.samples
            and (
                not 0 < acquired_at - self.samples[-1][1] <= 5
                or acquired_at - self.samples[0][1] > 5
            )
        ):
            self.reset()
            self.key = key
        detail = _detail_observation(frame, viewport)
        if self.detail is not None and not _same_detail_observation(self.detail, detail):
            self.reset()
            self.key = key
        if level_observed:
            if type(level) is not int or not 0 <= level <= 25:
                self.reset()
                return None
            if self.level is not None and level != self.level:
                self.reset()
                self.key = key
            self.level = level
        elif not self.samples:
            return None  # A new target requires a real first grade reading.
        self.detail = detail
        self.samples.append((token, acquired_at))
        # Retain the real first-grade frame, not a cached grade attached to a
        # later middle frame. Only the two latest image observations rotate.
        if len(self.samples) > 3:
            self.samples = self.samples[:1] + self.samples[-2:]
        if (
            not level_observed
            or len(self.samples) < 3
            or self.samples[-1][1] - self.samples[0][1] < 0.3 - 1e-6
        ):
            return None
        # A signature binds the proof to THIS image, not three animated frames.
        signature = detail_signature(frame, viewport)
        tokens = tuple(t for t, _ in self.samples)
        proof = ScanProof(
            position,
            generation,
            self.level,
            tokens,
            tuple(t for _, t in self.samples),
            signature,
            (tokens[0], tokens[-1]),
            tokens[-2:],
        )
        return proof if proof.valid() else None


@dataclass(frozen=True)
class ScanSnapshot:
    frame: object
    viewport: tuple
    proof: ScanProof

    @classmethod
    def freeze(cls, frame, viewport, proof):
        if not proof.valid() or detail_signature(frame, viewport) != proof.signature:
            raise ValueError('Unverified scan snapshot')
        frozen = frame.copy()
        frozen.flags.writeable = False
        return cls(frozen, tuple(viewport), proof)


class ScanPipeline:
    """Capacity counts running, waiting AND unconsumed results. Never drop work."""

    def __init__(
        self,
        parse,
        capacity=4,
        *,
        timings=None,
        name='echo-scan-parser',
        diagnostics=None,
        channel='detail',
        cancel_event=None,
    ):
        self.parse, self.capacity = parse, capacity
        self.timings = timings
        self.diagnostics, self.channel = diagnostics, channel
        self.cancelled = cancel_event if cancel_event is not None else threading.Event()
        self._jobs, self._results = queue.Queue(), queue.Queue()
        self._lock = threading.Lock()
        self._count = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def pending(self):
        with self._lock:
            return self._count

    def submit(self, snapshot):
        with self._lock:
            if self.cancelled.is_set() or self._count >= self.capacity:
                return False
            self._count += 1
            self._jobs.put((snapshot, time.perf_counter()))
            if self.timings is not None:
                self.timings.maximum('queue_high_watermark', self._count)
            if self.diagnostics is not None:
                self.diagnostics.count(self.channel + '_submitted')
                self.diagnostics.maximum(self.channel + '_queue_high_water', self._count)
            return True

    def poll(self):
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._count -= 1
        return result

    def _run(self):
        while not self.cancelled.is_set():
            try:
                snapshot, queued_at = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if self.cancelled.is_set():
                break
            began = time.perf_counter()
            if self.timings is not None:
                self.timings.record('queue_wait', began - queued_at)
            try:
                value, error = self.parse(snapshot), None
            except OperationCancelled as exc:
                value, error = None, exc
            except Exception as exc:
                value, error = None, exc
                if self.diagnostics is not None:
                    self.diagnostics.exception(
                        self.channel + '_parse',
                        exc,
                        'scan_levels_failed' if self.channel == 'grade' else 'scan_parse_failed',
                    )
            cancelled = isinstance(error, OperationCancelled)
            if self.diagnostics is not None:
                self.diagnostics.count(
                    self.channel + '_parse_cancelled'
                    if cancelled
                    else self.channel + '_parse_errors'
                    if error is not None
                    else self.channel + '_parsed'
                )
            if self.timings is not None:
                ended = time.perf_counter()
                self.timings.record('parse_job', ended - began)
                self.timings.record('queued_to_parse_end', ended - queued_at)
                self.timings.increment(
                    'parse_cancelled'
                    if cancelled
                    else 'parse_errors'
                    if error is not None
                    else 'parse_successes'
                )
            if not self.cancelled.is_set():
                self._results.put((snapshot, value, error))
            # Release the full-resolution source before waiting for another job.
            del snapshot, value, error

    def cancel(self):
        self.cancelled.set()
        for channel in (self._jobs, self._results):
            while True:
                try:
                    channel.get_nowait()
                except queue.Empty:
                    break

    def close(self):
        self.cancel()
        self._thread.join(timeout=2)
