# 验证观察值校验、五槽COST和主词条套装要求。
import json
import tempfile
import unittest
from dataclasses import replace
from tests.native_runtime import ScoreService
from tests.native_runtime import score_selected_template
from echo_workshop.core.allocation.models import AllocationRequest, RoleRequest, SlotRule
from tests.native_runtime import AllocationHarness
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.requirements_check import RequirementsCheckService
from echo_workshop.core.costs import cost_layouts_from_rows, cost_layouts_to_rows, read_cost_layout
from echo_workshop.core.models import EchoRecord, StatValue
from echo_workshop.core.requirements import (
    RoleRequirements,
    normalize_main_requirements,
    normalize_set_requirements,
    requirements_from_rows,
    requirements_to_rows,
)
from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES
from echo_workshop.resources.weights import WeightProvider
from tests.calculation.helpers import (
    FakeScores,
    role,
    tuned,
    with_cost_layout,
    with_role_requirements,
)
from uuid import uuid4


def item(profile, name, value, **kwargs):
    return EchoRecord(
        profile,
        4,
        (StatValue(name, value, str(value) + '%'), StatValue('攻击', 150)),
        name='测试声骸',
        sonata='测试套装',
        level=25,
        **kwargs,
    )


ROOT = __import__('pathlib').Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def native_rejects(test, *, costs=(4, 3, 3, 1, 1), main=None, allowed=(), sets=()):
    """Business verdicts now come from the shared native terminal only."""
    jobs = ComputeJobs(ENGINE)
    try:
        service = RequirementsCheckService(compute=jobs)
        spec = RoleRequirements(costs, main or ((),) * 5, allowed, sets)
        with test.assertRaises(ValueError):
            service.check({('1304', 'default'): spec})
    finally:
        jobs.close()
        jobs.wait_closed()


class TestMainStatValidation(unittest.TestCase):
    def test_validation_limits_are_independent_and_immutable(self):
        import echo_workshop.core.models as models

        limits = models.MAINSTAT_VALIDATION_MAXIMA
        self.assertEqual(41.8, limits[4][0]['防御%'])
        self.assertEqual(41.5, MAX_MAINSTAT_VALUES[4][0]['防御%'])
        with self.assertRaises(TypeError):
            limits[4][0]['防御%'] = 1
        with self.assertRaises(TypeError):
            limits[4] = ()

    def test_observed_c4_defense_and_healing_upper_values_are_valid(self):
        for name, value in (('防御%', 41.8), ('治疗效果加成', 26.4)):
            r = item(str(uuid4()), name, value)
            self.assertEqual((), r.validation_issues)
            self.assertFalse(r.usable)  # Acceptance still requires explicit confirmation.
            self.assertTrue(replace(r, review='confirmed').usable)

    def test_above_upper_bound_and_nonpositive_values_stay_invalid(self):
        for name, maximum in (('防御%', 41.8), ('治疗效果加成', 26.4)):
            for value in (maximum + 0.02, maximum + 1, 0, -1):
                self.assertIn('main:invalid:0', item(str(uuid4()), name, value).validation_issues)

    def test_second_slot_cost_and_stat_names_are_not_relaxed(self):
        profile = str(uuid4())
        for r in (
            replace(item(profile, '防御%', 41.8), cost=3),
            replace(
                item(profile, '治疗效果加成', 26.4),
                main_stats=(StatValue('治疗效果加成', 26.4), StatValue('防御', 150)),
            ),
            item(profile, '未知主词条', 26.4),
        ):
            self.assertTrue(any(s.startswith('main:invalid:') for s in r.validation_issues))

    def test_existing_scoring_basis_and_exact_row_arithmetic_are_unchanged(self):
        self.assertEqual(41.5, MAX_MAINSTAT_VALUES[4][0]['防御%'])
        self.assertEqual(26.0, MAX_MAINSTAT_VALUES[4][0]['治疗效果加成'])
        for name, observed, legacy in (('防御%', 41.8, 41.5), ('治疗效果加成', 26.4, 26.0)):
            r = item(str(uuid4()), name, observed)
            old = replace(r, main_stats=(StatValue(name, legacy), r.main_stats[1]))
            for template in ('白芷-通用', '维里奈-通用', '角色-通用'):
                a = score_selected_template(template, 4, '4C' + name, r.main_stats, r.sub_stats)
                b = score_selected_template(template, 4, '4C' + name, old.main_stats, old.sub_stats)
                self.assertEqual(a, b)

    def test_matrix_excludes_pending_and_invalid_but_scores_confirmed_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            provider = WeightProvider(root)
            service = ScoreService(provider)
            r = item(str(uuid4()), '治疗效果加成', 26.4)
            records = [
                r,
                replace(r, item_id=str(uuid4()), review='confirmed'),
                item(r.profile_id, '治疗效果加成', 26.42, review='confirmed'),
            ]
            scores = service.compare(records, [('1503', 'default')], inventory_revision=0)
            self.assertIsNone(scores[0].score)
            self.assertIsNotNone(scores[1].score)
            self.assertIsNone(scores[2].score)
            self.assertEqual('excluded', scores[0].recommendation)
            self.assertEqual('excluded', scores[2].recommendation)


class TestCostLayoutCore(unittest.TestCase):
    def test_strict_five_slots_and_stable_key_json_rows(self):
        self.assertEqual((4, 3, 3, 1, 1), read_cost_layout([4, 3, 3, 1, 1]))
        self.assertEqual((4, 4, 1, 1, 1), read_cost_layout((4, 4, 1, 1, 1)))
        for invalid in ((4, 3, 3, 1), (4, 3, 3, 1, True), '43311', (4, 3, 3, 1, 2)):
            with self.assertRaises(ValueError):
                read_cost_layout(invalid)
        # Shape-valid layouts over the total cap are rejected by the native check.
        self.assertEqual((4, 4, 3, 1, 1), read_cost_layout((4, 4, 3, 1, 1)))
        native_rejects(self, costs=(4, 4, 3, 1, 1))
        values = {('1304', 'default'): (4, 3, 3, 1, 1), ('1304', 'another'): (4, 4, 1, 1, 1)}
        rows = json.loads(json.dumps(cost_layouts_to_rows(values)))
        self.assertEqual(values, cost_layouts_from_rows(rows))
        with self.assertRaises(ValueError):
            cost_layouts_from_rows(rows + [rows[0]])
        print(
            'COST_SCHEMA_OK: exact five typed costs; total>12 rejected by the native terminal; profile-owned JSON rows retain exact role/variant IDs'
        )

    def test_real_slot_replacement_retains_constraints_and_conflicts_are_explicit(self):
        pin = str(uuid4())
        original = role(
            target=123.45,
            cost_budget=12,
            distinct_names=False,
            owner_label='历史角色',
            slots=(
                SlotRule(4, ('攻击%',)),
                SlotRule(3, ('攻击%',)),
                SlotRule(3, ('生命%',)),
                SlotRule(1),
                SlotRule(1, ('防御%',), pin),
            ),
        )
        updated = with_cost_layout(original, (4, 4, 1, 1, 1))
        self.assertEqual((4, 4, 1, 1, 1), tuple(slot.cost for slot in updated.slots))
        self.assertEqual(12, updated.cost_budget)
        for name in ('key', 'target', 'rule', 'set_requirements', 'distinct_names', 'owner_label'):
            self.assertEqual(getattr(original, name), getattr(updated, name))
        self.assertEqual(
            tuple(slot.main_names for slot in original.slots),
            tuple(slot.main_names for slot in updated.slots),
        )
        self.assertEqual(pin, updated.slots[4].fixed_item_id)
        self.assertEqual(12, original.cost_budget)
        self.assertEqual(
            original, RoleRequest.from_dict(json.loads(json.dumps(original.to_dict())))
        )
        conflict_main = role(
            slots=(
                SlotRule(4),
                SlotRule(3, ('衍射伤害加成',)),
                SlotRule(3),
                SlotRule(1),
                SlotRule(1),
            )
        )
        with self.assertRaisesRegex(ValueError, '第 2 槽.*主词条'):
            with_cost_layout(conflict_main, (4, 4, 1, 1, 1))
        conflict_pin = role(
            slots=(
                SlotRule(4),
                SlotRule(3, fixed_item_id=pin),
                SlotRule(3),
                SlotRule(1),
                SlotRule(1),
            )
        )
        with self.assertRaisesRegex(ValueError, '第 2 槽.*固定'):
            with_cost_layout(conflict_pin, (4, 4, 1, 1, 1))
        smaller = role(cost_budget=5, slots=tuple(SlotRule(1) for _ in range(5)))
        with self.assertRaisesRegex(ValueError, '已设置的预算 5'):
            with_cost_layout(smaller, (4, 3, 3, 1, 1))
        print(
            'COST_CONSTRAINTS_OK: real slots/budget change; other rules/main/pin retained; incompatible main/pin/lower-budget edits fail without mutation'
        )

    def test_solver_uses_changed_slot_costs_and_treats_budget_as_native(self):
        profile = str(uuid4())
        records = tuple(tuned(profile, cost) for cost in (4, 3, 3, 1, 1, 1))
        with tempfile.TemporaryDirectory() as temp:
            snapshot = WeightProvider(temp).active
            service = AllocationHarness(FakeScores({}))
            request = AllocationRequest(profile, (role(cost_budget=12),))
            first = service.solve(records, request, inventory_revision=7, snapshot=snapshot)
            self.assertEqual(5, len(first.roles[0].item_ids))
            changed = AllocationRequest(
                profile, (with_cost_layout(request.roles[0], (4, 4, 1, 1, 1)),)
            )
            missing = service.solve(records, changed, inventory_revision=7, snapshot=snapshot)
            self.assertEqual((), missing.roles[0].item_ids)
            complete_records = (*records, tuned(profile, 4))
            complete = service.solve(
                complete_records, changed, inventory_revision=7, snapshot=snapshot
            )
            by_id = {record.item_id: record for record in complete_records}
            self.assertEqual(
                [1, 1, 1, 4, 4], sorted(by_id[key].cost for key in complete.roles[0].item_ids)
            )
            # Budget and total-cost caps are native verdicts now; Python keeps only
            # the five-slot/type shape, so construction itself no longer rejects them.
            role(cost_budget=20)
            role(cost_budget=12, slots=tuple(SlotRule(cost) for cost in (4, 4, 4, 1, 1)))
        print(
            'COST_SOLVER_OK: current layouts alter feasible UUID builds; over-budget requests fail the native precheck'
        )


class TestRoleRequirementsCore(unittest.TestCase):
    def test_five_main_choices_and_set_minimums_are_validated_without_invented_tiers(self):
        costs = (4, 3, 3, 1, 1)
        mains = (('暴击', '暴击伤害'), ('攻击%', '共鸣效率'), (), ('生命%',), ())
        self.assertEqual(mains, normalize_main_requirements(costs, mains))
        for bad in (((),) * 4, ('暴击', (), (), (), ())):
            with self.assertRaises(ValueError):
                normalize_main_requirements(costs, bad)
        native_rejects(self, main=(('衍射伤害加成',), (), (), (), ()))
        self.assertEqual(
            (('自定义套装A', '自定义套装B'), (('自定义套装A', 2), ('自定义套装B', 3))),
            normalize_set_requirements(
                ('自定义套装A', '自定义套装B'), (('自定义套装A', 2), ('自定义套装B', 3))
            ),
        )
        for bad in ((('自定义套装A', True),), (('', 1),)):
            with self.assertRaises(ValueError):
                normalize_set_requirements(('自定义套装A', '自定义套装B'), bad)
        for bad in (
            (('自定义套装A', 3), ('自定义套装B', 3)),
            (('未允许', 1),),
            (('自定义套装A', 0),),
            (('自定义套装A', 1), ('自定义套装A', 1)),
        ):
            native_rejects(self, allowed=('自定义套装A', '自定义套装B'), sets=bad)
        print(
            'REQUIREMENTS_VALIDATE_OK: exactly five COST-compatible main lists; multi-main retained; allowed-set minima explicit/unique/positive/total<=5; no name-to-tier guesses'
        )

    def test_profile_owned_rows_keep_exact_role_variant_and_historical_multi_main(self):
        original = role(
            slots=(
                SlotRule(4, ('暴击', '暴击伤害')),
                SlotRule(3, ('攻击%', '共鸣效率')),
                SlotRule(3),
                SlotRule(1),
                SlotRule(1),
            )
        )
        first = RoleRequirements.from_role(original)
        second = replace(first, main_names=(('攻击%',), (), (), (), ()), set_requirements=())
        profiles = {
            'profile-a': requirements_to_rows({original.key: first, ('1304', 'another'): second}),
            'profile-b': requirements_to_rows({original.key: second}),
        }
        saved = json.loads(json.dumps(profiles, ensure_ascii=False))
        loaded = requirements_from_rows(saved['profile-a'])
        self.assertEqual(first, loaded[original.key])
        self.assertEqual(('暴击', '暴击伤害'), loaded[original.key].main_names[0])
        self.assertEqual(second, loaded[('1304', 'another')])
        self.assertEqual(second, requirements_from_rows(saved['profile-b'])[original.key])
        with self.assertRaises(ValueError):
            requirements_from_rows(saved['profile-a'] + [saved['profile-a'][0]])
        invalid = original.to_dict()
        invalid['cost_budget'] = 20
        before = json.dumps(invalid, sort_keys=True)
        # Python keeps schema/identity only; the native terminal rejects the budget.
        RoleRequest.from_dict(invalid)
        self.assertEqual(before, json.dumps(invalid, sort_keys=True))
        print(
            'REQUIREMENTS_SCHEMA_OK: exact profile/variant and multi-main rows; budget stays structural and the native terminal rejects it'
        )

    def test_atomic_merge_applies_actual_slot_and_set_rules_and_preserves_unrelated_constraints(
        self,
    ):
        pin = str(uuid4())
        original = role(
            target=123.45,
            cost_budget=12,
            owner_label='历史装配角色',
            distinct_names=False,
            slots=(
                SlotRule(4, ('暴击', '暴击伤害')),
                SlotRule(3),
                SlotRule(3),
                SlotRule(1),
                SlotRule(1, fixed_item_id=pin),
            ),
        )
        value = RoleRequirements(
            (4, 3, 3, 1, 1),
            (('攻击%',), ('共鸣效率',), (), ('防御%',), ()),
            ('全新套装',),
            (('全新套装', 5),),
        )
        merged = with_role_requirements(
            original, value, rule=CompatibilityRule('热熔', ('旧允许名',))
        )
        self.assertEqual(value.main_names, tuple(slot.main_names for slot in merged.slots))
        self.assertEqual(value.allowed_sonatas, merged.rule.sonatas)
        self.assertEqual(value.set_requirements, merged.set_requirements)
        self.assertEqual('热熔', merged.rule.element)
        for field in ('key', 'target', 'owner_label', 'distinct_names'):
            self.assertEqual(getattr(original, field), getattr(merged, field))
        self.assertEqual(pin, merged.slots[4].fixed_item_id)
        self.assertEqual(12, merged.cost_budget)
        self.assertEqual(12, original.cost_budget)
        self.assertEqual((('测试套装', 5),), original.set_requirements)
        changed_pinned = RoleRequirements((4, 3, 1, 1, 3), ((),) * 5, ('全新套装',), ())
        with self.assertRaisesRegex(ValueError, '第 5 槽.*固定'):
            with_role_requirements(original, changed_pinned)
        lower = role(cost_budget=5, slots=tuple(SlotRule(1) for _ in range(5)))
        with self.assertRaisesRegex(ValueError, '已设置的预算 5'):
            with_role_requirements(lower, value)
        print(
            'REQUIREMENTS_MERGE_OK: one real RoleRequest replacement applies mains/sets/new element; old-set invalid intermediate avoided; targets/pins/owner/distinct retained; conflicts explicit'
        )
