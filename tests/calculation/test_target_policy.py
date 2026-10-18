# 验证零目标、达标让分、搜索上界与退休政策原件保护。
from dataclasses import replace
from fractions import Fraction
import itertools
import random
import tempfile
import unittest
from tests.calculation.allocation_oracle import objective

from tests.native_runtime import ScoreService
from echo_workshop.core.allocation.models import (
    AllocationRequest,
    POLICY_VERSION,
    PRIORITY_POLICY_VERSION,
)
from tests.native_runtime import AllocationHarness
from echo_workshop.core.models import canonical_json
from echo_workshop.resources.weights import WeightProvider
from echo_workshop.resources.role_defaults import default_sets, default_rule
from tests.native_runtime import AllocationStore, validate_plan
from tests.calculation.helpers import FakeScores, tuned, role
from tests.calculation.test_allocation_search import PROFILE, search_record, search_role


def enumerate_objective(records, roles, values):
    """Independent enumeration, including empty builds; no production objective/bounds."""
    options = [(), *itertools.combinations(records, 5)]
    best = None
    for selections in itertools.product(options, repeat=len(roles)):
        ids = [r.item_id for selection in selections for r in selection]
        if len(ids) != len(set(ids)):
            continue
        totals = [
            sum(round(values[r.item_id, spec.character_id] * 100) for r in selection)
            for spec, selection in zip(roles, selections)
        ]
        targets = [round(spec.target * 100) for spec in roles]
        positive = [(total, target) for total, target in zip(totals, targets) if target]
        score = (
            sum(total >= target for total, target in positive),
            min(
                (min(Fraction(total, target), 1) for total, target in positive), default=Fraction(1)
            ),
            sum(len(selection) == 5 for selection in selections),
            sum(min(total, target) if target else total for total, target in zip(totals, targets)),
            sum(totals),
            -len(ids),
        )
        best = score if best is None else max(best, score)
    return best


class TestTargetPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = WeightProvider(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def solve(self, records, roles, values, groups=()):
        return AllocationHarness(FakeScores(values)).solve(
            records,
            AllocationRequest(PROFILE, roles, priority_groups=groups),
            inventory_revision=0,
            snapshot=self.provider.active,
            seconds=10,
            max_nodes=200000,
        )

    def test_random_zero_and_positive_targets_match_independent_enumeration(self):
        rng = random.Random(20261016)
        records = tuple(search_record(i) for i in range(10))
        for targets in ((0, 0), (50, 0), (0, 90), (40, 40), (400, 0), (0, 400)):
            roles = (search_role('1304', targets[0]), search_role('1109', targets[1]))
            values = {
                (r.item_id, spec.character_id): rng.randrange(1, 80)
                for r in records
                for spec in roles
            }
            result = self.solve(records, roles, values)
            with self.subTest(targets=targets):
                self.assertTrue(result.exhaustive)
                self.assertEqual(
                    enumerate_objective(records, roles, values), objective(result.roles, exact=True)
                )
                self.assertEqual(POLICY_VERSION, result.policy_version)

    def test_reached_target_yields_good_echoes_to_uncapped_peer(self):
        records = tuple(search_record(i) for i in range(10))
        roles = (search_role('1304', 50), search_role('1109', 0))
        # A could take 500 points but needs only 50; B benefits from all five good echoes.
        values = {
            (r.item_id, spec.character_id): (100 if spec.character_id == '1304' else 80)
            if i < 5
            else 10
            for i, r in enumerate(records)
            for spec in roles
        }
        result = self.solve(records, roles, values)
        self.assertTrue(result.exhaustive)
        self.assertEqual([5000, 40000], [r.total_cents for r in result.roles])
        self.assertEqual(set(r.item_id for r in records[:5]), set(result.roles[1].item_ids))
        no_target = (replace(roles[0], target=0), roles[1])
        uncapped = self.solve(records, no_target, values)
        self.assertEqual([50000, 5000], [r.total_cents for r in uncapped.roles])

    def test_lower_priority_does_not_steal_from_earlier_group(self):
        records = tuple(search_record(i) for i in range(10))
        roles = (search_role('1304', 50), search_role('1109', 0))
        values = {
            (r.item_id, spec.character_id): (100 if i < 5 else 10)
            for i, r in enumerate(records)
            for spec in roles
        }
        result = self.solve(records, roles, values, ((roles[0].key,), (roles[1].key,)))
        self.assertEqual(PRIORITY_POLICY_VERSION, result.policy_version)
        self.assertEqual(50000, result.roles[0].total_cents)

    def test_zero_target_roundtrip_and_retired_policy_preserves_original_file(self):
        records = tuple(tuned(PROFILE, cost) for cost in (4, 3, 3, 1, 1))
        solver = AllocationHarness(ScoreService(self.provider))
        store = AllocationStore(self.temp.name)
        for target, groups in ((0, ()), (150, ()), (150, ((('1304', 'default'),),))):
            request = AllocationRequest(PROFILE, (role(target=target),), priority_groups=groups)
            result = solver.solve(
                records, request, inventory_revision=0, snapshot=self.provider.active
            )
            payload = store.save(result, records, self.provider.active)
            self.assertEqual(payload, store.load(result.plan_id))
            payload['result']['policy_version'] = 'retired-policy-fixture'
            before = canonical_json(payload)
            with self.assertRaises(ValueError):
                validate_plan(payload)
            self.assertEqual(before, canonical_json(payload))
            path = store._path(result.plan_id)
            path.write_text(before, encoding='utf-8')
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                store.load(result.plan_id)
            self.assertEqual(original, path.read_bytes())

    def test_default_sets_are_source_counts_not_all_maximum_tiers(self):
        self.assertEqual((('凝夜白霜', 5),), default_sets('1102'))
        self.assertEqual((('失序彼岸之梦', 3), ('幽夜隐匿之帷', 2)), default_sets('1608'))
        self.assertEqual(
            (('碎梦亡鬼之魇', 1), ('剪心辑梦之影', 2), ('流金溯真之式', 2)), default_sets('1511')
        )
        self.assertEqual(('凝夜白霜',), default_rule('1106').sonatas)
        self.assertEqual((), default_sets('unknown'))
