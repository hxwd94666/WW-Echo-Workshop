# 冻结配装作业的候选与修订，在后台执行搜索和方案用例。
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ww_echo_workshop.application.compute_bridge import require_background
from ww_echo_workshop.application.loadout_diff import RoleLoadoutChange, plan_changes
from ww_echo_workshop.application.matching.scope import select_match_records
from ww_echo_workshop.application.operations import LinkedStop, checkpoint
from ww_echo_workshop.core.allocation.models import AllocationRequest, AllocationResult
from ww_echo_workshop.core.loadout import LoadoutSnapshot
from ww_echo_workshop.core.models import EchoRecord
from ww_echo_workshop.core.search_budget import SearchBudget
from ww_echo_workshop.resources.weights import WeightSnapshot


def allocation_candidates(records, item_ids, filters, *, queries, cancel_event=None):
    if filters is not None:
        if queries is None:
            raise ValueError('候选筛选需要显式的 Rust 库存查询服务')
        records = queries.filter_records(records, cancel_event=cancel_event, **filters)
    return select_match_records(records, item_ids, cancel_event=cancel_event)


@dataclass(frozen=True)
class AllocationScope:
    generation: int
    item_ids: frozenset[str] | None
    filters: Mapping[str, object] | None
    edit_plans: Mapping[str, str]
    replace_plans: Mapping[str, str]

    def __post_init__(self):
        if self.item_ids is not None:
            if isinstance(self.item_ids, (str, bytes)):
                raise ValueError('声骸选择必须是实例 ID 集合。')
            object.__setattr__(self, 'item_ids', frozenset(self.item_ids))
        for field in ('filters', 'edit_plans', 'replace_plans'):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, MappingProxyType(dict(value)))


@dataclass(frozen=True)
class AllocationSearchResult:
    result: AllocationResult
    records: tuple[EchoRecord, ...]
    weights: WeightSnapshot
    reservations: LoadoutSnapshot
    scope_generation: int
    change_details: tuple[RoleLoadoutChange, ...]


_CURRENT_WRITES = frozenset(
    (
        'save-protect',
        'protect',
        'release-current',
        'remove-current',
        'save-loadout',
        'release-items',
    )
)
_GUARDED_WRITES = _CURRENT_WRITES | {'save', 'mark', 'backup-loadout', 'remove-backup'}


class AllocationJobs:
    def __init__(self, inventory, queries, solver, scores, plans, *, weight_snapshot, stop_event):
        self._inventory = inventory
        self._queries = queries
        self._solver = solver
        self._scores = scores
        self._plans = plans
        self._weight_snapshot = weight_snapshot
        self._stop_event = stop_event

    def search(
        self,
        request: AllocationRequest,
        snapshot: WeightSnapshot,
        budget: SearchBudget,
        scope: AllocationScope,
        *,
        cancel_event=None,
    ) -> AllocationSearchResult:
        require_background()
        stop = LinkedStop(cancel_event, self._stop_event)
        checkpoint(stop)
        reservations = self._inventory.loadout_snapshot(request.profile_id, cancel_event=stop)
        records = allocation_candidates(
            reservations.records,
            scope.item_ids,
            scope.filters,
            queries=self._queries,
            cancel_event=stop,
        )
        result = self._solver.solve(
            records,
            request,
            inventory_revision=reservations.inventory_revision,
            snapshot=snapshot,
            cancel_event=stop,
            reservations=reservations,
            edit_plans=scope.edit_plans,
            replace_plans=scope.replace_plans,
            **budget.solver_kwargs(),
        )
        changes = plan_changes(result, reservations, snapshot, self._scores, stop)
        checkpoint(stop)
        return AllocationSearchResult(
            result, records, snapshot, reservations, scope.generation, changes
        )

    def plan(self, kind, arguments, scope: AllocationScope, *, cancel_event=None, progress=None):
        require_background()
        stop = LinkedStop(cancel_event, self._stop_event)
        arguments = dict(arguments)
        expected_revision = arguments.pop(
            '_guard_revision',
            arguments['result'].inventory_revision
            if kind == 'save'
            else arguments.get('inventory_revision'),
        )
        expected_weight = arguments.pop(
            '_guard_weight',
            arguments['result'].weight_version
            if kind == 'save'
            else arguments.get('weight_version'),
        )

        def before_commit():
            checkpoint(stop)
            # Current-loadout writes check both revisions in their transaction.
            # Their callback runs after the bump, so recheck only weight/cancel.
            if (
                kind not in _CURRENT_WRITES and self._inventory.revision != expected_revision
            ) or self._weight_snapshot().version != expected_weight:
                raise ValueError('库存/权重已变化，未提交方案/进度；旧文件保留，请重新评估')

        return self._plans.execute(
            kind,
            arguments,
            cancel_event=stop,
            progress=progress,
            before_commit=before_commit if kind in _GUARDED_WRITES else None,
            candidate_ids=scope.item_ids,
            candidate_filters=scope.filters,
            scope_generation=scope.generation,
            edit_plans=scope.edit_plans,
            replace_plans=scope.replace_plans,
        )
