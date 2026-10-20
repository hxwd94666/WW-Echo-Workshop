# 验证穷举搜索界限、公平目标与分组优先级。
import hashlib
import itertools
import json
import random
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from tests.native_runtime import ScoreService
from echo_workshop.core.allocation.models import (
    AllocationCancelled,
    AllocationRequest,
    POLICY_VERSION,
    PRIORITY_POLICY_VERSION,
    RoleAllocation,
    RoleRequest,
    SlotRule,
    cents,
    set_name,
)
from tests.native_runtime import AllocationHarness
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.models import EchoRecord, StatValue, canonical_json
from echo_workshop.resources.weights import WeightProvider
from pathlib import Path
from tests.calculation.helpers import FakeScores, role, tuned
from types import SimpleNamespace
from uuid import UUID, uuid4
from tests.calculation.allocation_oracle import objective


PROFILE = str(UUID(int=1))


def search_record(index, name=None, sonata='甲', owner=''):
    return EchoRecord(
        PROFILE,
        1,
        (StatValue('攻击%', 18), StatValue('生命', 2280)),
        (
            StatValue('暴击', 6.3),
            StatValue('暴击伤害', 12.6),
            StatValue('攻击%', 6.4),
            StatValue('共鸣效率', 6.8),
            StatValue('共鸣技能', 6.4),
        ),
        item_id=str(UUID(int=index + 100)),
        name=name or f'样例-{index}',
        sonata=sonata,
        owner=owner,
        level=25,
        review='confirmed',
    )


def search_role(character, target=100, **changes):
    return replace(
        RoleRequest(
            character,
            'default',
            target,
            CompatibilityRule('衍射', ('甲', '乙')),
            (SlotRule(1),) * 5,
            cost_budget=5,
        ),
        **changes,
    )


def brute_force(records, roles, scores):
    """No production pool builder, pruning, seed, symmetry or bound is reused."""
    by_id = {r.item_id: r for r in records}
    candidates = []
    for spec in roles:
        options = [()]
        for values in itertools.combinations(records, 5):
            ids = tuple(r.item_id for r in values)
            if spec.distinct_names and len({set_name(r.name) for r in values}) < 5:
                continue
            counts = {
                name: sum(set_name(r.sonata) == set_name(name) for r in values)
                for name, _ in spec.set_requirements
            }
            if any(counts[name] < minimum for name, minimum in spec.set_requirements):
                continue
            if any(slot.fixed_item_id and slot.fixed_item_id not in ids for slot in spec.slots):
                continue
            pins = {
                slot.fixed_item_id
                for other in roles
                if other is not spec
                for slot in other.slots
                if slot.fixed_item_id
            }
            if pins.intersection(ids):
                continue
            options.append(ids)
        candidates.append(options)
    best = None
    for selection in itertools.product(*candidates):
        ids = [value for group in selection for value in group]
        if len(set(ids)) != len(ids):
            continue
        rows = []
        for spec, group in zip(roles, selection):
            values = tuple(
                round(scores.get((value, spec.character_id), 10) * 100) for value in group
            )
            moves = sum(
                not spec.owner_label or by_id[value].owner != spec.owner_label for value in group
            )
            rows.append(
                RoleAllocation(
                    *spec.key, '', group, values, sum(values), cents(spec.target), moves, ()
                )
            )
        candidate = objective(rows, exact=True)
        best = candidate if best is None else max(best, candidate)
    return best


class TestAllocationSearch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = WeightProvider(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def solve(self, records, roles, values, **kwargs):
        return AllocationHarness(FakeScores(values)).solve(
            records,
            AllocationRequest(PROFILE, roles),
            inventory_revision=10,
            snapshot=self.provider.active,
            **kwargs,
        )

    def test_energy_range_filters_complete_builds(self):
        records = [search_record(i) for i in range(8)]
        exact = search_role('1304', energy_regen_min=134, energy_regen_max=134)
        result = self.solve(records, (exact,), {})
        self.assertEqual(len(result.roles[0].item_ids), 5)
        impossible = search_role('1304', energy_regen_min=140, energy_regen_max=150)
        result = self.solve(records, (impossible,), {})
        self.assertEqual(result.roles[0].item_ids, ())

    def test_six_priority_bounds_match_independent_random_enumeration(self):
        randomizer = random.Random(24007)
        for case in range(12):
            records = tuple(
                search_record(
                    i,
                    name=f'样例-{i // 2}' if case % 3 == 0 else None,
                    sonata='甲' if i % 2 else '乙',
                    owner='A' if i % 3 else 'B',
                )
                for i in range(10)
            )
            roles = (
                search_role(
                    '1304',
                    randomizer.choice((77, 100, 135)),
                    owner_label='A',
                    set_requirements=(('甲', 2),) if case % 2 else (),
                ),
                search_role(
                    '1109',
                    randomizer.choice((60, 105, 150)),
                    owner_label='B',
                    set_requirements=(('乙', 2),) if case % 2 else (),
                ),
            )
            if case % 4 == 0:
                roles = (
                    replace(
                        roles[0],
                        slots=(SlotRule(1, fixed_item_id=records[0].item_id),) + (SlotRule(1),) * 4,
                    ),
                    roles[1],
                )
            values = {
                (r.item_id, spec.character_id): randomizer.randrange(100, 6500) / 100
                for r in records
                for spec in roles
            }
            with self.subTest(case=case):
                expected = brute_force(records, roles, values)
                result = self.solve(records, roles, values, seconds=10, max_nodes=200000)
                self.assertTrue(result.exhaustive)
                self.assertEqual(expected, objective(result.roles, exact=True))

    def test_bounded_seed_caps_reached_targets_before_total_score(self):
        records = tuple(search_record(i) for i in range(10))
        roles = (search_role('1304', 100), search_role('1109', 50))
        values = {
            (r.item_id, spec.character_id): (
                (100 if i < 5 else 10) if spec.character_id == '1304' else 10
            )
            for i, r in enumerate(records)
            for spec in roles
        }
        result = self.solve(records, roles, values, seconds=5, max_nodes=300)
        self.assertEqual(2, result.objective[0])
        self.assertEqual(1, result.objective[1])
        # v2 的第四项是封顶效用，不再奖励已达标角色继续提高达成率。
        self.assertEqual(15000, result.objective[3])
        self.assertEqual(10, len({i for r in result.roles for i in r.item_ids}))
        self.assertLessEqual(result.nodes, 300)
        if not result.exhaustive:
            self.assertEqual('node_limit', result.stop_reason)

    def test_low_ranked_required_set_uuid_is_not_top_k_truncated(self):
        records = tuple(search_record(i, sonata='乙' if i == 399 else '甲') for i in range(400))
        roles = (search_role('1304', 100, set_requirements=(('乙', 1),)),)
        values = {(r.item_id, '1304'): 1 if i == 399 else 100 for i, r in enumerate(records)}
        result = self.solve(records, roles, values, seconds=5, max_nodes=5000)
        self.assertEqual(5, len(result.roles[0].item_ids))
        self.assertIn(records[-1].item_id, result.roles[0].item_ids)

    def test_same_fingerprint_uuid_instances_survive_seed_and_exact_search(self):
        base = search_record(0)
        records = tuple(replace(base, item_id=str(UUID(int=i + 100))) for i in range(10))
        roles = (
            search_role('1304', 50, distinct_names=False),
            search_role('1109', 50, distinct_names=False),
        )
        result = self.solve(records, roles, {})
        self.assertTrue(result.exhaustive)
        self.assertEqual(10, len({i for r in result.roles for i in r.item_ids}))
        self.assertEqual(2, result.objective[0])

    def test_cancel_after_matrix_preparation_discards_result(self):
        records = tuple(search_record(i) for i in range(20))
        event = threading.Event()

        class CancelScores(FakeScores):
            def compare(self, *args, **kwargs):
                result = super().compare(*args, **kwargs)
                event.set()
                return result

        with self.assertRaises(AllocationCancelled):
            AllocationHarness(CancelScores({})).solve(
                records,
                AllocationRequest(PROFILE, (search_role('1304'), search_role('1109'))),
                inventory_revision=10,
                snapshot=self.provider.active,
                cancel_event=event,
            )

    def test_seed_cannot_escape_shared_node_or_time_budget(self):
        records = tuple(search_record(i) for i in range(20))
        roles = (search_role('1304'), search_role('1109'))
        result = self.solve(records, roles, {}, max_nodes=1)
        self.assertFalse(result.exhaustive)
        self.assertEqual(('node_limit', 1), (result.stop_reason, result.nodes))
        self.assertTrue(all(len(r.item_ids) in (0, 5) for r in result.roles))
        result = self.solve(records, roles, {}, seconds=1e-9)
        self.assertEqual('time_limit', result.stop_reason)
        self.assertFalse(result.exhaustive)


class TestPriorityAllocation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.provider = WeightProvider(self.temp.name)
        self.profile = str(uuid4())
        self.roles = tuple(
            role(character, target=100, slots=(SlotRule(1),) * 5, cost_budget=5)
            for character in ('1304', '1109')
        )
        self.records = tuple(
            tuned(self.profile, item_id=str(UUID(int=100 + i)), name=f'声骸-{i}') for i in range(10)
        )
        self.request = AllocationRequest(
            self.profile, self.roles, priority_groups=((self.roles[0].key,), (self.roles[1].key,))
        )

    def tearDown(self):
        self.temp.cleanup()

    def solve(self, request=None, records=None, scores=None, **kwargs):
        return AllocationHarness(scores or FakeScores({})).solve(
            self.records if records is None else records,
            request or self.request,
            inventory_revision=7,
            snapshot=self.provider.active,
            **kwargs,
        )

    def test_request_schema_roundtrip_invalid_partitions_and_legacy_hash(self):
        legacy = replace(self.request, priority_groups=())
        expected = asdict(legacy)
        expected.pop('priority_groups')
        expected['roles'] = list(expected['roles'])
        self.assertEqual(expected, legacy.to_dict())
        self.assertEqual(
            hashlib.sha256(canonical_json(expected).encode()).hexdigest(), legacy.version
        )
        self.assertEqual(
            self.request,
            AllocationRequest.from_dict(json.loads(canonical_json(self.request.to_dict()))),
        )
        self.assertNotEqual(self.request.version, legacy.version)
        for groups in (
            [],
            ((),),
            ((self.roles[0].key,),),
            ((self.roles[0].key, self.roles[0].key),),
            ((self.roles[0].key,), (('missing', 'default'),)),
        ):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                replace(self.request, priority_groups=groups)
        invalid = self.request.to_dict()
        invalid['priority_groups'] = ['1304']
        with self.assertRaises(ValueError):
            AllocationRequest.from_dict(invalid)
        result = self.solve(legacy, max_nodes=1)
        self.assertEqual(POLICY_VERSION, result.policy_version)
        self.assertNotIn('priority_groups', result.to_dict()['request'])

    def test_equal_group_reuses_original_fair_objective_and_scores(self):
        scores = FakeScores(
            {
                (record.item_id, character): (35 if index < 5 else 15)
                if character == '1304'
                else (34 if index < 5 else 1)
                for index, record in enumerate(self.records)
                for character in ('1304', '1109')
            }
        )
        legacy = self.solve(replace(self.request, priority_groups=()), scores=scores)
        equal = self.solve(
            replace(self.request, priority_groups=(tuple(r.key for r in self.roles),)),
            scores=scores,
        )
        self.assertTrue(legacy.exhaustive)
        self.assertTrue(equal.exhaustive)
        self.assertEqual(objective(legacy.roles, exact=True), objective(equal.roles, exact=True))
        self.assertEqual(
            [(r.item_ids, r.scores, r.total_cents) for r in legacy.roles],
            [(r.item_ids, r.scores, r.total_cents) for r in equal.roles],
        )
        self.assertEqual(PRIORITY_POLICY_VERSION, equal.policy_version)

    def test_earlier_group_keeps_best_five_and_never_reuses_physical_uuid(self):
        scores = FakeScores(
            {
                (record.item_id, character): (90 if index < 5 else 10)
                for index, record in enumerate(self.records)
                for character in ('1304', '1109')
            }
        )
        result = self.solve(scores=scores)
        expected = {record.item_id for record in self.records[:5]}
        self.assertEqual(expected, set(result.roles[0].item_ids))
        self.assertEqual(10, len({item for row in result.roles for item in row.item_ids}))
        reversed_result = self.solve(
            replace(self.request, priority_groups=tuple(reversed(self.request.priority_groups))),
            scores=scores,
        )
        self.assertEqual(expected, set(reversed_result.roles[1].item_ids))
        # Output stays in original stable role order, independent of priority order.
        self.assertEqual(
            [r.key for r in self.roles],
            [(r.character_id, r.modal_id) for r in reversed_result.roles],
        )

    def test_later_group_fixed_uuid_is_reserved_before_earlier_group(self):
        fixed = self.records[0].item_id
        later = replace(
            self.roles[1], slots=(SlotRule(1, fixed_item_id=fixed),) + (SlotRule(1),) * 4
        )
        scores = FakeScores(
            {
                (record.item_id, character): 100 - index
                for index, record in enumerate(self.records)
                for character in ('1304', '1109')
            }
        )
        result = self.solve(replace(self.request, roles=(self.roles[0], later)), scores=scores)
        self.assertNotIn(fixed, result.roles[0].item_ids)
        self.assertEqual(fixed, result.roles[1].item_ids[0])
        self.assertEqual(10, len({item for row in result.roles for item in row.item_ids}))
        # An infeasible earlier group also retains its fixed UUID reservation.
        infeasible = replace(
            self.roles[0],
            slots=(SlotRule(1, ('生命%',), fixed_item_id=fixed),) + (SlotRule(1),) * 4,
        )
        after_failed = self.solve(
            replace(self.request, roles=(infeasible, self.roles[1])), scores=scores
        )
        self.assertEqual((), after_failed.roles[0].item_ids)
        self.assertNotIn(fixed, after_failed.roles[1].item_ids)
        missing = replace(
            later, slots=(SlotRule(1, fixed_item_id=str(uuid4())),) + (SlotRule(1),) * 4
        )
        with self.assertRaisesRegex(ValueError, '当前档案'):
            self.solve(replace(self.request, roles=(self.roles[0], missing)))

    def test_node_budget_uncomputed_later_group_is_savable_and_cancel_propagates(self):
        result = self.solve(scores=ScoreService(self.provider), max_nodes=1)
        self.assertEqual(
            (1, 'node_limit', False), (result.nodes, result.stop_reason, result.exhaustive)
        )
        self.assertEqual((), result.roles[1].item_ids)
        self.assertIn('尚未计算本组', result.roles[1].reasons[0])
        event = threading.Event()
        event.set()
        with self.assertRaises(AllocationCancelled):
            self.solve(cancel_event=event)

    def test_groups_share_one_native_request_and_absolute_deadline(self):
        from unittest.mock import patch
        from tests.native_runtime import _matrix_jobs

        with patch.object(_matrix_jobs, 'submit', wraps=_matrix_jobs.submit) as submit:
            result = self.solve(seconds=1e-9)
        self.assertEqual(1, submit.call_count)
        self.assertEqual('time_limit', result.stop_reason)
        self.assertGreaterEqual(result.elapsed, 1e-9)
        self.assertFalse(result.exhaustive)
        self.assertEqual(0, result.nodes)
        self.assertTrue(all(not row.item_ids for row in result.roles))

    def test_reservation_edit_replace_and_original_validation_survive_groups(self):
        claims = tuple(
            (record.item_id, '1304' if index < 5 else '1109')
            for index, record in enumerate(self.records)
        )
        current = tuple(
            dict(
                character_id=role.character_id,
                modal_id='default',
                plan_id=str(UUID(int=900 + index)),
                issues=(),
            )
            for index, role in enumerate(self.roles)
        )
        snapshot = SimpleNamespace(
            profile_id=self.profile, inventory_revision=7, owners=dict(claims), current=current
        )
        # Both explicit edit and replacement subsets are passed only to their groups.
        result = self.solve(
            reservations=snapshot,
            edit_plans={'1304': current[0]['plan_id']},
            replace_plans={'1109': current[1]['plan_id']},
        )
        self.assertEqual([5, 5], [len(row.item_ids) for row in result.roles])
        self.assertEqual({r.item_id for r in self.records[:5]}, set(result.roles[0].item_ids))
        protected = self.solve(reservations=snapshot)
        self.assertEqual([0, 0], [len(row.item_ids) for row in protected.roles])
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.solve(reservations=snapshot, edit_plans={'1109': str(uuid4())})
        with self.assertRaisesRegex(ValueError, '保持原模版'):
            self.solve(
                reservations=SimpleNamespace(
                    **{
                        **snapshot.__dict__,
                        'current': (current[0], {**current[1], 'modal_id': 'other'}),
                    }
                ),
                edit_plans={'1109': current[1]['plan_id']},
            )
        with self.assertRaisesRegex(ValueError, '库存快照'):
            self.solve(
                reservations=SimpleNamespace(**{**snapshot.__dict__, 'inventory_revision': 8})
            )
