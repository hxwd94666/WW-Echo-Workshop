# 独立枚举验证 Rust 达标目标、五件独占、套装与优先级搜索。
from dataclasses import replace
from fractions import Fraction
import itertools
import random
from pathlib import Path
import unittest

from echo_workshop.application.compute_protocol import ComputeFailure, ComputeWorker
from echo_workshop.core.allocation.models import AllocationRequest, SlotRule, cents
from echo_workshop.core.compatibility import CompatibilityRule
from tests.calculation.test_allocation_search import PROFILE, search_record, search_role
from tests.calculation.helpers import tuned

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def independent_rank(rows, targets):
    totals = [row['total_cents'] for row in rows]
    positive = [(total, target) for total, target in zip(totals, targets) if target > 0]
    return (
        sum(total >= target for total, target in positive),
        min((min(Fraction(total, target), 1) for total, target in positive), default=Fraction(1)),
        sum(len(row['item_ids']) == 5 for row in rows),
        sum(min(total, target) if target else total for total, target in zip(totals, targets)),
        sum(totals),
        -sum(row['moves'] for row in rows),
    )


def legal_options(records, roles, index, scores, blocked=()):
    role = roles[index]
    foreign = {
        slot.fixed_item_id
        for other in roles
        if other is not role
        for slot in other.slots
        if slot.fixed_item_id
    }
    candidates = [
        r
        for r in records
        if r.usable
        and len(r.sub_stats) == 5
        and not r.locked
        and r.item_id not in foreign
        and r.item_id not in blocked
        and r.sonata in role.rule.sonatas
    ]
    options = [dict(item_ids=[], total_cents=0, moves=0)]
    for group in itertools.combinations(candidates, 5):
        if role.distinct_names and len({r.name for r in group}) < 5:
            continue
        if any(
            len({r.name for r in group if r.sonata == name}) < count
            for name, count in role.set_requirements
        ):
            continue
        if not any(
            all(
                record.cost == slot.cost
                and (not slot.main_names or record.main_stats[0].stat_name in slot.main_names)
                and (not slot.fixed_item_id or record.item_id == slot.fixed_item_id)
                for record, slot in zip(order, role.slots)
            )
            for order in itertools.permutations(group)
        ):
            continue
        options.append(
            dict(
                item_ids=[r.item_id for r in group],
                total_cents=sum(scores[r.item_id, role.character_id] for r in group),
                moves=sum(not role.owner_label or r.owner != role.owner_label for r in group),
            )
        )
    return options


def independent_optimum(records, roles, scores):
    options = [legal_options(records, roles, index, scores) for index in range(len(roles))]
    best = None
    for selected in itertools.product(*options):
        ids = [item for row in selected for item in row['item_ids']]
        if len(ids) != len(set(ids)):
            continue
        value = independent_rank(selected, [int(role.target * 100) for role in roles])
        if best is None or value > best:
            best = value
    return best


class TestNativeAllocation(unittest.TestCase):
    def test_three_roles_with_fixed_anchors_match_independent_enumeration(self):
        rng = random.Random(351)
        records = tuple(search_record(i) for i in range(15))
        roles = tuple(
            search_role(
                f'fixture_{r}',
                100,
                slots=tuple(SlotRule(1, fixed_item_id=records[r * 4 + j].item_id) for j in range(4))
                + (SlotRule(1),),
            )
            for r in range(3)
        )
        for case in range(5):
            scores = {
                (record.item_id, role.character_id): (1000 if i < 12 else rng.randrange(1000, 9000))
                for i, record in enumerate(records)
                for role in roles
            }
            result = self.solve(records, roles, scores)
            with self.subTest(case=case):
                self.assertTrue(result['exhaustive'], result)
                self.assertEqual(
                    independent_optimum(records, roles, scores),
                    independent_rank(result['roles'], [10000] * 3),
                )

    def test_low_rank_required_uuid_is_not_truncated(self):
        records = tuple(search_record(i, sonata='甲' if i == 399 else '乙') for i in range(400))
        roles = (search_role('1304', 100, set_requirements=(('甲', 1),)),)
        scores = {
            (record.item_id, '1304'): 100 if i == 399 else 10000 for i, record in enumerate(records)
        }
        result = self.solve(records, roles, scores, nodes=5000)
        self.assertIn(records[-1].item_id, result['roles'][0]['item_ids'])

    def test_identical_content_instances_keep_distinct_physical_uuids(self):
        records = tuple(search_record(i, name='相同内容') for i in range(10))
        roles = (
            search_role('1304', 50, distinct_names=False),
            search_role('1109', 50, distinct_names=False),
        )
        scores = {(record.item_id, role.character_id): 1000 for record in records for role in roles}
        result = self.solve(records, roles, scores)
        self.assertTrue(result['exhaustive'])
        self.assertEqual(2, result['objective'][0])
        self.assertEqual(10, len({i for row in result['roles'] for i in row['item_ids']}))

    def solve(self, records, roles, scores, *, groups=(), nodes=200000, extra=None):
        payload = dict(
            request=AllocationRequest(PROFILE, tuple(roles), priority_groups=groups).to_dict(),
            records=[r.to_dict() for r in records],
            max_nodes=nodes,
            matrix_cents=[
                [scores[r.item_id, role.character_id] for r in records] for role in roles
            ],
        )
        payload.update(extra or {})
        with ComputeWorker(ENGINE, fault_tests=True) as worker:
            return worker.call('allocate_matrix', payload)

    def test_random_targets_constraints_and_ownership_match_independent_enumeration(self):
        rng = random.Random(20261016)
        for case in range(12):
            records = tuple(
                search_record(
                    i,
                    sonata='甲' if i % 2 else '乙',
                    name=f'样例-{i // 2}' if case % 3 == 0 else None,
                    owner='A' if i % 3 else 'B',
                )
                for i in range(10)
            )
            roles = (
                search_role(
                    '1304',
                    rng.choice((0, 50, 150, 400)),
                    owner_label='A',
                    distinct_names=case % 4 != 0,
                    set_requirements=(('甲', 2),) if case % 2 else (),
                ),
                search_role(
                    '1109',
                    rng.choice((0, 50, 150, 400)),
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
            scores = {
                (r.item_id, role.character_id): rng.randrange(1, 8000)
                for r in records
                for role in roles
            }
            expected = independent_optimum(records, roles, scores)
            result = self.solve(records, roles, scores)
            with self.subTest(case=case):
                self.assertTrue(result['exhaustive'], result)
                self.assertEqual(
                    expected,
                    independent_rank(result['roles'], [int(role.target * 100) for role in roles]),
                )

    def test_five_peers_rebalance_170_and_140_and_prove_upper_bound(self):
        records = tuple(search_record(i) for i in range(25))
        roles = tuple(search_role(f'fixture_{i}', 150) for i in range(5))
        points = [3400] * 5 + [3000] * 15 + [2800] * 5
        scores = {
            (r.item_id, role.character_id): points[i]
            for i, r in enumerate(records)
            for role in roles
        }
        result = self.solve(records, roles, scores, nodes=300)
        self.assertTrue(result['exhaustive'], result)
        self.assertEqual(5, result['objective'][0])
        self.assertEqual(76000, result['objective'][4])
        self.assertTrue(all(row['total_cents'] >= 15000 for row in result['roles']))
        self.assertEqual(25, len({i for row in result['roles'] for i in row['item_ids']}))
        print(
            'RUST_FIVE_PEER_REBALANCE:',
            [row['total_cents'] for row in result['roles']],
            result['nodes'],
        )

    def test_zero_target_peer_benefits_and_priority_keeps_earlier_group(self):
        records = tuple(search_record(i) for i in range(10))
        roles = (search_role('1304', 50), search_role('1109', 0))
        scores = {
            (r.item_id, role.character_id): (10000 if role.character_id == '1304' else 8000)
            if i < 5
            else 1000
            for i, r in enumerate(records)
            for role in roles
        }
        together = self.solve(records, roles, scores)
        self.assertEqual([5000, 40000], [row['total_cents'] for row in together['roles']])
        groups = ((roles[0].key,), (roles[1].key,))
        priority = self.solve(records, roles, scores, groups=groups)
        self.assertTrue(priority['exhaustive'])
        self.assertEqual([50000, 5000], [row['total_cents'] for row in priority['roles']])

    def test_later_fixed_uuid_is_reserved_before_earlier_group(self):
        records = tuple(search_record(i) for i in range(10))
        roles = (
            search_role('1304', 0),
            search_role(
                '1109',
                0,
                slots=(SlotRule(1, fixed_item_id=records[0].item_id),) + (SlotRule(1),) * 4,
            ),
        )
        scores = {
            (r.item_id, role.character_id): 10000 - i * 100
            for i, r in enumerate(records)
            for role in roles
        }
        result = self.solve(records, roles, scores, groups=((roles[0].key,), (roles[1].key,)))
        self.assertNotIn(records[0].item_id, result['roles'][0]['item_ids'])
        self.assertIn(records[0].item_id, result['roles'][1]['item_ids'])

    def test_budget_and_invalid_input_do_not_claim_optimality(self):
        records = tuple(search_record(i) for i in range(10))
        roles = (search_role('1304', 150), search_role('1109', 150))
        scores = {(r.item_id, role.character_id): 1000 for r in records for role in roles}
        limited = self.solve(records, roles, scores, nodes=1)
        self.assertFalse(limited['exhaustive'])
        self.assertEqual('node_limit', limited['stop_reason'])
        self.assertEqual(1, limited['nodes'])
        bad = AllocationRequest(PROFILE, roles).to_dict()
        bad['roles'][0]['target'] = 1.001
        with self.assertRaises(ComputeFailure):
            self.solve(records, roles, scores, extra={'request': bad})

    def test_objective_exact_ratios_match_independent_fraction_rank(self):
        rng = random.Random(1792160154)
        inputs = []
        for _ in range(500):
            totals = [rng.randrange(100000) for _ in range(5)]
            targets = [rng.choice([0, rng.randrange(1, 100000)]) for _ in range(5)]
            inputs.append(dict(totals=totals, targets=targets, complete=5, moves=25))
        with ComputeWorker(ENGINE) as worker:
            results = worker.call('objective_batch', {'items': inputs})['items']
        for data, result in zip(inputs, results):
            rows = [dict(total_cents=n, item_ids=list(range(5)), moves=5) for n in data['totals']]
            expected = independent_rank(rows, data['targets'])
            actual = list(result['objective'])
            actual[1] = Fraction(*result['minimum_ratio'])
            self.assertEqual(expected, tuple(actual))

    def test_mixed_cost_pins_main_stats_and_sets_match_independent_enumeration(self):
        rng = random.Random(84)
        for case in range(2):
            records = tuple(
                tuned(
                    PROFILE,
                    (4, 3, 1)[i % 3],
                    item_id=search_record(i).item_id,
                    name=f'声骸-{i}',
                    sonata='甲' if i % 2 else '乙',
                )
                for i in range(12)
            )
            slots = tuple(SlotRule(cost) for cost in (4, 3, 3, 1, 1))
            roles = (
                search_role(
                    '1304', 150, slots=slots, cost_budget=12, set_requirements=(('甲', 2),)
                ),
                search_role('1109', 0, slots=slots, cost_budget=12, set_requirements=(('乙', 2),)),
            )
            if case:
                roles = (
                    replace(
                        roles[0], slots=(SlotRule(4, ('暴击',), records[0].item_id),) + slots[1:]
                    ),
                    roles[1],
                )
                records = tuple(
                    replace(r, locked=True) if i == 11 else r for i, r in enumerate(records)
                )
            scores = {
                (r.item_id, role.character_id): rng.randrange(1000, 7000)
                for r in records
                for role in roles
            }
            expected = independent_optimum(records, roles, scores)
            result = self.solve(records, roles, scores)
            with self.subTest(case=case):
                self.assertTrue(result['exhaustive'], result)
                self.assertEqual(expected, independent_rank(result['roles'], [15000, 0]))

    def test_real_rust_matching_allocation_and_evidence_validation(self):
        import json

        data = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_matching.json').read_text(encoding='utf-8')
        )
        choices = data['payload']['selections'][:2]
        for choice in choices:
            choice['rule']['element'] = choice['rule']['element'] or '衍射'
        records = tuple(search_record(i) for i in range(10))
        roles = tuple(
            search_role(
                s['character_id'],
                50,
                rule=CompatibilityRule(s['rule']['element'], tuple(s['rule']['sonatas'])),
            )
            for s in choices
        )
        payload = dict(
            request=AllocationRequest(PROFILE, roles).to_dict(),
            records=[r.to_dict() for r in records],
            templates={s['character_id'] + '/' + s['modal_id']: s['template'] for s in choices},
        )
        with ComputeWorker(ENGINE) as worker:
            matrix = worker.call(
                'match_batch', {'records': payload['records'], 'selections': choices}
            )
            # 列式单元不回显身份与整数字段：按位置还原记录，用十进制换算分值。
            scores = {}
            for position, entry in enumerate(matrix['cells']):
                score_entry = entry[0]
                if score_entry is None:
                    continue
                item_id = matrix['ids'][position // len(choices)]
                character = choices[position % len(choices)]['character_id']
                scores[(item_id, character)] = cents(score_entry[3])
            result = worker.call('allocate', payload)
            self.assertTrue(result['exhaustive'], result)
            self.assertEqual(
                independent_optimum(records, roles, scores),
                independent_rank(result['roles'], [5000, 5000]),
            )
            checked = worker.call(
                'validate_allocation',
                dict(payload, allocations=result['roles'], require_complete=True),
            )
            self.assertEqual(result['objective'], checked['objective'])
            wrong = [dict(row) for row in result['roles']]
            wrong[0]['total_cents'] += 1
            with self.assertRaises(ComputeFailure) as error:
                worker.call('validate_allocation', dict(payload, allocations=wrong))
            self.assertEqual('invalid_evidence', error.exception.kind)

    def test_precheck_reports_missing_fixed_uuid_without_expanding_scope(self):
        records = tuple(search_record(i) for i in range(6))
        missing = search_record(20).item_id
        role = search_role(
            '1304', 0, slots=(SlotRule(1, fixed_item_id=missing),) + (SlotRule(1),) * 4
        )
        with ComputeWorker(ENGINE) as worker:
            result = worker.call(
                'allocation_precheck',
                {
                    'request': AllocationRequest(PROFILE, (role,)).to_dict(),
                    'records': [r.to_dict() for r in records],
                },
            )
        self.assertEqual([missing], result['missing_pins'])
        self.assertEqual([], result['roles'][0]['slot_ids'][0])
        self.assertEqual(6, result['roles'][0]['confirmed'])

    def test_protected_claims_require_explicit_current_plan_context(self):
        records = tuple(search_record(i) for i in range(5))
        roles = (search_role('1304', 0),)
        scores = {(r.item_id, '1304'): 1000 for r in records}
        reservations = {
            'profile_id': PROFILE,
            'inventory_revision': 7,
            'claims': [[r.item_id, '1304'] for r in records],
            'current': [
                {
                    'character_id': '1304',
                    'modal_id': 'default',
                    'plan_id': 'current-plan',
                    'issues': [],
                }
            ],
        }
        protected = self.solve(
            records, roles, scores, extra={'inventory_revision': 7, 'reservations': reservations}
        )
        self.assertEqual([], protected['roles'][0]['item_ids'])
        editable = self.solve(
            records,
            roles,
            scores,
            extra={
                'inventory_revision': 7,
                'reservations': reservations,
                'edit_plans': {'1304': 'current-plan'},
            },
        )
        self.assertEqual(5, len(editable['roles'][0]['item_ids']))
        with self.assertRaises(ComputeFailure):
            self.solve(
                records,
                roles,
                scores,
                extra={'inventory_revision': 8, 'reservations': reservations},
            )
