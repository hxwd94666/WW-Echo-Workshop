# 保存实时识别结果的只读快照，供工作台手动捕获候选；不保存截图或原始帧。
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class LiveSnapshot:
    cost: int | None
    main_rows: tuple[tuple[str, float], ...]
    sub_rows: tuple[tuple[str, float], ...]
    selected_ids: tuple[str, ...]
    current_score: float | None
    observed_cost: int | None
    layout: str
    captured_at: float


class LiveSnapshotStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest = None

    def capture(self, analysis, *, now=None):
        if analysis is None:
            return None
        main = tuple(
            (str(row.stat_name), float(row.value)) for row in getattr(analysis, 'main_rows', ())
        )
        sub = tuple(
            (str(row.stat_name), float(row.value)) for row in getattr(analysis, 'sub_rows', ())
        )
        if not main and not sub:
            return None
        snapshot = LiveSnapshot(
            cost=getattr(analysis, 'cost', None),
            main_rows=main,
            sub_rows=sub,
            selected_ids=tuple(getattr(analysis, 'selected_ids', ()) or ()),
            current_score=getattr(analysis, 'current_score', None),
            observed_cost=getattr(analysis, 'observed_cost', None),
            layout=str(getattr(analysis, 'layout', '') or ''),
            captured_at=time.monotonic() if now is None else now,
        )
        with self._lock:
            self._latest = snapshot
        return snapshot

    def latest(self):
        with self._lock:
            return self._latest

    def clear(self):
        with self._lock:
            self._latest = None
