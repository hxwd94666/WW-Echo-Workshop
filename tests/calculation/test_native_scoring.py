# 对照冻结样本验证独立 Rust 进程的全部词条分数和评级身份。
import json
from pathlib import Path
import unittest

from echo_workshop.application.compute_protocol import ComputeWorker

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


class TestNativeScoring(unittest.TestCase):
    def test_retired_python_backend_and_helpers_are_deleted(self):
        from echo_workshop.core import scoring

        for name in (
            'calculate_echo_score',
            'PreparedScore',
            'prepare_score',
            'score_prepared',
            'substat_tier',
            'substat_tier_label',
            '_entry_weight',
            '_main_name',
            '_score_entry',
            '_truncate_2',
            '_score_result',
            '_max_main_value',
        ):
            self.assertFalse(hasattr(scoring, name), name)
        self.assertEqual('xwuid-single-entry-truncation-v1', scoring.ALGORITHM_VERSION)

    def test_rating_boundaries_decimal_thresholds_and_five_piece_rounding(self):
        data = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_rating.json').read_text(encoding='utf-8')
        )
        with ComputeWorker(ENGINE) as worker:
            for offset in range(0, len(data['items']), 64):
                cases = data['items'][offset : offset + 64]
                results = worker.call('rating_batch', {'items': [case['input'] for case in cases]})[
                    'items'
                ]
                self.assertEqual([case['expected'] for case in cases], results, offset)
        print(f'RUST_RATING_BOUNDARIES_OK: {len(data["items"])} cases')

    def test_frozen_scores_rows_potential_grade_and_identity(self):
        data = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_scoring.json').read_text(encoding='utf-8')
        )
        origin = ROOT / 'tests/calculation/fixtures/scoring_baseline.json'
        baseline = json.loads(origin.read_text(encoding='utf-8'))
        # The source fixture also contains independently maintained allocation
        # cases. Compare the retired scoring test's actual inputs/expectations,
        # not an unrelated whole-file hash or a second native evaluation.
        self.assertEqual(len(baseline['cases']), len(data['items']))
        for case, item in zip(baseline['cases'], data['items']):
            (
                char,
                modal,
                cost,
                primary,
                value,
                secondary,
                flat,
                count,
                rows,
                current,
                potential,
                label,
                standard,
            ) = case
            self.assertEqual(
                baseline['templates'][char][modal], data['templates'][item['template_key']]
            )
            self.assertEqual(f'{char}/{modal}', item['template_key'])
            self.assertEqual(cost, item['cost'])
            self.assertEqual(str(cost), item['cost_key'])
            self.assertEqual(
                [dict(stat_name=primary, value=value), dict(stat_name=secondary, value=flat)],
                item['main_rows'],
            )
            self.assertEqual(baseline['substats'][:count], item['sub_rows'])
            self.assertEqual(
                dict(
                    row_scores=rows,
                    current_score=current,
                    potential_score=potential,
                    label=label,
                    standard_id=standard,
                ),
                item['expected'],
            )
        with ComputeWorker(ENGINE) as worker:
            for offset in range(0, len(data['items']), 128):
                items = data['items'][offset : offset + 128]
                keys = {item['template_key'] for item in items}
                result = worker.call(
                    'score_batch',
                    dict(
                        templates={key: data['templates'][key] for key in keys},
                        items=[
                            {key: value for key, value in item.items() if key != 'expected'}
                            for item in items
                        ],
                    ),
                )['items']
                self.assertEqual(len(items), len(result))
                for item, actual in zip(items, result):
                    expected = item['expected']
                    for field in ('row_scores', 'current_score', 'potential_score'):
                        self.assertEqual(
                            expected[field], actual[field], (offset, item['template_key'], field)
                        )
                    self.assertEqual(
                        expected['label'], actual['rating']['label'] if actual['rating'] else None
                    )
                    self.assertEqual(
                        expected['standard_id'],
                        actual['rating']['standard_id'] if actual['rating'] else None,
                    )
        print(f'RUST_GOLDEN_SCORING_OK: {len(data["items"])} rows/current/potential/grade/id cases')

    def test_invalid_template_and_numeric_input_are_typed_errors(self):
        from echo_workshop.application.compute_protocol import ComputeFailure

        with ComputeWorker(ENGINE) as worker:
            with self.assertRaises(ComputeFailure) as error:
                worker.call(
                    'score_batch',
                    {
                        'templates': {},
                        'items': [
                            {
                                'template_key': 'missing',
                                'cost': 1,
                                'cost_key': '1',
                                'main_rows': [],
                                'sub_rows': [],
                            }
                        ],
                    },
                )
            self.assertEqual('invalid_request', error.exception.kind)
