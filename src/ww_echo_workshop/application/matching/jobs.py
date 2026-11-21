# 执行冻结的匹配候选读取与显式评分作业，不依赖界面或宿主。
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ww_echo_workshop.application.compute_bridge import require_background
from ww_echo_workshop.application.match_projection import MatchProjections
from ww_echo_workshop.application.matching.feedback import (
    MatchFeedback,
    feedback_from_code,
    validate_match_requirements,
)
from ww_echo_workshop.application.pins import PinCatalog, prepare_pin_catalog
from ww_echo_workshop.application.scoring import ComparisonScore
from ww_echo_workshop.application.operations import LinkedStop
from ww_echo_workshop.core.cancellation import checkpoint
from ww_echo_workshop.core.compatibility import CompatibilityRule
from ww_echo_workshop.core.models import EchoRecord
from ww_echo_workshop.core.requirements import RoleRequirements
from ww_echo_workshop.resources.weights import WeightSnapshot


RoleKey = tuple[str, str]


@dataclass(frozen=True)
class MatchingRequest:
    profile_id: str | None
    snapshot: WeightSnapshot
    defaults_version: str
    selections: tuple[RoleKey, ...]
    selected_ids: frozenset[str] | None
    filters: Mapping[str, object]
    rules: Mapping[RoleKey, CompatibilityRule]
    requirements: Mapping[RoleKey, RoleRequirements]
    catalogue_only: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'selections', tuple(tuple(key) for key in self.selections))
        if self.selected_ids is not None:
            # Keep an explicit empty selection distinct from a dynamic scope.
            if isinstance(self.selected_ids, (str, bytes)):
                raise ValueError('声骸选择必须是实例 ID 集合。')
            object.__setattr__(self, 'selected_ids', frozenset(self.selected_ids))
        for name in ('filters', 'rules', 'requirements'):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class MatchingResult:
    request: MatchingRequest
    revision: int
    records: tuple[EchoRecord, ...]
    catalog: tuple[EchoRecord, ...]
    claims: Mapping[str, str]
    scores: tuple[ComparisonScore, ...]
    labels: tuple[str, ...]
    projections: MatchProjections | None
    fixed_catalog: PinCatalog
    feedback: MatchFeedback | None


class MatchingService:
    def __init__(self, inventory, queries, scores, projections, pin_checks, stop_event):
        self._inventory = inventory
        self._queries = queries
        self._scores = scores
        self._projections = projections
        self._pin_checks = pin_checks
        self._stop_event = stop_event

    def run(self, request: MatchingRequest, *, cancel_event=None) -> MatchingResult:
        require_background()
        stop = LinkedStop(cancel_event, self._stop_event)
        checkpoint(stop)
        revision, records, claims = self._inventory.inventory_snapshot(
            request.profile_id, cancel_event=stop, include_loadouts=True
        )
        if not request.catalogue_only:
            validate_match_requirements(
                request.requirements, records, request.snapshot, stop, pin_checks=self._pin_checks
            )
        scope = self._queries.matching_scope(
            records,
            selected_ids=request.selected_ids,
            **request.filters,
            protected_ids=claims,
            cancel_event=stop,
        )
        records, catalog = tuple(scope.records), tuple(scope.catalog)
        scores, projections, labels = (), None, ()
        if not request.catalogue_only:
            labels = tuple(
                f'{request.snapshot.template(*key)["name"]}\n{key[0]}/{key[1]}'
                for key in request.selections
            )
            if request.selections:
                scores = tuple(
                    self._scores.compare(
                        records,
                        request.selections,
                        inventory_revision=revision,
                        rules=request.rules,
                        requirements=request.requirements,
                        cancel_event=stop,
                        snapshot=request.snapshot,
                    )
                )
                projections = self._projections.project(
                    records, request.selections, scores, cancel_event=stop
                )
        fixed = prepare_pin_catalog(
            catalog, queries=self._queries, usable_only=True, cancel_event=stop
        )
        checkpoint(stop)
        return MatchingResult(
            request,
            revision,
            records,
            catalog,
            MappingProxyType(dict(claims or {})),
            scores,
            labels,
            projections,
            fixed,
            feedback_from_code(scope.feedback_code),
        )
