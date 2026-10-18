# 执行声骸一对一快速评分。
"""Explicit single-record scoring through the same service used by matching."""

from dataclasses import dataclass, replace
import json

from echo_workshop.application.diagnostics import begin_job
from echo_workshop.application.scoring import ComparisonCancelled, ComparisonScore
from echo_workshop.application.compute_bridge import require_background
from echo_workshop.application.record_quality import quality_for
from echo_workshop.core.cancellation import checkpoint


@dataclass(frozen=True)
class QuickScoreResult:
    comparison: ComparisonScore
    notice: str = ''


def saved_requirements(preferences, profile_id, key):
    """Resolve one role's saved slot requirements without recomputing scores."""
    from echo_workshop.core.costs import cost_layouts_from_rows, DEFAULT_COST_LAYOUT
    from echo_workshop.core.requirements import requirements_from_rows, RoleRequirements

    data = json.loads(preferences) if preferences else {}
    row = data.get('profiles', {}).get(profile_id, {}).get('calculation', {})
    requirements = requirements_from_rows(row.get('role_requirements', []))
    costs = cost_layouts_from_rows(row.get('cost_layouts', []))
    spec = requirements.get(key)
    if key in costs:
        spec = (
            replace(spec, costs=costs[key]) if spec else RoleRequirements(costs[key], ((),) * 5, ())
        )
    elif spec is None:
        spec = RoleRequirements(DEFAULT_COST_LAYOUT, ((),) * 5, ())
    return spec


def quick_score_notice(record, comparison, preferences):
    """Explain explicit rejection only; neither change the score nor infer a fit."""
    return {
        'compatibility': '不符合已保存适配范围',
        'slot': '不符合已保存槽位要求',
    }.get(getattr(comparison, 'exclusion', ''), '')


class QuickScoreStale(ValueError):
    """The requested record or weight snapshot changed during calculation."""


@dataclass(frozen=True)
class QuickRecordState:
    revision: int
    quality: object = None


def refresh_record(runtime, record, quality, *, revision, cancel_event=None):
    """Read/validate in the existing dialog worker; unchanged facts need no RPC."""
    require_background()
    checkpoint(cancel_event)
    if runtime.stop_event.is_set() or runtime._closed:
        raise ComparisonCancelled('评分已取消。')
    if runtime.repository.revision != revision:
        raise QuickScoreStale('库存已变化，请等待最新记录。')
    try:
        current = runtime.repository.get(record.item_id)
    except KeyError:
        current = None
    if current is not None:
        if current.profile_id != record.profile_id:
            raise QuickScoreStale('声骸所属档案已变化，请重新选择。')
        if current == record and quality is not None:
            fact = quality_for(record, quality)
        else:
            _, _, facts = runtime.inventory_queries.review(
                ((current, 'item', ()),), cancel_event=cancel_event
            )
            fact = facts[0]
    else:
        fact = None
    checkpoint(cancel_event)
    if runtime.stop_event.is_set() or runtime._closed:
        raise ComparisonCancelled('评分已取消。')
    if runtime.repository.revision != revision:
        raise QuickScoreStale('库存已变化，请等待最新记录。')
    return QuickRecordState(revision, fact)


def validate_context(runtime, record, snapshot, cancel_event=None):
    if (
        (cancel_event is not None and cancel_event.is_set())
        or runtime.stop_event.is_set()
        or runtime._closed
    ):
        raise ComparisonCancelled('评分已取消。')
    try:
        current = runtime.repository.get(record.item_id)
    except KeyError:
        raise QuickScoreStale('声骸已删除，请关闭后重新选择。') from None
    if current is None:
        raise QuickScoreStale('声骸已删除，请关闭后重新选择。')
    if current != record:
        raise QuickScoreStale('声骸记录已变化，请重新评分。')
    if runtime.weights.active.version != snapshot.version:
        raise QuickScoreStale('评分权重已更新，请重新评分。')


def score_one(runtime, record, key, *, snapshot=None, preferences='', cancel_event=None):
    """Return a frozen score plus an explicit-fit notice, with no user writes.

    Main-stat normalization, custom weights, character element and compatibility
    all come from ScoreService, not a second quick-score formula.
    """
    snapshot = snapshot or runtime.weights.active
    job = begin_job('quick-score', {'weight_version': snapshot.version})
    try:
        job.stage('validation')
        if (
            not isinstance(key, (tuple, list))
            or len(key) != 2
            or any(not isinstance(value, str) or not value for value in key)
        ):
            raise ValueError('请选择角色和模版后再评分。')
        key = tuple(key)
        validate_context(runtime, record, snapshot, cancel_event)
        # Read the selected role's saved compatibility rules only. Malformed
        # saved matching settings are not silently treated as a fresh rule.
        from echo_workshop.application.recommendations import comparison_choices

        _, rules = comparison_choices(preferences, record.profile_id)
        revision = runtime.repository.revision
        job.stage('scoring')
        entries = runtime.scores.compare(
            (record,),
            (key,),
            inventory_revision=revision,
            rules={key: rules[key]} if key in rules else None,
            requirements={key: saved_requirements(preferences, record.profile_id, key)},
            cancel_event=cancel_event,
            snapshot=snapshot,
        )
        validate_context(runtime, record, snapshot, cancel_event)
        if len(entries) != 1 or entries[0].score is None:
            raise ValueError('此声骸尚未完整核对，请先核对并编辑。')
        result = QuickScoreResult(entries[0], quick_score_notice(record, entries[0], preferences))
        job.finish(
            'completed',
            'comparison_completed',
            counters={
                'records': 1,
                'roles': 1,
                'rating_available': int(getattr(entries[0].score, 'rating', None) is not None),
            },
        )
        return result
    except QuickScoreStale:
        job.finish('cancelled', counters={'stale': 1})
        raise
    except ComparisonCancelled:
        job.finish('cancelled', 'comparison_cancelled')
        raise
    except Exception as error:
        job.finish('failed', 'comparison_failed', error=error)
        raise
