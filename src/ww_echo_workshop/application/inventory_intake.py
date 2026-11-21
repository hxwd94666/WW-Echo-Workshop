# 定义库存工具栏可使用的收录命令和只读状态。
from enum import Enum, auto
from typing import Protocol

from ww_echo_workshop.capture.intake import CaptureStatus
from ww_echo_workshop.capture.scan.models import ScanStatus


class IntakeStart(Enum):
    STARTED = auto()
    NEED_SHORTCUT = auto()
    DISCONNECTED = auto()


class InventoryIntake(Protocol):
    @property
    def status(self) -> CaptureStatus: ...

    @property
    def ready(self) -> bool: ...

    @property
    def idle(self) -> bool: ...

    @property
    def mode(self) -> str: ...

    @property
    def minimum_level(self) -> str: ...

    @property
    def scan_status(self) -> ScanStatus: ...

    @property
    def scan_profile(self) -> str: ...

    @property
    def failed_positions(self) -> tuple[tuple[int, int], ...]: ...

    def failure_snapshot(self) -> dict: ...

    def set_mode(self, mode: str) -> None: ...

    def set_minimum_level(self, level: str) -> None: ...

    def start(self, profile_id: str) -> IntakeStart: ...

    def pause_or_resume(self) -> bool: ...

    def stop(self) -> None: ...

    def reset_for_profile(self) -> None: ...
