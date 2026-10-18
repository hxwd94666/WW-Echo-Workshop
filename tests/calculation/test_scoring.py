# 验证原生单件算式、档位、冻结边界与匹配缓存精度。
import tempfile
import threading
import unittest
import json
from pathlib import Path
from dataclasses import asdict, replace
from tests.native_runtime import filter_records
from echo_workshop.core.cancellation import ComparisonCancelled
from tests.native_runtime import ScoreService
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import EchoRecord, StatValue, canonical_json
from tests.native_runtime import calculate_echo_score, substat_tier_label
from echo_workshop.resources.weights import WeightProvider
from tests.calculation.helpers import sample
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


def row(name, value):
    return SimpleNamespace(stat_name=name, value=value)


def reference_score(cost, cost_key, main_rows, sub_rows):
    template = {
        'main_props': {'4': {'暴击': 0.5, '攻击': 0.025}},
        'sub_props': {
            '共鸣效率': 0.2,
            '技能伤害加成': 1.1,
            '攻击': 0.1,
            '攻击%': 1.1,
            '暴击': 2,
            '暴击伤害': 1,
        },
        'skill_weight': [0, 0.38, 0, 0],
        'score_max': [72.808, 76.358, 80.358],
    }
    return calculate_echo_score(template, cost, cost_key, main_rows, sub_rows)


class TestEchoScore(unittest.TestCase):
    def test_readme_reference_echo_scores_49_96(self):
        main_rows = [row('暴击', 22), row('攻击', 150)]
        sub_rows = [
            row('暴击', 10.5),
            row('暴击伤害', 21),
            row('攻击%', 11.6),
            row('重击伤害加成', 11.6),
            row('攻击', 60),
        ]

        score = reference_score(4, '4C', main_rows, sub_rows)

        self.assertEqual((6.84, 2.33, 13.06, 13.06, 7.93, 3.01, 3.73), score.row_scores)
        self.assertEqual(49.96, score.current_score)
        self.assertEqual(49.96, score.potential_score)

    def test_potential_keeps_existing_rolls_and_fills_best_missing_substats(self):
        main_rows = [row('暴击', 22), row('攻击', 150)]
        sub_rows = [row('暴击', 6.3)]

        score = reference_score(4, '4C', main_rows, sub_rows)

        self.assertLess(score.current_score, score.potential_score)
        self.assertNotEqual(50.0, score.potential_score)
        # Existing low crit roll stays unchanged; four distinct best max rolls fill the gaps.
        self.assertEqual(44.73, score.potential_score)

    def test_unleveled_main_stats_use_level_25_values_in_both_scores(self):
        main_rows = [row('暴击', 4.4), row('攻击', 30)]

        score = reference_score(4, '4C', main_rows, [])

        # +25 values are Crit 22% and flat ATK 150, not the observed level-0 values.
        self.assertEqual((6.84, 2.33), score.row_scores)
        self.assertEqual(9.17, score.current_score)
        self.assertEqual(49.96, score.potential_score)

    def test_substat_tier_labels_support_eight_and_four_roll_tables(self):
        self.assertEqual('1档', substat_tier_label('暴击', 6.3))
        self.assertEqual('8档', substat_tier_label('暴击', 10.5))
        self.assertEqual('2档', substat_tier_label('暴击伤害', 13.79))
        self.assertEqual('1档', substat_tier_label('攻击', 30))
        self.assertEqual('4档', substat_tier_label('防御', 70))
        self.assertEqual('', substat_tier_label('未知词条', 10))


class TestMatrixPerformance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = WeightProvider(self.temp.name)
        self.record = sample(str(uuid4()), review='confirmed')
        self.keys = (('1304', 'default'), ('1109', 'default'), ('1606', 'default'))

    def tearDown(self):
        self.temp.cleanup()

    def test_native_keeps_frozen_fractional_weight_boundaries(self):
        data = json.loads(
            (Path(__file__).with_name('fixtures') / 'native_score_edges.json').read_text(
                encoding='utf-8'
            )
        )
        for case in data['fractional_cases']:
            result = calculate_echo_score(
                data['fractional_template'],
                3,
                '3',
                tuple(StatValue(**row) for row in case['main_rows']),
                tuple(StatValue(**row) for row in case['sub_rows']),
            )
            self.assertEqual(canonical_json(case['expected']), canonical_json(asdict(result)))

    def test_cache_preserves_ordered_contributions_not_only_sorted_fingerprint(self):
        service = ScoreService(self.provider)
        first = service.compare([self.record], self.keys[:1], inventory_revision=1)[0]
        reordered = replace(self.record, sub_stats=tuple(reversed(self.record.sub_stats)))
        self.assertEqual(self.record.fingerprint, reordered.fingerprint)
        second = service.compare([reordered], self.keys[:1], inventory_revision=1)[0]
        self.assertEqual(
            first.score.row_scores[:2] + tuple(reversed(first.score.row_scores[2:])),
            second.score.row_scores,
        )
        self.assertEqual(first.score.current_score, second.score.current_score)

    def test_validation_and_exclusion_are_owned_by_rust_not_python(self):
        def validation(record):
            raise AssertionError('生产匹配不应再调用 Python 库存业务校验')

        records = [self.record, replace(self.record, item_id=str(uuid4()), review='pending')]
        with patch.object(EchoRecord, 'validation_issues', property(validation)):
            rows = ScoreService(self.provider).compare(records, self.keys, inventory_revision=1)
        self.assertEqual(6, len(rows))
        self.assertTrue(all(row.score is not None and not row.issues for row in rows[:3]))
        self.assertTrue(all(row.score is None for row in rows[3:]))
        self.assertTrue(all(row.recommendation == 'excluded' for row in rows[3:]))
        self.assertTrue(all('inventory:not_confirmed' in row.issues for row in rows[3:]))

    def test_same_fingerprint_does_not_merge_instances(self):
        other = replace(self.record, item_id=str(uuid4()))
        rows = ScoreService(self.provider).compare(
            [self.record, other], self.keys, inventory_revision=1
        )
        self.assertEqual(
            [self.record.item_id] * 3 + [other.item_id] * 3, [row.item_id for row in rows]
        )

    def test_bounded_lru_hits_promote_and_oldest_cell_is_evicted(self):
        service = ScoreService(self.provider, cache_entries=2)
        first = service.compare([self.record], self.keys[:2], inventory_revision=1)
        self.assertIs(
            first[0], service.compare([self.record], self.keys[:1], inventory_revision=1)[0]
        )
        service.compare([self.record], self.keys[2:], inventory_revision=1)
        self.assertEqual(2, len(service._cache))
        self.assertIs(
            first[0], service.compare([self.record], self.keys[:1], inventory_revision=1)[0]
        )
        self.assertIsNot(
            first[1], service.compare([self.record], self.keys[1:2], inventory_revision=1)[0]
        )
        self.assertEqual(2, len(service._cache))

    def test_cache_zero_disables_retention_and_rejects_invalid_capacity(self):
        service = ScoreService(self.provider, cache_entries=0)
        first = service.compare([self.record], self.keys, inventory_revision=1)
        self.assertEqual(first, service.compare([self.record], self.keys, inventory_revision=1))
        self.assertEqual(0, len(service._cache))
        for value in (-1, True, 50001, 0.5):
            with self.assertRaises(ValueError):
                ScoreService(self.provider, cache_entries=value)

    def test_same_revision_changed_content_and_review_recompute(self):
        service = ScoreService(self.provider)
        first = service.compare([self.record], self.keys[:1], inventory_revision=1)[0]
        changed = replace(self.record, sub_stats=(StatValue('暴击', 10.5),))
        second = service.compare([changed], self.keys[:1], inventory_revision=1)[0]
        self.assertNotEqual(first.score, second.score)
        for record in (
            replace(self.record, review='pending'),
            replace(self.record, issues=('ocr:mixed',)),
        ):
            result = service.compare([record], self.keys[:1], inventory_revision=1)[0]
            self.assertIsNone(result.score)
            self.assertEqual('excluded', result.recommendation)

    def test_cancelled_empty_batch_raises_instead_of_publishing_empty_result(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ComparisonCancelled):
            ScoreService(self.provider).compare([], [], inventory_revision=1, cancel_event=cancel)

    def test_cancellation_after_final_record_prevents_partial_publication(self):
        cancel = threading.Event()

        def records():
            yield self.record
            cancel.set()

        with self.assertRaises(ComparisonCancelled):
            ScoreService(self.provider).compare(
                records(), self.keys, inventory_revision=1, cancel_event=cancel
            )

    def test_cancelled_score_waiter_leaves_before_busy_lock_is_released(self):
        service, cancel = ScoreService(self.provider), threading.Event()
        entered, done, errors = threading.Event(), threading.Event(), []

        def wait():
            entered.set()
            try:
                service.compare([self.record], self.keys, inventory_revision=1, cancel_event=cancel)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        with service._lock:
            worker = threading.Thread(target=wait)
            worker.start()
            self.assertTrue(entered.wait(2))
            cancel.set()
            finished_while_locked = done.wait(2)
        worker.join(2)
        self.assertTrue(finished_while_locked)
        self.assertIsInstance(errors[0], ComparisonCancelled)

    def test_filter_cancellation_after_final_record_does_not_publish_partial_list(self):
        cancel = threading.Event()

        def records():
            yield self.record
            cancel.set()

        with self.assertRaises(OperationCancelled):
            filter_records(records(), cancel_event=cancel)
