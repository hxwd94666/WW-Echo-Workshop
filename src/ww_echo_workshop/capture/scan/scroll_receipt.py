# 保留固定三行翻页的滚轮节奏、批内计数与停稳确认。
from dataclasses import dataclass, field

from .models import BagGrid
from .signatures import _same_grid, _scroll_overlap

ROWS_PER_BATCH = 3
WHEEL_INTERVAL = 0.08
WHEEL_SETTLE = 0.30


def wheel_count(batch):
    if type(batch) is not int or batch < 1:
        raise ValueError('Positive scroll batch required')
    return 25 if batch % 6 == 0 else 24


@dataclass
class FixedScrollBatch:
    origin: BagGrid
    number: int
    steps: int = field(init=False, default=0)
    _last_before_input: object = field(init=False, default=None)
    _previous: BagGrid | None = field(init=False, default=None)

    @property
    def remaining(self):
        return wheel_count(self.number) - self.steps

    def input_sent(self, token):
        self.steps += 1
        self._last_before_input = token
        self.require_observation()

    def require_observation(self):
        self._previous = None

    def settled(self, grid):
        if (
            self.remaining
            or grid.capture_token is None
            or grid.capture_token in (self.origin.capture_token, self._last_before_input)
            or grid.viewport != self.origin.viewport
            or grid.context != self.origin.context
            or self.origin.scrollbar is None
            or grid.scrollbar is None
            or grid.scrollbar <= self.origin.scrollbar
        ):
            self.require_observation()
            return False
        previous, self._previous = self._previous, grid
        if previous is None or previous.capture_token == grid.capture_token:
            return False
        return _same_grid(previous, grid)

    def incomplete_turn(self, grid):
        # A clearly overlapping ordinary page means the fixed input under-shot.
        # Never use it to choose a different offset or dispatch extra wheel inputs.
        if grid.bottom_confirmed:
            return False
        for shift in range(len(self.origin.rows())):
            metric = _scroll_overlap(self.origin, grid, shift)
            if (
                metric['geometry']
                and metric['anchors'] >= 5
                and metric['mean'] <= 13
                and metric['worst'] <= 18
            ):
                return True
        return False
