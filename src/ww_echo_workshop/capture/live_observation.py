# 规划实时评分的单帧 OCR 区域、同帧回退及评分稳定性。
from dataclasses import dataclass, replace

from ww_echo_workshop.capture.viewport import COMPARISON_SPLIT


CONFIRM_AFTER = 0.12
MISSING_GRACE = 0.45
COST_GRACE = 0.45


def analysis_key(analysis, *, include_cost=True):
    def rows(values):
        return tuple(
            (getattr(row, 'stat_name', ''), round(float(getattr(row, 'value', 0.0)), 2))
            for row in values
        )

    return (
        getattr(analysis, 'layout', ''),
        getattr(analysis, 'comparison', False),
        getattr(analysis, 'layout_provisional', False),
        getattr(analysis, 'page_kind', ''),
        getattr(analysis, 'cost', None) if include_cost else None,
        getattr(analysis, 'observed_cost', None) if include_cost else None,
        getattr(analysis, 'selected_template', ''),
        tuple(getattr(analysis, 'selected_ids', ()) or ()),
        rows(getattr(analysis, 'main_rows', ())),
        rows(getattr(analysis, 'sub_rows', ())),
    )


def _fingerprint(frame, regions):
    return tuple(hash(frame[y : y + h, x : x + w].tobytes()) for x, y, w, h in regions)


def _detail_fingerprint(frame, view, layout, comparison, side):
    if comparison or layout in ('enhancement', 'tuning'):
        # Exclude the 3D model, transitions elsewhere on screen and the other
        # comparison column. Pixels stay in the viewport's capture space.
        divisor = COMPARISON_SPLIT
        start, end = (
            (0.52, divisor)
            if comparison and side == 'left'
            else (divisor, 0.995)
            if comparison
            else (0.01, 0.5)
        )
        x, y = view.offset('right' if comparison else 'left')
        x1, x2 = round(x + start * view.reference_width), round(x + end * view.reference_width)
        pixels = frame[y : y + view.height, x1:x2, :3]
    else:
        ((x, y, w, h), *_) = view.live_regions(layout)
        pixels = frame[y : y + h, x : x + w, :3]
    # White glyphs on the translucent panel are much less sensitive to
    # animation behind it than raw BGR bytes.
    return layout, comparison, hash((pixels.min(axis=2) > 180).tobytes())


def _cost_header_fingerprint(frame, view, analysis, side, last):
    if not analysis.main_rows or analysis.layout not in ('bag', 'display', 'single'):
        return None
    # OCR rectangles can jitter on identical pixels. On a miss, compare the
    # exact previous header crop, rather than recropping to the noisy row.
    rows = analysis.main_rows
    if (
        analysis.observed_cost is None
        and last is not None
        and last.observed_cost is not None
        and last.main_rows
        and (last.layout, last.comparison) == (analysis.layout, analysis.comparison)
    ):
        rows = last.main_rows
    region = view.cost_header_region(rows[0].y, comparison=analysis.comparison, side=side)
    if region is None:
        return None
    return region, _fingerprint(frame, (region,))


def _reuse_score(candidate, stable, *, retained_cost=None):
    return replace(
        candidate,
        cost=stable.cost,
        retained_cost=retained_cost,
        row_scores=stable.row_scores,
        summary=stable.summary,
        tier_labels=stable.tier_labels,
        tier_colors=stable.tier_colors,
        current_score=stable.current_score,
        potential_score=stable.potential_score,
        score_text=stable.score_text,
        potential_text=stable.potential_text,
        rating_color=stable.rating_color,
    )


def _reference_only(candidate):
    return replace(
        candidate,
        observed_cost=None,
        score_text=f'{candidate.current_score:.2f}',
        rating_color=None,
    )


class _ScoreConfirmation:
    def __init__(self):
        self.current = None
        self._visual = None
        self._cost_header = None
        self._cost_missing_since = None
        self._cost_conflict = None
        self._pending = None
        self._missing_since = None

    @property
    def retry(self):
        return any(
            value is not None
            for value in (
                self._pending,
                self._missing_since,
                self._cost_missing_since,
                self._cost_conflict,
            )
        )

    def accept(self, candidate, visual, now, *, cost_header=None):
        stable = self.current
        if stable is None:
            return self._commit(candidate, visual, cost_header)
        if candidate.current_score is None:
            self._pending = None
            self._cost_missing_since = self._cost_conflict = None
            if self._missing_since is None:
                self._missing_since = now
            if now - self._missing_since < MISSING_GRACE:
                return None
            return self._commit(candidate, visual, cost_header)
        self._missing_since = None
        key = analysis_key(candidate)
        if key == analysis_key(stable):
            # Reuse only scoring presentation; fresh coordinates must come
            # from this OCR read.
            return self._commit(_reuse_score(candidate, stable), visual, cost_header)
        if analysis_key(candidate, include_cost=False) == analysis_key(stable, include_cost=False):
            if candidate.observed_cost is None and stable.observed_cost is not None:
                if (
                    visual == self._visual
                    and cost_header is not None
                    and cost_header == self._cost_header
                ):
                    if self._cost_missing_since is None:
                        self._cost_missing_since = now
                    if now - self._cost_missing_since < COST_GRACE:
                        # Keep old visual evidence out of the fresh OCR field.
                        self._pending = None
                        return _reuse_score(candidate, stable, retained_cost=stable.observed_cost)
                return self._commit(candidate, visual, cost_header)
            if candidate.observed_cost is not None:
                if self._cost_conflict is not None:
                    trusted, proposed, started = self._cost_conflict
                    if candidate.observed_cost == trusted or (
                        key == proposed and now - started >= CONFIRM_AFTER
                    ):
                        return self._commit(candidate, visual, cost_header)
                    if key != proposed:
                        self._cost_conflict = (trusted, key, now)
                    return _reference_only(candidate)
                if stable.observed_cost is None:
                    # Explicitly observed COST outranks a reference estimate,
                    # even when nothing in the panel pixels moved.
                    return self._commit(candidate, visual, cost_header)
                if candidate.observed_cost != stable.observed_cost:
                    reference = self._commit(_reference_only(candidate), visual, cost_header)
                    self._cost_conflict = (stable.observed_cost, key, now)
                    return reference
        self._cost_missing_since = self._cost_conflict = None
        if visual == self._visual and cost_header == self._cost_header:
            if stable.observed_cost is not None and candidate.observed_cost is None:
                # The panel still looks the same but the parsed rows disagree;
                # that is not strong enough evidence to extend a confirmed COST.
                return self._commit(_reference_only(stable), visual, cost_header)
            self._pending = None
            return stable
        if self._pending is None or self._pending[0] != key:
            self._pending = (key, now)
            return None
        if now - self._pending[1] < CONFIRM_AFTER:
            return None
        return self._commit(candidate, visual, cost_header)

    def _commit(self, candidate, visual, cost_header):
        self._pending = self._missing_since = None
        self._cost_missing_since = self._cost_conflict = None
        self.current = candidate if candidate.current_score is not None else None
        self._visual = visual
        self._cost_header = cost_header
        return candidate


@dataclass(frozen=True)
class LiveObservation:
    analysis: object
    boxes: tuple


class LiveObservationState:
    def __init__(self):
        self.reset()

    def reset(self):
        self._context = self._scene_digest = self._panel_digest = None
        self._header = None
        self._full_at = float('-inf')
        self._confirm_full = False
        self._layout = ''
        self._comparison = False
        self._costs = {}
        self._primary = _ScoreConfirmation()
        self._reference = _ScoreConfirmation()
        self._reference_template = None

    def confirm_reference(self, analysis, frame, view, now, *, template):
        if template != self._reference_template:
            self._reference = _ScoreConfirmation()
            self._reference_template = template
        visual = _detail_fingerprint(frame, view, analysis.layout, True, 'left')
        header = _cost_header_fingerprint(frame, view, analysis, 'left', self._reference.current)
        return self._reference.accept(analysis, visual, now, cost_header=header)

    def observe(self, frame, view, *, context, now, read_regions, analyze):
        import numpy as np

        key = (context, frame.shape, view.bounds)
        if key != self._context:
            self.reset()
            self._context = key
        scene_digest = hash(frame[::32, ::32].tobytes())
        scene_changed = self._scene_digest is not None and scene_digest != self._scene_digest
        x, y, w, h = view.bounds
        header = frame[y : y + round(h * 0.12) : 4, x : x + w : 4, :3].min(axis=2) > 180
        # A changed page caption bypasses the cooldown: a right-only crop
        # can still contain valid rows after switching into a comparison.
        page_changed = self._header is not None and np.count_nonzero(header != self._header) > 16
        full = (
            not self._layout
            or self._confirm_full
            or page_changed
            or now - self._full_at >= 2.0
            or (scene_changed and now - self._full_at >= 0.5)
        )
        regions = view.live_regions('comparison' if self._comparison else self._layout)
        panel_digest = None
        if not full:
            # Coarse scene samples only schedule wide reads. A narrow glyph
            # can fall entirely between those points, so compare the actual
            # OCR pixels before reusing the visible score.
            panel_digest = _fingerprint(frame, regions)
            if panel_digest == self._panel_digest and not (
                self._primary.retry or self._reference.retry
            ):
                return None

        def read(regions, layout='', comparison=False):
            boxes = tuple(read_regions(regions))
            return boxes, analyze(boxes, layout, comparison, self._costs.get)

        if full or self._comparison:
            boxes, analysis = read(view.ocr_regions())
            full = True
        else:
            boxes, analysis = read(regions, self._layout)
            if not analysis.main_rows:
                boxes, analysis = read(view.ocr_regions())
                full = True
        if full:
            self._full_at = now
        layout = self._layout or analysis.layout
        comparison = self._comparison or analysis.comparison
        new_page = bool(
            (page_changed or self._confirm_full)
            and analysis.layout
            and (self._layout, self._comparison) != (analysis.layout, analysis.comparison)
        )
        if new_page:
            layout, comparison = analysis.layout, analysis.comparison
        visual = _detail_fingerprint(frame, view, layout, comparison, 'right')
        cost_header = _cost_header_fingerprint(
            frame, view, analysis, 'right', self._primary.current
        )
        analysis = self._primary.accept(analysis, visual, now, cost_header=cost_header)
        self._confirm_full = analysis is None and full
        self._scene_digest = scene_digest
        self._header = header
        if analysis is not None and (full or analysis.current_score is None):
            self._layout = analysis.layout
            self._comparison = bool(analysis.comparison)
        if not self._comparison:
            self._reference = _ScoreConfirmation()
            self._reference_template = None
        next_regions = view.live_regions('comparison' if self._comparison else self._layout)
        self._panel_digest = (
            panel_digest
            if panel_digest is not None and next_regions == regions
            else _fingerprint(frame, next_regions)
        )
        if analysis is None:
            return None
        signature = tuple((row.stat_name, row.value) for row in analysis.main_rows)
        if analysis.observed_cost is not None:
            self._costs[signature] = analysis.observed_cost
            if len(self._costs) > 64:
                self._costs.pop(next(iter(self._costs)))
        return LiveObservation(analysis, boxes)
