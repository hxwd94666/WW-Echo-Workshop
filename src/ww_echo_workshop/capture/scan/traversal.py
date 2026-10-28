# 推进扫描遍历、暂停与收录状态。
from dataclasses import replace
import threading
from .models import (
    SCAN_LEVELS,
    MAX_FAILURE_DETAILS,
    ScanFailure,
    ScanAction,
    ScanStatus,
    ScanStartError,
)
from .signatures import (
    _near_signature,
    _signature_distance,
    _same_grid,
    _matching_rows,
    _same_geometry,
    _scroll_overlap,
)


class ScanTraversal:
    """Bounded positional traversal; callers acknowledge each accepted detail.

    A full stable row is retained across scrolls. Ambiguous/missing overlap
    interrupts instead of guessing, even when all visible stat values match.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._status = ScanStatus()
        self.generation = 0
        self.minimum_level = 25
        self.profile_id = ''
        self.weights = self.sonata = None
        self._grid = self._resume_grid = self._target = None
        self._offset = 0
        self._seen = set()
        self._submitted = set()
        self._phase = 'idle'
        self._resume_phase = None
        self._scroll_checkpoint = False
        self._retries = self._bottom_probes = 0
        self._selection_reclicks = 0
        self._confirmed = False
        self.descending = False
        self._last_level = None
        self.resume_requested = False
        self.failed_positions = ()
        self._failure_details = ()
        self.last_position_check = ''
        self._overlap_pending = None
        self._anchor_active = False
        self._last_completed = None
        self._fixed = False

    def queue(self, profile_id, *, minimum_level=25, weights=None, sonata=None):
        with self._lock:
            if self._status.state in ('preparing', 'running', 'paused', 'draining'):
                raise ValueError('已有扫描正在进行')
            if type(minimum_level) is not int or minimum_level not in SCAN_LEVELS:
                raise ValueError('请选择有效最低等级')
            self.generation += 1
            self.profile_id, self.minimum_level = profile_id, minimum_level
            self.weights, self.sonata = weights, sonata
            self._status = ScanStatus('preparing', '准备扫描，请在 20 秒内切回声骸背包')
            self.resume_requested = False
            self.failed_positions = ()
            self._failure_details = ()
            return self.generation

    @property
    def status(self):
        with self._lock:
            return self._status

    def begin(
        self,
        profile_id,
        grid,
        *,
        minimum_level=25,
        weights=None,
        sonata=None,
        descending=False,
        queued_generation=None,
    ):
        with self._lock:
            if type(minimum_level) is not int or minimum_level not in SCAN_LEVELS:
                raise ValueError('Minimum scan level must be 5, 10, 15, 20 or 25')
            if self._status.state in ('running', 'paused'):
                raise RuntimeError('Scan already active')
            if (
                not profile_id
                or not grid.tiles
                or (grid.scrollbar is None and not grid.single_page)
                or (grid.scrollbar is not None and grid.scrollbar > 0.15)
            ):
                raise ScanStartError('请将声骸背包拉至顶部，再开始扫描')
            if not grid.single_page and not any(len(row) == 6 for row in grid.rows()):
                raise ValueError('A complete bag row is required')
            if queued_generation is None:
                self.generation += 1
            elif self.generation != queued_generation or self._status.state != 'preparing':
                raise ValueError('过期扫描请求')
            self.minimum_level = minimum_level
            self.profile_id, self.weights, self.sonata = profile_id, weights, sonata
            self._grid, self._resume_grid, self._target = grid, None, None
            self._offset, self._seen = 0, set()
            self._submitted.clear()
            self._phase, self._retries, self._bottom_probes = 'select', 0, 0
            self._selection_reclicks = 0
            self._resume_phase, self._scroll_checkpoint = None, False
            self._confirmed = False
            self.descending, self._last_level = bool(descending), None
            self.failed_positions = ()
            self._failure_details = ()
            self.last_position_check = ''
            self._overlap_pending = None
            self._anchor_active = False
            self._last_completed = None
            self._fixed = False
            self._status = ScanStatus('running', '扫描中')
            return self.generation

    def stop(self, message='已停止；保留已收录的声骸'):
        with self._lock:
            self.generation += 1
            self._status = replace(self._status, state='interrupted', message=message, backlog=0)
            self._submitted.clear()
            self._phase, self._confirmed = 'idle', False

    def pause(self, message='已暂停；继续前检查列表位置'):
        with self._lock:
            if self._status.state == 'running':
                self._resume_grid = self._grid
                self._resume_phase = self._phase
                self._scroll_checkpoint = False
                self._status = replace(self._status, state='paused', message=message)
                self._confirmed = False
                self.resume_requested = False

    def checkpoint_scroll_pause(self, grid):
        """Record the settled paused view without advancing the row anchor.

        Only the adapter waiting inside an unfinished eight-step segment calls
        this. Partial scrolling may hide the top row; the complete segment is
        still reconciled against its original grid exactly once after resume.
        """
        with self._lock:
            if (
                self._status.state != 'paused'
                or self._resume_phase != 'scroll'
                or self._target is not None
                or grid is None
                or grid.context != self._grid.context
                or grid.viewport != self._grid.viewport
            ):
                return False
            if self._anchor_active:
                edge = grid.anchor_boundary
                shift = None if edge is None else self._last_completed[0] - edge.row - self._offset
                if (
                    edge is None
                    or not 0 <= shift <= 3
                    or grid.scrollbar is None
                    or self._grid.scrollbar is None
                    or grid.scrollbar < self._grid.scrollbar - 0.003
                ):
                    return False
                self._resume_grid, self._scroll_checkpoint = grid, True
                return True
            old, new = self._grid.rows(), grid.rows()
            shifts = [
                shift
                for shift in (0, 1)
                if min(len(old) - shift, len(new)) > 0 and _matching_rows(self._grid, grid, shift)
            ]
            if (
                len(shifts) != 1
                or grid.scrollbar is None
                or self._grid.scrollbar is None
                or grid.scrollbar < self._grid.scrollbar - 0.003
            ):
                return False
            selection = self._grid.selected
            expected = (
                (selection[0] - shifts[0], selection[1])
                if selection is not None and selection[0] >= shifts[0]
                else None
            )
            if grid.selected is not None and grid.selected != expected:
                return False
            self._resume_grid, self._scroll_checkpoint = grid, True
            return True

    def resume(self, grid):
        with self._lock:
            if self._status.state != 'paused':
                return False
            if self._fixed:
                if grid is None:
                    return False
                self._grid = grid
                self._status = replace(self._status, state='running', message='继续扫描')
                self._phase = self._resume_phase or (
                    'select' if self._target is None else 'confirm'
                )
                self._resume_phase = None
                self._confirmed = False
                self._retries = 0
                self.resume_requested = False
                return True
            if self._resume_phase == 'scroll' and not self._scroll_checkpoint:
                self.stop('滚动中断位置未确认；请回到顶部重新扫描')
                return False
            if self._resume_grid is None or not _same_grid(self._resume_grid, grid):
                self.stop('列表已变化；请回到顶部重新扫描')
                return False
            scrolling = self._resume_phase == 'scroll'
            if not scrolling:
                self._grid = grid
            self._status = replace(self._status, state='running', message='继续扫描')
            self._phase = (
                'scroll' if scrolling else ('select' if self._target is None else 'confirm')
            )
            self._resume_phase, self._scroll_checkpoint = None, False
            self._retries, self._confirmed = 0, False
            self.resume_requested = False
            return True

    def _interrupt(self, message):
        self.stop(message)
        return ScanAction('interrupted', message=message)

    def advance(self, grid, *, foreground=True, profile_id=None, generation=None):
        with self._lock:
            if generation is not None and generation != self.generation:
                return ScanAction('wait', message='过期扫描回调')
            if self._status.state != 'running':
                return ScanAction(self._status.state, message=self._status.message)
            if profile_id is not None and profile_id != self.profile_id:
                return self._interrupt('档案已切换；扫描已停止')
            if not foreground:
                self.pause('游戏不在前台，扫描已暂停')
                return ScanAction('paused', message=self._status.message)
            if self._fixed:
                return self._advance_fixed(grid)
            if grid is None or not grid.tiles:
                return self._interrupt('未识别到背包列表；扫描已停止')
            if grid.context != self._grid.context or grid.viewport != self._grid.viewport:
                return self._interrupt('页面或筛选已变化；请重新扫描')
            if self._phase == 'scroll':
                return self._after_scroll(grid)
            expected_selection = (self._target.row, self._target.column) if self._target else None
            if (
                grid.selected is not None
                and grid.selected != self._grid.selected
                and grid.selected != expected_selection
            ):
                return self._interrupt('选中声骸已变化；扫描已停止')
            # A changed page without our own scroll must never reuse positions.
            # Selection/hover changes this cell's brightness, not its position.
            if not (
                len(grid.rows()) == len(self._grid.rows())
                and _same_geometry(self._grid, grid)
                and _matching_rows(
                    self._grid, grid, target=expected_selection, stationary_noise=True
                )
            ):
                changes = [
                    (
                        x.row + 1,
                        x.column + 1,
                        round(
                            sum(abs(v - w) for v, w in zip(x.signature, y.signature))
                            / max(1, len(x.signature)),
                            1,
                        ),
                    )
                    for x, y in zip(self._grid.tiles, grid.tiles)
                    if not _near_signature(x.signature, y.signature)
                ]
                bounds_changes = [
                    (x.row + 1, x.column + 1, x.bounds, y.bounds)
                    for x, y in zip(self._grid.tiles, grid.tiles)
                    if x.bounds != y.bounds
                ]
                self.last_position_check = (
                    '单元数='
                    + str((len(self._grid.tiles), len(grid.tiles)))
                    + '；差异行列与亮度差='
                    + repr(changes[:4])
                    + '；几何一致='
                    + str(_same_geometry(self._grid, grid))
                    + '；坐标差异='
                    + repr(bounds_changes[:3])
                    + '；选中='
                    + repr((self._grid.selected, grid.selected, expected_selection))
                )
                return self._interrupt('列表位置已变化；请回到顶部重新扫描')
            no_rail = (
                grid.single_page
                and self._grid.single_page
                and grid.scrollbar is None
                and self._grid.scrollbar is None
            )
            if not no_rail and (
                grid.scrollbar is None
                or self._grid.scrollbar is None
                or abs(grid.scrollbar - self._grid.scrollbar) >= 0.003
            ):
                return self._interrupt('滚动条位置已变化；请回到顶部重新扫描')
            self._grid = grid
            if self._target is not None:
                identity = (self._target.row, self._target.column)
                self._target = next((t for t in grid.tiles if (t.row, t.column) == identity), None)
                if self._target is None:
                    return self._interrupt('选中位置已变化；扫描已停止')
                if grid.selected == identity:
                    # The host must still require fresh stable detail frames.
                    self._confirmed = True
                    self._phase = 'read'
                    return ScanAction('read', self._target)
                self._retries += 1
                if self._retries > 3:
                    # A sent click is not a game acknowledgement. Retry the
                    # same checked tile once, never skip to another tile or
                    # authorize reading the still-selected previous item.
                    def display(position):
                        return None if position is None else (position[0] + 1, position[1] + 1)

                    self.last_position_check = (
                        '目标='
                        + repr(display(identity))
                        + '；实际选中='
                        + repr(display(grid.selected))
                        + '；点击中心='
                        + repr(self._target.center)
                        + '；确认帧数='
                        + str(self._retries)
                        + '；已重试点击='
                        + str(self._selection_reclicks)
                    )
                    if self._phase == 'confirm' and self._selection_reclicks == 0:
                        self._selection_reclicks = 1
                        self._retries = 0
                        return ScanAction('click', self._target, '选中未确认，重试点击一次')
                    return self._interrupt('选中声骸未确认；请重新扫描')
                return ScanAction('wait', self._target, '等待选中声骸')
            for row in grid.rows():
                for tile in row:
                    if (self._offset + tile.row, tile.column) not in self._seen:
                        self._target, self._phase, self._retries = tile, 'confirm', 0
                        self._selection_reclicks = 0
                        self._confirmed = False
                        return ScanAction('click', tile)
            if grid.single_page:
                self._status = replace(self._status, state='finished', message='当前列表扫描完成')
                self._phase = 'idle'
                return ScanAction('finished', message=self._status.message)
            self._phase, self._retries = 'scroll', 0
            return ScanAction('scroll', message='滚动并定位上一件的底部金边')

    def enable_fixed(self, grid):
        """Freeze the first-page click points; no later coordinate fitting."""
        with self._lock:
            self._fixed = True
            self._grid = grid

    def fixed_next_page(self, grid):
        with self._lock:
            self._offset += 3
            self._grid = grid
            self._target = None
            self._phase = 'select'
            self._confirmed = False
            self._retries = 0

    def fixed_threshold(self):
        with self._lock:
            self._status = replace(
                self._status,
                state='finished',
                skipped=self._status.skipped + 1,
                message=f'已扫描完 +{self.minimum_level} 及以上声骸',
            )
            self._target = None
            self._phase = 'idle'

    def _advance_fixed(self, grid):
        self._grid = grid
        if self._target is not None:
            if grid.selected == (self._target.row, self._target.column):
                self._confirmed = True
                self._phase = 'read'
                return ScanAction('read', self._target)
            self._retries += 1
            if self._retries == 4:
                return ScanAction('click', self._target, '等待选中响应，重试本件')
            if self._retries >= 8:
                self.pause('点击暂未响应，扫描已暂停；切回游戏后继续')
                return ScanAction('paused', message=self._status.message)
            return ScanAction('wait', self._target)
        for tile in grid.tiles:
            if (self._offset + tile.row, tile.column) not in self._seen:
                self._target = tile
                self._phase = 'confirm'
                self._retries = 0
                self._confirmed = False
                return ScanAction('click', tile)
        if self._status.visited >= 3000:
            self._status = replace(self._status, state='finished', message='已扫描至背包容量上限')
            self._phase = 'idle'
            return ScanAction('finished')
        if grid.bottom_confirmed or grid.single_page:
            self._status = replace(self._status, state='finished', message='扫描完成')
            self._phase = 'idle'
            return ScanAction('finished')
        self._phase = 'scroll'
        return ScanAction('scroll')

    def accept_detail(self, level, outcome, *, generation, position):
        """Only final selected-panel level authorizes storage by the adapter.

        Returns a decision before storage. 'commit' keeps the tile pending until
        complete_detail is called with the repository's actual outcome.
        """
        with self._lock:
            if (
                generation != self.generation
                or self._status.state != 'running'
                or self._phase != 'read'
                or not self._confirmed
                or self._target is None
                or position != (self._offset + self._target.row, self._target.column)
            ):
                return 'stale'
            if type(level) is not int or not 0 <= level <= 25:
                self.complete_detail('failed', generation=generation, position=position)
                return 'failed'
            if self.descending and self._last_level is not None and level > self._last_level:
                self._interrupt('等级顺序发生变化；请重新检查排序')
                return 'stale'
            self._last_level = level
            if level < self.minimum_level:
                self.complete_detail('skipped', generation=generation, position=position)
                if self.descending:
                    self._status = replace(
                        self._status,
                        state='finished',
                        message=f'已扫描完 +{self.minimum_level} 及以上声骸',
                    )
                    self._phase = 'idle'
                    return 'threshold'
                return 'skipped'
            if outcome not in ('valid', 'review'):
                self.complete_detail('failed', generation=generation, position=position)
                return 'failed'
            return 'commit'

    @property
    def position(self):
        with self._lock:
            return (
                None
                if self._target is None
                else (self._offset + self._target.row, self._target.column)
            )

    def failure_snapshot(self):
        with self._lock:
            return dict(
                profile_id=self.profile_id,
                generation=self.generation,
                total=self._status.failed,
                details=self._failure_details,
                omitted=max(0, self._status.failed - len(self._failure_details)),
                state=self._status.state,
            )

    def _record_failure(self, position, reason):
        if len(self.failed_positions) < 2:
            self.failed_positions += (position,)
        if len(self._failure_details) < MAX_FAILURE_DETAILS:
            self._failure_details += (
                ScanFailure(
                    *position, reason if reason in ('level', 'parse', 'evidence') else 'detail'
                ),
            )

    def complete_detail(self, outcome, *, generation, position, reason='detail'):
        with self._lock:
            if outcome not in ('added', 'pending', 'skipped', 'failed', 'queued'):
                raise ValueError('Unknown scan detail outcome')
            if generation != self.generation or position != self.position:
                return False
            if self._target is None or not self._confirmed or self._status.state != 'running':
                raise RuntimeError('No confirmed scan tile')
            self._last_completed = self.position
            self._seen.add(self.position)
            if outcome == 'failed':
                self._record_failure(self.position, reason)
            if outcome == 'queued':
                self._submitted.add(position)
                self._status = replace(
                    self._status,
                    visited=self._status.visited + 1,
                    captured=self._status.captured + 1,
                    backlog=self._status.backlog + 1,
                )
            else:
                self._status = replace(
                    self._status,
                    visited=self._status.visited + 1,
                    **{outcome: getattr(self._status, outcome) + 1},
                )
            self._target, self._phase, self._confirmed = None, 'select', False
            return True

    def resolve_detail(self, outcome, *, generation, position, reason='detail'):
        with self._lock:
            if (
                generation != self.generation
                or position not in self._submitted
                or self._status.state not in ('running', 'draining')
            ):
                return False
            if outcome not in ('added', 'pending', 'failed'):
                raise ValueError('Unknown parsed outcome')
            self._submitted.remove(position)
            if outcome == 'failed':
                self._record_failure(position, reason)
            self._status = replace(
                self._status,
                backlog=self._status.backlog - 1,
                **{outcome: getattr(self._status, outcome) + 1},
            )
            return True

    def drain(self):
        with self._lock:
            if self._status.state == 'finished' and self._status.backlog:
                self._finish_message = self._status.message
                self._status = replace(self._status, state='draining', message='正在解析剩余声骸')
            elif self._status.state == 'draining' and not self._status.backlog:
                self._status = replace(self._status, state='finished', message=self._finish_message)

    def prepare_scroll(self):
        with self._lock:
            if self._status.state != 'running' or self._target is not None:
                return False
            self._phase, self._retries = 'scroll', 0
            self._overlap_pending = None
            return True

    def _after_scroll(self, grid, *, choose=True):
        old, new = self._grid.rows(), grid.rows()
        if len(old) == len(new) and _matching_rows(self._grid, grid):
            self._retries += 1
            at_bottom = (
                grid.bottom_confirmed
                and self._grid.bottom_confirmed
                and grid.scrollbar is not None
                and self._grid.scrollbar is not None
                and abs(grid.scrollbar - self._grid.scrollbar) < 0.003
            )
            if at_bottom:
                unvisited = any(
                    (self._offset + t.row, t.column) not in self._seen for t in grid.tiles
                )
                if unvisited:
                    self._grid, self._phase = grid, 'select'
                    return self.advance(grid) if choose else ScanAction('positioned')
                self._bottom_probes += 1
                if self._bottom_probes >= 2:
                    self._status = replace(self._status, state='finished', message='扫描完成')
                    self._phase = 'idle'
                    return ScanAction('finished', message='扫描完成')
                return ScanAction('scroll', message='确认列表底部')
            if self._retries > 3:
                return self._interrupt('滚动未生效或底部不明确；扫描已停止')
            return ScanAction('wait', message='等待滚动后的列表')
        # One unique positive row displacement; negative/ambiguous overlaps fail.
        candidates = []
        metrics = {shift: _scroll_overlap(self._grid, grid, shift) for shift in range(len(old))}
        edge_candidates = []
        for shift in range(1, len(old)):
            shared = min(len(old) - shift, len(new))
            if (
                shared
                and len(old[shift]) == len(new[0]) == 6
                and metrics[shift]['geometry']
                and metrics[shift]['anchors'] >= 3
                and metrics[shift]['mean'] <= 13
                and metrics[shift]['worst'] <= 18
            ):
                candidates.append(shift)
            elif (
                shared
                and len(old[shift]) == len(new[0]) == 6
                and metrics[shift]['geometry']
                and metrics[shift]['anchors'] >= 12
                and metrics[shift]['mean'] <= 13
                and metrics[shift]['worst'] <= 20
                and metrics[shift]['outliers'] == 1
            ):
                # One marginal cell may be lighting/jitter, not a different row.
                # This does NOT lower the normal bound or accept it immediately.
                edge_candidates.append(shift)
        # Repeated/similar rows need a clear winner over every other possible
        # shift (including unchanged content). Never choose the first near row.
        if candidates:
            best = min(candidates, key=lambda shift: metrics[shift]['mean'])
            alternatives = [
                m['mean'] for shift, m in metrics.items() if shift != best and m['anchors'] >= 3
            ]
            margin = min(alternatives, default=float('inf')) - metrics[best]['mean']
            if margin >= 6:
                candidates = [best]
            elif len(candidates) == 1:
                candidates = []
        else:
            margin = None
        edge_wait = False
        if not candidates and len(edge_candidates) == 1:
            best = edge_candidates[0]
            alternatives = [
                m['mean'] for shift, m in metrics.items() if shift != best and m['anchors'] >= 3
            ]
            margin = min(alternatives, default=float('inf')) - metrics[best]['mean']
            if margin >= 12 and grid.capture_token is not None:
                pending = self._overlap_pending
                if (
                    pending is not None
                    and pending[0] == best
                    and pending[1].capture_token != grid.capture_token
                    and _same_grid(pending[1], grid)
                ):
                    candidates = [best]
                    self._overlap_pending = None
                else:
                    self._overlap_pending = (best, grid)
                    edge_wait = True
        if not edge_wait:
            self._overlap_pending = None
        self.last_position_check = (
            '滚动候选位移='
            + repr(candidates)
            + '；滚动条='
            + repr((self._grid.scrollbar, grid.scrollbar))
            + '；完整行数='
            + repr((len(old), len(new)))
            + '；候选整体指标='
            + repr(
                {
                    shift: {
                        k: (round(v, 1) if k in ('mean', 'worst') else v) for k, v in metric.items()
                    }
                    for shift, metric in metrics.items()
                }
            )
            + '；候选间隔='
            + repr(None if margin is None else round(margin, 1))
            + '；边缘候选待复核='
            + str(edge_wait)
            + '；首格坐标='
            + repr((old[0][0].bounds if old else None, new[0][0].bounds if new else None))
            + '；候选行缩略图差异='
            + repr(
                [
                    (
                        shift,
                        [
                            round(_signature_distance(x.signature, y.signature), 1)
                            for x, y in zip(old[shift], new[0])
                        ],
                    )
                    for shift in range(1, len(old))
                    if new
                ]
            )
        )
        if not candidates:
            # A fresh screenshot can still contain an in-flight wheel animation.
            # Keep the original anchor and offset until overlap is actually
            # acknowledged. Observe at most four frames; never resend wheels.
            self._retries += 1
            if self._retries <= 3:
                return ScanAction('wait', message='等待滚动动画和重叠行稳定')
            return self._interrupt('滚动重叠位置不明确；请回到顶部重新扫描')
        if len(candidates) != 1:
            return self._interrupt('滚动重叠位置不明确；请回到顶部重新扫描')
        if (
            grid.scrollbar is None
            or self._grid.scrollbar is None
            or grid.scrollbar <= self._grid.scrollbar
        ):
            return self._interrupt('滚动方向未确认；扫描已停止')
        self._offset += candidates[0]
        self._grid, self._phase, self._retries = grid, 'select', 0
        self._bottom_probes = 0
        return self.advance(grid) if choose else ScanAction('positioned')
