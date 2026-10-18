# 定义扫描批次、图块与状态数据。
from dataclasses import dataclass

SCAN_LEVELS = (5, 10, 15, 20, 25)
MAX_FAILURE_DETAILS = 200


@dataclass(frozen=True)
class ScanFailure:
    row: int
    column: int
    reason: str = 'detail'

    @property
    def message(self):
        return {'level': '等级未读清', 'parse': '词条解析未通过', 'evidence': '截图证据已失效'}.get(
            self.reason, '声骸详情未读清'
        )


@dataclass(frozen=True)
class ScanTile:
    row: int
    column: int
    bounds: tuple
    signature: tuple
    level: object = None

    @property
    def center(self):
        x, y, w, h = self.bounds
        return (x + w // 2, y + h // 2)


@dataclass(frozen=True)
class BagGrid:
    tiles: tuple
    selected: object = None  # (local row, column), only a full visible tile.
    scrollbar: object = None  # Normalized knob center; missing is not bottom.
    context: tuple = ()  # Layout/filter/sort context supplied by the OCR caller.
    viewport: tuple = ()
    bottom_confirmed: bool = False  # Knob reaches the measured list-end region.
    single_page: bool = False
    capture_token: object = None  # Fresh host capture, required for edge recovery.
    anchor_boundary: object = None  # Measured last-selected bottom, possibly clipped.

    def rows(self):
        return tuple(
            tuple(t for t in self.tiles if t.row == r) for r in sorted({t.row for t in self.tiles})
        )


@dataclass(frozen=True)
class ScanAction:
    kind: str
    tile: object = None
    message: str = ''


@dataclass(frozen=True)
class ScanStatus:
    state: str = 'stopped'
    message: str = '打开背包，从列表顶部开始扫描'
    visited: int = 0
    added: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    captured: int = 0
    backlog: int = 0


class ScanStartError(ValueError):
    """Expected startup condition with a player-facing recovery instruction."""
