# 验证独占、公平优先级与预算搜索。
import itertools
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from tests.native_runtime import ScoreService
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.allocation.models import (
    AllocationCancelled,
    AllocationRequest,
    RoleAllocation,
    SlotRule,
    cents,
)
from tests.native_runtime import allocation_precheck
from tests.native_runtime import AllocationHarness
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.models import StatValue
from echo_workshop.resources.weights import WeightProvider, WeightSnapshot
from tests.calculation.helpers import FakeScores, role, tuned
from uuid import uuid4
from tests.calculation.allocation_oracle import objective


class TestAllocation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = WeightProvider(self.temp.name)
        self.service = AllocationHarness(ScoreService(self.provider))
        self.profile = str(uuid4())
        self.records = tuple(tuned(self.profile, cost) for cost in (4, 3, 3, 1, 1))
        self.request = AllocationRequest(self.profile, (role(),))

    def tearDown(self):
        self.temp.cleanup()

    def solve(self, records=None, request=None, **kwargs):
        return self.service.solve(
            records if records is not None else self.records,
            request or self.request,
            inventory_revision=7,
            snapshot=self.provider.active,
            **kwargs,
        )

    def test_five_slot_constraints_sum_current_score_and_snapshot_trace(self):
        result = self.solve()
        self.assertTrue(result.exhaustive)
        row = result.roles[0]
        self.assertEqual(5, len(row.item_ids))
        self.assertEqual(sum(row.scores), row.total_cents)
        scores = self.service.scores.compare(
            self.records, [('1304', 'default')], inventory_revision=7
        )
        self.assertEqual(sum(round(s.score.current_score * 100) for s in scores), row.total_cents)
        self.assertEqual(
            (7, self.provider.active.version), (result.inventory_revision, result.weight_version)
        )
        self.assertTrue(row.reasons)

    def test_global_fairness_matches_independent_brute_force_not_greedy_columns(self):
        records = tuple(tuned(self.profile) for _ in range(10))
        values = {
            (r.item_id, c): (35 if i < 5 else 15) if c == '1304' else (34 if i < 5 else 1)
            for i, r in enumerate(records)
            for c in ('1304', '1109')
        }
        self.service = AllocationHarness(FakeScores(values))
        roles = tuple(
            role(c, 100, slots=tuple(SlotRule(1) for _ in range(5)), cost_budget=5)
            for c in ('1304', '1109')
        )
        request = AllocationRequest(self.profile, roles)
        result = self.solve(records, request, seconds=10, max_nodes=200000)

        def build(c, ids):
            scores = tuple(round(values[(value, c)] * 100) for value in ids)
            return RoleAllocation(
                c, 'default', '', tuple(ids), scores, sum(scores), 10000, len(ids), ()
            )

        ids = [r.item_id for r in records]
        best = objective([build('1304', ()), build('1109', ())], exact=True)
        options = [(), *itertools.combinations(ids, 5)]
        for left in options:
            rest = [value for value in ids if value not in left]
            for right in [(), *itertools.combinations(rest, 5)]:
                best = max(best, objective([build('1304', left), build('1109', right)], exact=True))
        self.assertTrue(result.exhaustive)
        self.assertEqual(best, objective(result.roles, exact=True))
        chosen = [value for row in result.roles for value in row.item_ids]
        self.assertEqual(10, len(set(chosen)))
        self.assertEqual(2, result.objective[0])

    def test_same_fingerprint_distinct_uuid_are_not_merged(self):
        records = tuple(
            replace(
                self.records[0],
                item_id=str(uuid4()),
                cost=1,
                main_stats=tuned(self.profile).main_stats,
            )
            for _ in range(5)
        )
        request = AllocationRequest(
            self.profile,
            (
                role(
                    slots=tuple(SlotRule(1) for _ in range(5)),
                    cost_budget=5,
                    distinct_names=False,
                    set_requirements=(),
                ),
            ),
        )
        self.assertEqual(5, len(self.solve(records, request).roles[0].item_ids))
        self.assertEqual(
            0,
            len(
                self.solve(
                    records,
                    replace(request, roles=(replace(request.roles[0], distinct_names=True),)),
                )
                .roles[0]
                .item_ids
            ),
        )

    def test_locked_items_protected_even_with_explicit_slot_pin(self):
        records = (replace(self.records[0], locked=True), *self.records[1:])
        self.assertEqual((), self.solve(records).roles[0].item_ids)
        pinned = replace(
            role(), slots=(SlotRule(4, fixed_item_id=records[0].item_id), *role().slots[1:])
        )
        self.assertEqual(
            0,
            len(self.solve(records, AllocationRequest(self.profile, (pinned,))).roles[0].item_ids),
        )

    def test_duplicate_pins_same_character_and_invalid_cost_budget_are_rejected(self):
        pin = self.records[1].item_id
        with self.assertRaises(ValueError):
            AllocationRequest(
                self.profile,
                (
                    replace(
                        role(),
                        slots=(
                            SlotRule(4),
                            SlotRule(3, fixed_item_id=pin),
                            SlotRule(3, fixed_item_id=pin),
                            SlotRule(1),
                            SlotRule(1),
                        ),
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            AllocationRequest(self.profile, (role(), role(modal_id='phantom')))
        # Budget/total-cost caps are native verdicts: construction stays shape-only,
        # the solver must reject the over-budget request or return no build.
        invalid_budget = role(cost_budget=11)
        try:
            result = self.solve(request=AllocationRequest(self.profile, (invalid_budget,)))
        except (ValueError, ComputeFailure):
            pass
        else:
            self.assertEqual((), result.roles[0].item_ids)

    def test_unknown_rules_and_mainstat_constraints_reject_or_exclude(self):
        with self.assertRaises(ValueError):
            role(rule=CompatibilityRule())
        constrained = replace(role(), slots=(SlotRule(4, ('暴击伤害',)), *role().slots[1:]))
        self.assertEqual(
            (),
            self.solve(request=AllocationRequest(self.profile, (constrained,))).roles[0].item_ids,
        )

    def test_pending_invalid_and_wrong_element_are_not_candidates(self):
        variants = (
            replace(self.records[0], review='pending'),
            replace(self.records[0], issues=('mixed_frame',)),
        )
        for first in variants:
            self.assertEqual((), self.solve((first, *self.records[1:])).roles[0].item_ids)
        wrong = replace(
            self.records[1], main_stats=(StatValue('湮灭伤害加成', 30), StatValue('攻击', 100))
        )
        self.assertEqual(
            (), self.solve((self.records[0], wrong, *self.records[2:])).roles[0].item_ids
        )

    def test_set_count_constraints_and_traditional_names(self):
        records = tuple(replace(r, sonata='羽落空塵之歌') for r in self.records)
        spec = role(
            rule=CompatibilityRule('衍射', ('羽落空尘之歌', '测试套装')),
            set_requirements=(('羽落空尘之歌', 5),),
        )
        self.assertEqual(
            5, len(self.solve(records, AllocationRequest(self.profile, (spec,))).roles[0].item_ids)
        )
        spec = replace(spec, set_requirements=(('测试套装', 2),))
        self.assertEqual(
            (), self.solve(records, AllocationRequest(self.profile, (spec,))).roles[0].item_ids
        )
        disallowed = role(set_requirements=(('别的套装', 5),))
        try:
            result = self.solve(request=AllocationRequest(self.profile, (disallowed,)))
        except (ValueError, ComputeFailure):
            pass
        else:
            self.assertEqual((), result.roles[0].item_ids)

    def test_allow_untuned_uses_current_not_theoretical_potential(self):
        records = tuple(replace(r, sub_stats=r.sub_stats[:2]) for r in self.records)
        self.assertEqual((), self.solve(records).roles[0].item_ids)
        result = self.solve(records, replace(self.request, fully_tuned=False))
        scores = self.service.scores.compare(records, [role().key], inventory_revision=7)
        self.assertEqual(
            sum(round(s.score.current_score * 100) for s in scores), result.roles[0].total_cents
        )
        self.assertGreater(
            sum(s.score.potential_score * 100 for s in scores), result.roles[0].total_cents
        )

    def test_node_and_time_budgets_never_claim_global_optimum(self):
        result = self.solve(max_nodes=1)
        self.assertEqual((False, 'node_limit'), (result.exhaustive, result.stop_reason))
        result = self.solve(seconds=1e-9)
        self.assertEqual((False, 'time_limit'), (result.exhaustive, result.stop_reason))
        self.assertTrue(all(len(row.item_ids) in (0, 5) for row in result.roles))

    def test_cancel_and_cross_profile_missing_pin_removed_template_fail_explicitly(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(AllocationCancelled):
            self.solve(cancel_event=event)
        with self.assertRaises(ValueError):
            self.solve((replace(self.records[0], profile_id=str(uuid4())), *self.records[1:]))
        with self.assertRaises(ValueError):
            self.solve((*self.records, self.records[0]))
        with self.assertRaises(ValueError):
            self.solve(request=AllocationRequest(self.profile, (role(character='deleted'),)))
        spec = replace(role(), slots=(SlotRule(4, fixed_item_id=str(uuid4())), *role().slots[1:]))
        with self.assertRaises(ValueError):
            self.solve(request=AllocationRequest(self.profile, (spec,)))

    def test_explicit_cancel_in_search_and_fixed_snapshot(self):
        self.service = AllocationHarness(FakeScores({}))
        event = threading.Event()
        event.set()
        with self.assertRaises(AllocationCancelled):
            self.solve(cancel_event=event)
        original = self.provider.active
        from tests.calculation.helpers import changed_snapshot

        self.provider.activate(changed_snapshot())
        result = self.service.solve(
            self.records, self.request, inventory_revision=7, snapshot=original
        )
        self.assertEqual(original.version, result.weight_version)

    def test_unmet_target_has_slot_counts_and_independent_score_bound(self):
        result = self.solve(request=AllocationRequest(self.profile, (role(target=100000),)))
        self.assertTrue(any('上界' in reason for reason in result.roles[0].reasons))
        self.assertTrue(any('距插件目标分差' in reason for reason in result.roles[0].reasons))

    def test_request_roundtrip_hash_and_cent_validation(self):
        self.assertEqual(self.request, AllocationRequest.from_dict(self.request.to_dict()))
        restored = AllocationRequest.from_dict(json.loads(json.dumps(self.request.to_dict())))
        self.assertEqual(self.request, restored)
        self.assertEqual(self.request.version, restored.version)
        self.assertNotEqual(self.request.version, replace(self.request, fully_tuned=False).version)
        self.assertEqual(0, cents(0))
        for value in (-1, True, float('nan'), 1.001):
            with self.assertRaises(ValueError):
                cents(value)

    def test_history_owner_is_only_final_tiebreaker_not_game_truth(self):
        records = tuple(tuned(self.profile, owner='今汐' if i < 5 else '其他') for i in range(6))
        self.service = AllocationHarness(FakeScores({}))
        spec = role(slots=tuple(SlotRule(1) for _ in range(5)), cost_budget=5, owner_label='今汐')
        result = self.solve(records, AllocationRequest(self.profile, (spec,)))
        self.assertEqual(0, result.roles[0].moves)
        self.assertEqual({r.item_id for r in records[:5]}, set(result.roles[0].item_ids))


class TestAllocationPrecheck(unittest.TestCase):
    def setUp(self):
        self.profile = str(uuid4())
        self.snapshot = WeightSnapshot.builtin()

    def request(self, roles=None, fully_tuned=True):
        return AllocationRequest(self.profile, tuple(roles or (role(),)), fully_tuned)

    def test_actual_filter_chain_separates_review_tuning_match_and_protection(self):
        full = tuned(self.profile, 4)
        un = tuned(self.profile, 1)
        un = replace(un, level=10, sub_stats=un.sub_stats[:2])
        records = (
            full,
            un,
            tuned(self.profile, 3, sonata='其他套装'),
            tuned(self.profile, 1, locked=True),
            tuned(self.profile, 3, review='pending'),
        )
        report = allocation_precheck(records, self.request(), self.snapshot)
        row = report['roles'][0]
        self.assertEqual(
            (5, 4, 3, 2, 1),
            tuple(row[key] for key in ('total', 'confirmed', 'tuned', 'matched', 'protected')),
        )
        self.assertEqual((1, 0, 0, 0, 0), tuple(map(len, row['slot_ids'])))
        self.assertEqual(1, report['pending'])
        self.assertEqual(('测试套装', 5, 1), row['sets'][0])
        all_tuning = allocation_precheck(records, self.request(fully_tuned=False), self.snapshot)[
            'roles'
        ][0]
        self.assertEqual(
            (4, 3, 2), tuple(all_tuning[key] for key in ('tuned', 'matched', 'protected'))
        )
        self.assertEqual((1, 0, 0, 1, 1), tuple(map(len, all_tuning['slot_ids'])))

    def test_fixed_items_respect_other_roles_pins_and_locked_item_protection(self):
        records = tuple(tuned(self.profile, cost) for cost in (4, 3, 3, 1, 1)) + (
            tuned(self.profile, 4, locked=True),
        )
        first = role(slots=(SlotRule(4, fixed_item_id=records[0].item_id),) + role().slots[1:])
        second = role(
            '1109', slots=(SlotRule(4, fixed_item_id=records[-1].item_id),) + role().slots[1:]
        )
        rows = allocation_precheck(records, self.request((first, second)), self.snapshot)['roles']
        self.assertEqual((records[0].item_id,), rows[0]['slot_ids'][0])
        # The frozen baseline excludes local locks even when explicitly pinned.
        self.assertEqual((), rows[1]['slot_ids'][0])
        self.assertEqual((5, 4), tuple(row['protected'] for row in rows))
        self.assertNotIn(
            records[-1].item_id, {item for row in rows for slot in row['slot_ids'] for item in slot}
        )

    def test_slot_mainstat_and_cost_use_exact_same_constraints(self):
        records = tuple(tuned(self.profile, cost) for cost in (4, 3, 3, 1, 1))
        one = replace(role(), slots=(SlotRule(4, ('暴击伤害',)),) + role().slots[1:])
        report = allocation_precheck(records, self.request((one,)), self.snapshot)
        self.assertEqual((), report['roles'][0]['slot_ids'][0])
        self.assertEqual((0, 2, 2, 2, 2), tuple(map(len, report['roles'][0]['slot_ids'])))

    def test_missing_pin_is_reported_not_substituted(self):
        missing = str(uuid4())
        one = replace(role(), slots=(SlotRule(4, fixed_item_id=missing),) + role().slots[1:])
        report = allocation_precheck((tuned(self.profile, 4),), self.request((one,)), self.snapshot)
        self.assertEqual((missing,), report['missing_pins'])
        self.assertFalse(report['roles'][0]['slot_ids'][0])

    def test_nonempty_slot_counts_do_not_prove_unique_five_item_build(self):
        records = tuple(tuned(self.profile, cost) for cost in (4, 3, 1))
        request = self.request()
        report = allocation_precheck(records, request, self.snapshot)
        self.assertTrue(all(report['roles'][0]['slot_ids']))
        self.assertNotIn('feasible', report)
        provider = type('Provider', (), {'active': self.snapshot})()
        result = AllocationHarness(ScoreService(provider)).solve(
            records, request, inventory_revision=1, snapshot=self.snapshot
        )
        self.assertEqual((), result.roles[0].item_ids)

    def test_removed_selection_duplicate_cross_profile_and_cancel_are_strict(self):
        records = (tuned(self.profile, 4),)
        with self.assertRaisesRegex(ValueError, 'no longer available'):
            allocation_precheck(records, self.request((role(character='deleted'),)), self.snapshot)
        with self.assertRaises(ValueError):
            allocation_precheck(records + records, self.request(), self.snapshot)
        with self.assertRaises(ValueError):
            allocation_precheck((tuned(str(uuid4()), 4),), self.request(), self.snapshot)
        event = threading.Event()
        event.set()
        with self.assertRaises(OperationCancelled):
            allocation_precheck(records, self.request(), self.snapshot, cancel_event=event)
