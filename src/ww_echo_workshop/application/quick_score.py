# 执行声骸一对一快速评分。
"""Explicit single-record scoring through the same service used by matching."""

from dataclasses import dataclass, replace
import json

from ww_echo_workshop.application.diagnostics import begin_job
from ww_echo_workshop.application.scoring import ComparisonCancelled, ComparisonScore
from ww_echo_workshop.application.compute_bridge import require_background
from ww_echo_workshop.application.record_quality import quality_for
from ww_echo_workshop.core.cancellation import checkpoint


@dataclass(frozen=True)
class QuickScoreResult:
    comparison: ComparisonScore
    notice: str = ''


def saved_requirements(preferences, profile_id, key):
    """Resolve one role's saved slot requirements without recomputing scores."""
    from ww_echo_workshop.core.costs import cost_layouts_from_rows, DEFAULT_COST_LAYOUT
    from ww_echo_workshop.core.requirements import requirements_from_rows, RoleRequirements

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


class QuickScoreService:
    def __init__(self, inventory, queries, scores, *, weight_snapshot, closed):
        self._inventory = inventory
        self._queries = queries
        self._scores = scores
        self.weight_snapshot = weight_snapshot
        self.closed = closed

    def try_revision(self):
        return self._inventory.try_revision()

    def verify_selection(self, record):
        try:
            current = self._inventory.get(record.item_id)
        except KeyError:
            raise ValueError('声骸已删除，请刷新后重新选择。') from None
        if current != record:
            raise ValueError('声骸记录已变化，请刷新后重新选择。')

    def refresh_record(self, record, quality, *, revision, cancel_event=None):
        """Read/validate in the existing dialog worker; unchanged facts need no RPC."""
        require_background()
        checkpoint(cancel_event)
        if self.closed():
            raise ComparisonCancelled('评分已取消。')
        if self._inventory.revision != revision:
            raise QuickScoreStale('库存已变化，请等待最新记录。')
        try:
            current = self._inventory.get(record.item_id)
        except KeyError:
            current = None
        if current is not None:
            if current.profile_id != record.profile_id:
                raise QuickScoreStale('声骸所属档案已变化，请重新选择。')
            if current == record and quality is not None:
                fact = quality_for(record, quality)
            else:
                _, _, facts = self._queries.review(
                    ((current, 'item', ()),), cancel_event=cancel_event
                )
                fact = facts[0]
        else:
            fact = None
        checkpoint(cancel_event)
        if self.closed():
            raise ComparisonCancelled('评分已取消。')
        if self._inventory.revision != revision:
            raise QuickScoreStale('库存已变化，请等待最新记录。')
        return QuickRecordState(revision, fact)

    def validate_context(self, record, snapshot, cancel_event=None):
        if (cancel_event is not None and cancel_event.is_set()) or self.closed():
            raise ComparisonCancelled('评分已取消。')
        try:
            current = self._inventory.get(record.item_id)
        except KeyError:
            raise QuickScoreStale('声骸已删除，请关闭后重新选择。') from None
        if current is None:
            raise QuickScoreStale('声骸已删除，请关闭后重新选择。')
        if current != record:
            raise QuickScoreStale('声骸记录已变化，请重新评分。')
        if self.weight_snapshot().version != snapshot.version:
            raise QuickScoreStale('评分权重已更新，请重新评分。')

    def score_one(self, record, key, *, snapshot=None, preferences='', cancel_event=None):
        """Return a frozen score plus an explicit-fit notice, with no user writes.

        Main-stat normalization, custom weights, character element and compatibility
        all come from ScoreService, not a second quick-score formula.
        """
        snapshot = snapshot or self.weight_snapshot()
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
            self.validate_context(record, snapshot, cancel_event)
            # Read the selected role's saved compatibility rules only. Malformed
            # saved matching settings are not silently treated as a fresh rule.
            from ww_echo_workshop.application.recommendations import comparison_choices

            _, rules = comparison_choices(preferences, record.profile_id)
            revision = self._inventory.revision
            job.stage('scoring')
            entries = self._scores.compare(
                (record,),
                (key,),
                inventory_revision=revision,
                rules={key: rules[key]} if key in rules else None,
                requirements={key: saved_requirements(preferences, record.profile_id, key)},
                cancel_event=cancel_event,
                snapshot=snapshot,
            )
            self.validate_context(record, snapshot, cancel_event)
            if len(entries) != 1 or entries[0].score is None:
                raise ValueError('此声骸尚未完整核对，请先核对并编辑。')
            result = QuickScoreResult(
                entries[0], quick_score_notice(record, entries[0], preferences)
            )
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
