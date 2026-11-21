# 推进扫描遍历、暂停与收录状态。
from dataclasses import replace
import threading
from .models import (
    SCAN_LEVELS,
    MAX_FAILURE_DETAILS,
    ScanFailure,
    ScanAction,
    ScanResult,
    ScanStatus,
    ScanStartError,
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
        self._grid = self._target = None
        self._offset = 0
        self._seen = set()
        self._submitted = set()
        self._phase = 'idle'
        self._resume_phase = None
        self._retries = 0
        self._confirmed = False
        self.descending = False
        self._last_level = None
        self.resume_requested = False
        self.failed_positions = ()
        self._failure_details = ()
        self._latest_run = None
        self._retired_result = None
        self._retired_submitted = set()

    def queue(self, profile_id, *, minimum_level=25, weights=None, sonata=None):
        with self._lock:
            if self._status.state in ('preparing', 'running', 'paused', 'draining'):
                raise ValueError('已有扫描正在进行')
            if type(minimum_level) is not int or minimum_level not in SCAN_LEVELS:
                raise ValueError('请选择有效最低等级')
            if self._status.state == 'finished':
                self._retire_result(self.generation)
            self.generation += 1
            self._latest_run = self.generation
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

    def status_for(self, generation):
        with self._lock:
            return self._status if self.generation == generation else None

    def _result(self, generation):
        return ScanResult(
            generation,
            self._status,
            self.minimum_level,
            self.failed_positions,
            self._failure_details,
        )

    def _retire_result(self, generation):
        self._retired_result = self._result(generation)
        self._retired_submitted = set(self._submitted)

    def result_for(self, generation):
        with self._lock:
            if self.generation == generation and self._status.state == 'finished':
                return self._result(generation)
            result = self._retired_result
            return result if result is not None and result.generation == generation else None

    def can_publish_result(self, generation):
        with self._lock:
            return self._latest_run == generation

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
            if not profile_id or not grid.at_top:
                raise ScanStartError('请将声骸背包拉至顶部，再开始扫描')
            if not grid.single_page and not any(len(row) == 6 for row in grid.rows()):
                raise ValueError('A complete bag row is required')
            if queued_generation is None:
                if self._status.state == 'finished':
                    self._retire_result(self.generation)
                self.generation += 1
            elif self.generation != queued_generation or self._status.state != 'preparing':
                raise ValueError('过期扫描请求')
            self._latest_run = self.generation
            self.minimum_level = minimum_level
            self.profile_id, self.weights, self.sonata = profile_id, weights, sonata
            self._grid, self._target = grid, None
            self._offset, self._seen = 0, set()
            self._submitted.clear()
            self._phase, self._retries = 'select', 0
            self._resume_phase = None
            self._confirmed = False
            self.descending, self._last_level = bool(descending), None
            self.failed_positions = ()
            self._failure_details = ()
            self._status = ScanStatus('running', '扫描中')
            return self.generation

    def stop(self, message='已停止；保留已收录的声骸', *, generation=None):
        with self._lock:
            if generation is not None and self.generation != generation:
                return False
            retiring = self.generation
            had_run = self._status.state not in ('stopped', 'interrupted')
            self.generation += 1
            self._status = replace(self._status, state='interrupted', message=message, backlog=0)
            if had_run:
                self._retire_result(retiring)
            self._submitted.clear()
            self._phase, self._confirmed = 'idle', False
            return True

    def pause(self, message='已暂停；继续前检查列表位置', *, generation=None):
        with self._lock:
            if generation is not None and self.generation != generation:
                return False
            if self._status.state == 'running':
                self._resume_phase = self._phase
                self._status = replace(self._status, state='paused', message=message)
                self._confirmed = False
                self.resume_requested = False
                return True
            return False

    def resume(self, *, generation=None):
        """Resume a page already validated by the capture adapter."""
        with self._lock:
            if generation is not None and self.generation != generation:
                return False
            if self._status.state != 'paused' or self._grid is None:
                return False
            self._status = replace(self._status, state='running', message='继续扫描')
            self._phase = self._resume_phase or ('select' if self._target is None else 'confirm')
            self._resume_phase = None
            self._confirmed = False
            self._retries = 0
            self.resume_requested = False
            return True

    def request_resume(self, *, generation):
        with self._lock:
            if generation == self.generation and self._status.state == 'paused':
                self.resume_requested = True

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
            if self._phase == 'scroll':
                # Only a completed fixed scroll batch can advance the page.
                return ScanAction('scroll')
            if (
                grid is None
                or grid.tiles != self._grid.tiles
                or grid.context != self._grid.context
                or grid.viewport != self._grid.viewport
            ):
                return self._interrupt('列表位置已变化；请回到顶部重新扫描')
            return self._advance_measured_page(grid)

    @property
    def page_offset(self):
        with self._lock:
            return self._offset

    def accept_fixed_page(self, origin, destination, *, generation):
        from .scroll_receipt import ROWS_PER_BATCH

        with self._lock:
            if (
                generation != self.generation
                or self._status.state != 'running'
                or self._phase != 'scroll'
                or self._target is not None
                or origin.tiles != self._grid.tiles
                or origin.context != self._grid.context
                or destination.context != origin.context
                or origin.viewport != self._grid.viewport
                or destination.viewport != origin.viewport
                or not destination.tiles
                or any(
                    (self._offset + tile.row, tile.column) not in self._seen
                    for tile in origin.tiles
                )
            ):
                return False
            self._offset += ROWS_PER_BATCH
            self._grid = destination
            self._target = None
            self._phase = 'select'
            self._confirmed = False
            self._retries = 0
            return True

    def _advance_measured_page(self, grid):
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
            if generation != self.generation:
                return self._resolve_retired_commit(outcome, generation, position)
            # A pause can arrive after the repository accepted a polled result.
            # Consume its bookkeeping without authorizing new input or writes.
            if position not in self._submitted or self._status.state not in (
                'running',
                'paused',
                'draining',
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

    def _resolve_retired_commit(self, outcome, generation, position):
        # Only already-written slots can update the old run after a stop.
        result = self._retired_result
        if (
            result is None
            or result.generation != generation
            or outcome not in ('added', 'pending')
            or position not in self._retired_submitted
        ):
            return False
        self._retired_submitted.remove(position)
        counter = {outcome: getattr(result.status, outcome) + 1}
        self._retired_result = replace(result, status=replace(result.status, **counter))
        if generation == self._latest_run and self._status.state == 'interrupted':
            self._status = replace(self._status, **counter)
        return True

    def drain(self, *, generation=None):
        with self._lock:
            if generation is not None and self.generation != generation:
                return
            if self._status.state == 'finished' and self._status.backlog:
                self._finish_message = self._status.message
                self._status = replace(self._status, state='draining', message='正在解析剩余声骸')
            elif self._status.state == 'draining' and not self._status.backlog:
                self._status = replace(self._status, state='finished', message=self._finish_message)
