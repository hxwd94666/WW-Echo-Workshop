# 验证套装按名称计件政策与匹配固定槽的真实计算边界。
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from echo_workshop.core.compatibility import CompatibilityRule
from tests.native_runtime import ScoreService
from tests.native_runtime import validate_fixed_items
from echo_workshop.core.allocation.models import AllocationRequest, SlotRule
from tests.native_runtime import allocation_precheck
from tests.native_runtime import AllocationHarness
from echo_workshop.core.models import canonical_json
from echo_workshop.core.requirements import (
    RoleRequirements,
    requirements_from_rows,
    requirements_to_rows,
)
from echo_workshop.resources.weights import WeightSnapshot
from tests.calculation.helpers import (
    FakeScores,
    role,
    set_counts,
    set_requirements_satisfied,
    tuned,
    with_role_requirements,
)


class TestRequirementPolicies(unittest.TestCase):
    def test_unique_names_are_per_set_and_retired_policy_is_rejected(self):
        profile = str(uuid4())
        records = tuple(tuned(profile, 1, name='同名' if i < 2 else f'名称{i}') for i in range(5))
        fresh = role(slots=(SlotRule(1),) * 5, distinct_names=False)
        self.assertEqual(4, set_counts(records, unique_names=True)['测试套装'])
        self.assertFalse(set_requirements_satisfied(fresh, records))
        self.assertFalse(
            set_requirements_satisfied(
                replace(fresh, set_requirements=(('测试套装', 2),)), records[:2]
            )
        )
        self.assertTrue(set_requirements_satisfied(replace(fresh, set_requirements=()), records))
        split = (*records[:1], replace(records[1], sonata='另一套装'))
        self.assertEqual({'测试套装': 1, '另一套装': 1}, set_counts(split, unique_names=True))
        raw = AllocationRequest(profile, (fresh,)).to_dict()
        raw['roles'][0].pop('set_unique_names')
        with self.assertRaises(ValueError):
            AllocationRequest.from_dict(json.loads(canonical_json(raw)))
        with self.assertRaises(ValueError):
            replace(fresh, set_unique_names=False)
        with self.assertRaises(ValueError):
            replace(fresh, set_unique_names=1)
        print('SET_POLICY: current distinct-name contract; omitted/retired policy rejected')

    def test_real_solver_precheck_and_replacement_share_name_policy(self):
        from echo_workshop.application.allocation import AllocationService
        from echo_workshop.core.loadout import LoadoutSnapshot
        from tests.native_runtime import source_payload, replacement_candidates

        profile = str(uuid4())
        records = tuple(tuned(profile, 1, name='重复' if i < 2 else f'名称{i}') for i in range(5))
        fresh = role(slots=(SlotRule(1),) * 5, distinct_names=False)
        request = AllocationRequest(profile, (fresh,))
        snapshot = WeightSnapshot.builtin()
        solver = AllocationHarness(FakeScores({}))
        result = solver.solve(records, request, inventory_revision=0, snapshot=snapshot, seconds=1)
        self.assertFalse(result.roles[0].item_ids)
        check = allocation_precheck(records, request, snapshot)
        self.assertEqual((('测试套装', 5, 4),), check['roles'][0]['sets'])
        unique = (*records, tuned(profile, 1, name='补足第五名称'))
        completed = solver.solve(
            unique, request, inventory_revision=0, snapshot=snapshot, seconds=1
        )
        self.assertEqual(5, len(completed.roles[0].item_ids))
        real = AllocationService(ScoreService(SimpleNamespace(active=snapshot))).solve(
            unique, request, inventory_revision=0, snapshot=snapshot, seconds=1
        )
        payload = source_payload(
            dict(profile_id=profile, result=real, records=unique, snapshot=snapshot)
        )
        slot = real.roles[0].item_ids.index(unique[-1].item_id)
        live = LoadoutSnapshot(profile, 0, 0, (), (), unique)
        options = replacement_candidates(payload, live, fresh.character_id, slot)
        blocked = next(
            row for row in options.candidates if row.item_id in {r.item_id for r in records}
        )
        self.assertFalse(blocked.available)
        self.assertIn('套装件数不足', blocked.reason)
        self.assertEqual(
            5,
            len(
                {
                    next(r.name for r in unique if r.item_id == i)
                    for i in completed.roles[0].item_ids
                }
            ),
        )
        print(
            'REAL_CONSTRAINTS: solver rejects duplicate fifth name, precheck counts four, replacement agrees, distinct fifth candidate succeeds'
        )

    def test_fixed_slots_roundtrip_conflicts_visibility_and_scores_are_independent(self):
        profile = str(uuid4())
        first, other = (tuned(profile, 4) for _ in range(2))
        free = tuned(profile, 1)
        costs = (4, 3, 3, 1, 1)
        spec = RoleRequirements(
            costs, ((),) * 5, ('测试套装',), fixed_item_ids=(first.item_id, '', '', '', '')
        )
        restored = requirements_from_rows(
            json.loads(json.dumps(requirements_to_rows({('1304', 'default'): spec})))
        )
        self.assertEqual(spec, restored[('1304', 'default')])
        original = role(
            slots=(
                SlotRule(4, fixed_item_id=first.item_id),
                SlotRule(3),
                SlotRule(3),
                SlotRule(1),
                SlotRule(1),
            )
        )
        old_spec = RoleRequirements(costs, ((),) * 5, ('测试套装',))
        self.assertNotIn('fixed_item_ids', old_spec.to_dict())
        self.assertEqual(
            first.item_id, with_role_requirements(original, old_spec).slots[0].fixed_item_id
        )
        cleared = replace(spec, fixed_item_ids=('',) * 5)
        self.assertFalse(
            any(slot.fixed_item_id for slot in with_role_requirements(original, cleared).slots)
        )
        validate_fixed_items(spec, (first, other, free))
        with self.assertRaisesRegex(ValueError, '缺失'):
            validate_fixed_items(spec, ())
        with self.assertRaisesRegex(ValueError, '冲突'):
            validate_fixed_items(replace(spec, costs=(1, 3, 3, 1, 1)), (first,))
        with self.assertRaisesRegex(ValueError, '多个槽位'):
            replace(spec, fixed_item_ids=(first.item_id, '', '', first.item_id, ''))
        # 可见性由原生匹配同源判定：界面不再用 Python 重算槽位。
        rules = {('1304', 'default'): CompatibilityRule('衍射', ('测试套装',))}

        def exclusion(record, requirements_spec):
            return (
                ScoreService(SimpleNamespace(active=WeightSnapshot.builtin()))
                .compare(
                    (record,),
                    [('1304', 'default')],
                    inventory_revision=0,
                    rules=rules,
                    requirements={('1304', 'default'): requirements_spec},
                )[0]
                .exclusion
            )

        self.assertEqual('', exclusion(first, spec))
        self.assertEqual('slot', exclusion(other, spec))
        self.assertEqual('', exclusion(free, spec))
        self.assertEqual('', exclusion(other, cleared))
        # A pinned UUID cannot migrate to another free compatible slot.
        c1_pin = replace(
            spec,
            costs=(1, 3, 3, 1, 1),
            fixed_item_ids=(free.item_id, '', '', '', ''),
            main_names=(('防御%',), (), (), (), ()),
        )
        self.assertEqual('slot', exclusion(free, c1_pin))
        service = ScoreService(SimpleNamespace(active=WeightSnapshot.builtin()))
        before = service.compare((first, other), [('1304', 'default')], inventory_revision=0)
        after = service.compare((first, other), [('1304', 'default')], inventory_revision=0)
        self.assertEqual(before, after)
        self.assertEqual(2, len(after))  # Visibility never rewrites numeric matrix/scope.
        print(
            'MATCH_PINS: UUID persistence, preserve/explicit clear, missing/conflicts explicit; per-slot visibility only, numeric scoring unchanged'
        )
