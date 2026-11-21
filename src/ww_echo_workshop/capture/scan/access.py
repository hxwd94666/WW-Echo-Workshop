# 按会话、前台与扫描阶段判定观察、提交和输入权限。
from enum import Enum, auto


class ScanAccess(Enum):
    ALLOW = auto()
    INACTIVE = auto()
    CANCEL = auto()
    CHANGED = auto()
    HOST_PAUSED = auto()
    DISCONNECTED = auto()
    OUT_OF_FOCUS = auto()


def scan_access(
    phase, *, operation, current, cancelled, context_matches, host_paused, ready, foreground
):
    phases = {
        'observe': ('preparing', 'running'),
        'input': ('preparing', 'running'),
        'resume': ('paused',),
        'commit': ('running', 'draining'),
    }
    if not current or phase not in ('preparing', 'running', 'paused', 'draining'):
        return ScanAccess.INACTIVE
    if cancelled:
        return ScanAccess.CANCEL
    if not context_matches:
        return ScanAccess.CHANGED
    if phase not in phases[operation]:
        return ScanAccess.INACTIVE
    if host_paused:
        return ScanAccess.HOST_PAUSED
    if not ready:
        return ScanAccess.DISCONNECTED
    if not foreground:
        return ScanAccess.OUT_OF_FOCUS
    return ScanAccess.ALLOW
