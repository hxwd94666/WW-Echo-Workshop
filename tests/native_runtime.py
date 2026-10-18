# 为开发测试显式注入构建目录内的 Rust 引擎，不改变生产路径。
import atexit
from pathlib import Path

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.scoring import ScoreService as ProductionScoreService
from echo_workshop.application.recommendations import (
    RecommendationService as ProductionRecommendations,
)
from echo_workshop.application.allocation import AllocationService
from echo_workshop.application.observations import ObservationService as ProductionObservations
from echo_workshop.application.custom_weights import CustomWeightService
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.pins import PinCheckService, pin_error_text
from echo_workshop.resources.weights import WeightProvider as ProductionWeights
from echo_workshop.core.allocation.models import cents
from echo_workshop.core.filters import InventoryFilters as ProductionInventoryFilters

# Storage tests explicitly use the same native test process owner as score fixtures.
from functools import partial
from echo_workshop.storage.inventory import EchoRepository as ProductionRepository
from echo_workshop.storage.plans import (
    AllocationStore as ProductionStore,
    validate_plan as production_validate_plan,
)
from echo_workshop.storage.workspaces import WorkspaceManager as ProductionWorkspaces
from echo_workshop.storage.control_repair import ControlRepair as ProductionRepair
from echo_workshop.application.repair import RepairService as ProductionRepairService
from echo_workshop.storage.workspace_bundle import (
    _plan as production_plan,
)
from echo_workshop.storage.profile_transfer import inspect_profile_archive as production_inspect
from echo_workshop.storage.loadouts import validate_current_plan_references as production_references
from echo_workshop.application.loadout_actions import (
    source_payload as production_source,
    clone_loadout_payload as production_clone,
)
from echo_workshop.application.replacement import (
    replacement_candidates as production_candidates,
    replacement_preview as production_preview,
)


ENGINE = Path(__file__).resolve().parents[1] / 'build/native/release/echo-workshop-compute.exe'
_jobs = ComputeJobs(ENGINE)
_matrix_jobs = ComputeJobs(ENGINE, fault_tests=True)


def _close():
    _jobs.close()
    _jobs.wait_closed()
    _matrix_jobs.close()
    _matrix_jobs.wait_closed()


atexit.register(_close)


class ScoreService(ProductionScoreService):
    def __init__(self, provider, **kwargs):
        super().__init__(provider, compute=_jobs, **kwargs)


class RecommendationService(ProductionRecommendations):
    def __init__(self):
        super().__init__(compute=_jobs)


class ObservationService(ProductionObservations):
    def __init__(self):
        super().__init__(compute=_jobs)


_observations = ObservationService()
_custom_weights = CustomWeightService(compute=_jobs)
_inventory_queries = InventoryQueryService(compute=_jobs)
# 页面筛选接线与库存选择契约共用同一原生查询服务。
inventory_queries = _inventory_queries
pin_checks = PinCheckService(compute=_jobs)


def validate_fixed_items(requirements, records):
    errors = pin_checks.check({('test', 'default'): requirements}, records)
    if errors:
        _, slot, code = errors[0]
        raise ValueError(pin_error_text(slot, code))


def prepare_pin_catalog(records, **kwargs):
    from echo_workshop.application.pins import prepare_pin_catalog as prepare

    return prepare(records, queries=_inventory_queries, **kwargs)


def filter_records(records, **kwargs):
    return _inventory_queries.filter_records(records, **kwargs)


def calculate_echo_score(template, cost, cost_key, main_rows, sub_rows):
    """Test-only raw native entry; no Python scoring or prepared backend."""
    from echo_workshop.application.compute_bridge import plain, score_result

    if template is None:
        raise ValueError('评分需要明确的冻结模版')
    payload = dict(
        templates={'fixture': plain(template)},
        items=[
            dict(
                template_key='fixture',
                cost=int(cost),
                cost_key=cost_key,
                main_rows=[dict(stat_name=r.stat_name, value=r.value) for r in main_rows],
                sub_rows=[dict(stat_name=r.stat_name, value=r.value) for r in sub_rows],
            )
        ],
    )
    value = _jobs.submit('score_batch', lambda: payload).result(timeout=12)
    return score_result(value['items'][0])


def substat_tier_label(stat_name, value):
    from echo_workshop.core.models import StatValue

    result = _observations.evaluate(
        (), (StatValue(stat_name, value),), selections=(), weight_version='tier-fixture', cost=1
    )
    tier = result.tiers[0]
    return f'{tier.index}档' if tier else ''


class WeightProvider(ProductionWeights):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, custom_evaluator=_custom_weights, **kwargs)


def editable_values(original):
    return _custom_weights.editable_values(
        original, identity=('test', 'default'), weight_version='test-template'
    )


def build_custom_template(original, main, sub):
    return _custom_weights.build(
        original, main, sub, identity=('test', 'default'), weight_version='test-template'
    )


def _rateable(template):
    """Reject malformed or NaN tables before they reach the JSON transport."""
    from collections.abc import Mapping

    from echo_workshop.core.rating import validate_rating_tables

    if not isinstance(template, Mapping):
        return False
    try:
        validate_rating_tables(template)
    except (ValueError, TypeError):
        return False
    return True


def single_rating(result_or_points, template, *, cost=None):
    """Test-only native single rating; validates inputs the engine rejects."""
    import math

    from echo_workshop.application.compute_bridge import plain, rating_result

    points = getattr(result_or_points, 'current_score', result_or_points)
    cost = getattr(result_or_points, 'cost', None) if cost is None else cost
    if type(points) not in (int, float) or isinstance(points, bool) or not math.isfinite(points):
        return None
    if type(cost) is not int or cost not in (1, 3, 4) or not _rateable(template):
        return None
    payload = {'items': [{'template': plain(template), 'cost': cost, 'score': float(points)}]}
    value = _jobs.submit('rating_batch', lambda: payload).result(timeout=12)
    return rating_result(value['items'][0])


def loadout_rating(scores_cents, template):
    """Test-only native five-piece rating over frozen cent scores."""
    from echo_workshop.application.compute_bridge import plain, rating_result

    if (
        not isinstance(scores_cents, (tuple, list))
        or len(scores_cents) != 5
        or any(type(value) is not int or value < 0 for value in scores_cents)
        or not _rateable(template)
    ):
        return None
    payload = {
        'items': [
            {'template': plain(template), 'scores_cents': [int(value) for value in scores_cents]}
        ]
    }
    value = _jobs.submit('rating_batch', lambda: payload).result(timeout=12)
    return rating_result(value['items'][0])


class NativeStandard:
    """Test-only standard view: thresholds come from the native engine."""

    def __init__(self, template, cost):
        self.template, self.cost = template, cost
        rating = single_rating(0, template, cost=cost)
        self.thresholds = rating.thresholds if rating is not None else ()

    def rate(self, score):
        return single_rating(score, self.template, cost=self.cost)


def single_standard(template, cost):
    if not isinstance(template, dict) or type(cost) is not int or cost not in (1, 3, 4):
        return None
    standard = NativeStandard(template, cost)
    return standard if standard.thresholds else None


def rating_template(snapshot_or_payload_snapshot, character, modal='default'):
    """Test-only frozen template resolution; grading still runs natively."""
    from echo_workshop.core.rating import project_grade_fields

    method = getattr(snapshot_or_payload_snapshot, 'scoring_template', None)
    if callable(method):
        try:
            return method(character, modal)
        except (ValueError, TypeError, KeyError):
            return None
    if not isinstance(snapshot_or_payload_snapshot, dict):
        return None
    try:
        template = snapshot_or_payload_snapshot['templates'][character][modal]
        state = snapshot_or_payload_snapshot.get('local_state')
        upstream = (
            state['upstream']['templates'].get(character, {}).get(modal)
            if isinstance(state, dict)
            else None
        )
        return project_grade_fields(template, upstream)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def filter_accepts(rule, record, kind='item', reserved=False):
    """Test-only native single-row filter decision through the shared engine."""
    from echo_workshop.application.inventory_view import ViewMetadata

    metadata = ViewMetadata('', '1' if reserved else '')
    indices = _inventory_queries.filter_view(((record, kind, metadata),), rule)
    return bool(indices)


class InventoryFilters(ProductionInventoryFilters):
    """测试用规则对象：筛选求值走原生引擎，构造与校验仍是现行契约。"""

    def accepts(self, record, kind='item', *, reserved=False):
        return filter_accepts(self, record, kind, reserved)


def analyze_echo_stats(*args, **kwargs):
    from echo_workshop.adapters.okww.stat_overlay import analyze_echo_stats as analyze
    from echo_workshop.application.live_selection import LiveSelection
    from echo_workshop.application.templates import DEFAULT_TEMPLATE
    from echo_workshop.resources.weights import WeightSnapshot

    snapshot = WeightSnapshot.builtin()

    def resolver(boxes, kind):
        key = ('default', 'default')
        return LiveSelection(
            key,
            DEFAULT_TEMPLATE,
            snapshot.scoring_template(*key),
            snapshot.version,
            '固定角色 / 模态',
        )

    kwargs.setdefault('template_resolver', resolver)
    return analyze(
        *args, **kwargs, observation_service=_observations, weight_version=snapshot.version
    )


def score_selected_template(label, cost, cost_key, main_rows, sub_rows):
    from echo_workshop.application.templates import TEMPLATE_OPTIONS
    from echo_workshop.resources.weights import WeightSnapshot

    character, modal, template = TEMPLATE_OPTIONS[label]
    return _observations.evaluate(
        main_rows,
        sub_rows,
        selections=((character, modal, template),),
        weight_version=WeightSnapshot.builtin().version,
        cost=cost,
        cost_key=cost_key,
    ).score_for((character, modal))


class _MatrixTransport:
    """Only test fixtures may supply declared scores; Rust still performs all search."""

    def __init__(self, scores, records, snapshot):
        self.scores, self.records, self.snapshot = scores, records, snapshot

    def submit(self, operation, builder, **options):
        assert operation == 'allocate'

        def payload():
            data = builder()
            selections = [(r['character_id'], r['modal_id']) for r in data['request']['roles']]
            cells = self.scores.compare(
                self.records,
                selections,
                snapshot=self.snapshot,
                inventory_revision=data['inventory_revision'],
                cancel_event=options.get('cancel_event'),
            )
            values = {
                (c.item_id, c.character_id, c.modal_id): cents(c.score.current_score)
                if c.score is not None and c.recommendation == 'match'
                else None
                for c in cells
            }
            data['matrix_cents'] = [
                [values.get((r.item_id, *key)) for r in self.records] for key in selections
            ]
            return data

        return _matrix_jobs.submit('allocate_matrix', payload, **options)


class AllocationHarness:
    def __init__(self, scores):
        self.scores = scores

    def solve(self, records, request, **options):
        records = tuple(records)
        compute = (
            self.scores.compute
            if isinstance(self.scores, ProductionScoreService)
            else _MatrixTransport(self.scores, records, options['snapshot'])
        )
        return AllocationService(self.scores, compute=compute).solve(records, request, **options)


def allocation_precheck(records, request, snapshot, **options):
    reservations = options.get('reservations')
    revision = reservations.inventory_revision if reservations is not None else 0
    return AllocationService(None, compute=_jobs).precheck(
        records, request, snapshot=snapshot, inventory_revision=revision, **options
    )


plan_evaluator = AllocationService(None, compute=_jobs)


class EchoRepository(ProductionRepository):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('plan_evaluator', plan_evaluator)
        super().__init__(*args, **kwargs)


class AllocationStore(ProductionStore):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('plan_evaluator', plan_evaluator)
        super().__init__(*args, **kwargs)


class WorkspaceManager(ProductionWorkspaces):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('plan_evaluator', plan_evaluator)
        super().__init__(*args, **kwargs)


class ControlRepair(ProductionRepair):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('plan_evaluator', plan_evaluator)
        super().__init__(*args, **kwargs)


class RepairService(ProductionRepairService):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('compute', _jobs)
        super().__init__(*args, **kwargs)


validate_plan = partial(production_validate_plan, plan_evaluator=plan_evaluator)
validate_current_plan_references = partial(production_references, plan_evaluator=plan_evaluator)
_plan = partial(production_plan, plan_evaluator=plan_evaluator)
inspect_profile_archive = partial(production_inspect, plan_evaluator=plan_evaluator)
source_payload = partial(production_source, plan_evaluator=plan_evaluator)
clone_loadout_payload = partial(production_clone, plan_evaluator=plan_evaluator)
replacement_candidates = partial(production_candidates, plan_evaluator=plan_evaluator)
replacement_preview = partial(production_preview, plan_evaluator=plan_evaluator)
