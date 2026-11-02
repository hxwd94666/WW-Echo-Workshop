# 实时评分与自动战斗并存时的轻量判定函数。
import time


def live_digest(frame):
    try:
        return hash(frame[::32, ::32].tobytes())
    except (AttributeError, TypeError):
        return None


def analysis_key(analysis):
    """Stable identity for live HUD rows; one-frame OCR jitter must not flip scores."""

    def rows(values):
        return tuple(
            (getattr(row, 'stat_name', ''), round(float(getattr(row, 'value', 0.0)), 2))
            for row in values
        )

    return (
        getattr(analysis, 'layout', ''),
        getattr(analysis, 'selected_template', ''),
        tuple(getattr(analysis, 'selected_ids', ()) or ()),
        rows(getattr(analysis, 'main_rows', ())),
        rows(getattr(analysis, 'sub_rows', ())),
    )


def combat_task(task):
    """Whether the host is actively running automatic combat."""
    return (
        task is not None
        and type(task).__name__ in ('AutoCombatTask', 'BaseCombatTask')
        and not bool(getattr(task, 'paused', False))
    )


def combat_active(executor, active, hold_until):
    """Return combat state and the time until which OCR stays suspended."""
    now = time.monotonic()
    if combat_task(active):
        return True, now + 5.0
    if executor is not None:
        for task in getattr(executor, 'trigger_tasks', ()):
            if type(task).__name__ not in ('AutoCombatTask', 'BaseCombatTask'):
                continue
            if getattr(task, '_in_combat', False) or getattr(task, '_abyss_combat_active', False):
                return True, now + 5.0
    if now < hold_until:
        return True, hold_until
    return False, 0.0
