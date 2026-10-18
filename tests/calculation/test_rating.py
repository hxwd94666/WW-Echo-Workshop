# 验证单件与整套评级、快照投影和历史冻结。
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from echo_workshop.application.live_selection import resolve_live_selection
from tests.native_runtime import RecommendationService
from tests.native_runtime import ScoreService
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.models import StatValue
from echo_workshop.core.rating import LABELS
from tests.native_runtime import (
    loadout_rating,
    rating_template,
    single_rating,
    single_standard,
)
from tests.native_runtime import calculate_echo_score
from echo_workshop.resources.weights import ALGORITHM_VERSION, WeightSnapshot
from tests.native_runtime import WeightProvider
from pathlib import Path
from tests.calculation.helpers import set_custom_template, changed_snapshot, tuned
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4


class TestEchoRating(unittest.TestCase):
    def test_cost_lower_bound_decimal_and_card_rounding_boundaries(self):
        template = WeightSnapshot.builtin().template('1304')
        for cost in (1, 3, 4):
            for label, score in zip(LABELS, (0, 24, 30, 35, 39, 42)):
                self.assertEqual(label, single_rating(score, template, cost=cost).label)
                if score:
                    self.assertEqual(
                        LABELS[LABELS.index(label) - 1],
                        single_rating(score - 0.01, template, cost=cost).label,
                    )
        for label, score in zip(LABELS, (0, 120, 150, 175, 195, 210)):
            cents = round(score * 100)
            values = (
                (cents, 0, 0, 0, 0)
                if cents <= 5000
                else tuple([cents // 5] * 4 + [cents - (cents // 5) * 4])
            )
            self.assertEqual(label, loadout_rating(values, template).label)
            if cents:
                below = (*values[:4], values[-1] - 1) if values[-1] else (values[0] - 1, 0, 0, 0, 0)
                self.assertEqual(
                    LABELS[LABELS.index(label) - 1], loadout_rating(below, template).label
                )
        asymmetric = dict(
            template,
            props_grade=(
                (0, 0.2, 0.56, 0.7, 0.8, 1),
                (0, 0.3, 0.6, 0.7, 0.8, 1),
                (0, 0.4, 0.6, 0.7, 0.8, 1),
            ),
            total_grade=(0, 0.1, 0.2, 0.564, 0.8, 1),
        )
        self.assertEqual(
            (10, 15, 20),
            tuple(single_standard(asymmetric, cost).thresholds[1] for cost in (1, 3, 4)),
        )
        self.assertEqual('A', single_rating(28, asymmetric, cost=1).label)
        self.assertEqual('B', single_rating(27.99, asymmetric, cost=1).label)
        self.assertEqual('S', loadout_rating((2820,) * 5, asymmetric).label)
        self.assertEqual('A', loadout_rating((2820, 2820, 2820, 2820, 2819), asymmetric).label)
        near = dict(
            template,
            total_grade=(0, 0.2, 0.4, 0.6, 0.8, 1),
            props_grade=((0, 0.2, 0.4, 0.6, 0.8, 1),) * 3,
        )
        self.assertEqual('SS', single_rating(49.96, near, cost=1).label)
        self.assertEqual(49.96, single_rating(49.96, near, cost=1).score)
        self.assertEqual('SSS', single_rating(50, near, cost=1).label)
        self.assertEqual(49.95, loadout_rating((4995, 0, 0, 0, 0), near).score)
        self.assertEqual(50, loadout_rating((4996, 0, 0, 0, 0), near).score)
        self.assertEqual(249.9, loadout_rating((5000, 5000, 5000, 5000, 4990), near).score)
        self.assertEqual(250, loadout_rating((5000, 5000, 5000, 5000, 4991), near).score)
        self.assertEqual('SS', loadout_rating((5000, 5000, 5000, 5000, 4990), near).label)
        self.assertEqual('SSS', loadout_rating((5000, 5000, 5000, 5000, 4991), near).label)
        for values in ((5000,) * 4, (5000,) * 6, (False, 0, 0, 0, 0), (-1, 0, 0, 0, 0)):
            self.assertIsNone(loadout_rating(values, template))
        self.assertIsNone(single_rating(float('nan'), template, cost=1))
        self.assertIsNone(single_rating(30, template, cost=True))
        print(
            'RATING_BOUNDARIES_OK: single=0/24/30/35/39/42 total=0/120/150/175/195/210; >= exact decimal28/141; COST1/3/4 map; strict4995/24990 rounding; incomplete/invalid unavailable'
        )

    def test_native_batch_and_real_score_service_keep_numeric_results(self):
        snapshot = WeightSnapshot.builtin()
        profile = str(uuid4())
        records = tuple(tuned(profile, cost) for cost in (1, 3, 4))
        with tempfile.TemporaryDirectory() as folder:
            provider = WeightProvider(folder)
            service = ScoreService(provider)
            cells = service.compare(records, (('1304', 'default'),), inventory_revision=1)
            for record, cell in zip(records, cells):
                template = snapshot.scoring_template('1304')
                with patch(
                    'echo_workshop.core.rating.validate_grade_table',
                    side_effect=AssertionError('Per-cell grade validation'),
                ):
                    scalar = calculate_echo_score(
                        template, record.cost, str(record.cost), record.main_stats, record.sub_stats
                    )
                self.assertEqual(scalar, cell.score)
                self.assertEqual(snapshot.version, cell.weight_version)
                self.assertIsNotNone(scalar.rating)
        self.assertEqual('xwuid-single-entry-truncation-v1', ALGORITHM_VERSION)
        print(
            'RATING_SCORE_PARITY_OK: native_batch=realScoreService; COST1/3/4; no Python table evaluation; algorithm unchanged'
        )


class TestRatingCompatibility(TestCase):
    def test_cross_template_rating_keeps_numbers_cache_and_snapshot_isolation(self):
        old = WeightSnapshot.builtin().to_dict()
        old.pop('version')
        old.pop('local_state', None)
        for key, bands in (
            ('1304', [0, 0.1, 0.2, 0.3, 0.4, 0.5]),
            ('1109', [0, 0.7, 0.75, 0.8, 0.9, 1]),
        ):
            old['templates'][key]['default']['props_grade'] = [bands[:] for _ in range(3)]
        snapshot = WeightSnapshot(
            old['templates'],
            old['conditions'],
            old['source'],
            source_revision=old.get('source_revision', ''),
            character_elements=old.get('character_elements', {}),
        )
        record = tuned(str(uuid4()), 1, name='评级兼容样本')
        selected = [('1304', 'default'), ('1109', 'default')]
        prefs = json.dumps(
            dict(
                schema_version=1, profiles={record.profile_id: dict(selections=selected, rules=[])}
            )
        )
        service = RecommendationService()
        result = service.compare(record, prefs, snapshot)
        rows = {row.character_id: row for row in result.rows}
        numbers = {key: (row.current_score, row.potential_score) for key, row in rows.items()}
        self.assertEqual('SSS', rows['1304'].rating.label)
        self.assertEqual('C', rows['1109'].rating.label)
        self.assertIn('SSS', result.text())
        self.assertIn(' · C · ', result.text())
        self.assertTrue(all(row.weight_version == snapshot.version for row in result.rows))
        before = service.calculations
        with patch.object(
            service.compute,
            'submit',
            side_effect=AssertionError('Cache projection rescored'),
        ):
            self.assertIs(result, service.compare(record, prefs, snapshot))
        self.assertEqual(before, service.calculations)
        newer = snapshot.to_dict()
        newer.pop('version')
        newer.pop('local_state', None)
        newer['templates']['1304']['default']['props_grade'] = [
            [0, 0.7, 0.75, 0.8, 0.9, 1] for _ in range(3)
        ]
        new_snapshot = WeightSnapshot(
            newer['templates'],
            newer['conditions'],
            newer['source'],
            source_revision=newer.get('source_revision', ''),
            character_elements=newer.get('character_elements', {}),
        )
        updated = service.compare(record, prefs, new_snapshot)
        by_id = {row.character_id: row for row in updated.rows}
        self.assertEqual('C', by_id['1304'].rating.label)
        self.assertEqual('SSS', rows['1304'].rating.label)
        self.assertEqual(
            numbers, {key: (row.current_score, row.potential_score) for key, row in by_id.items()}
        )
        print(
            'MODIFIED_CROSS_TEMPLATE_EXACT_ROLE_GRADES_SSS_C_CACHE_NO_RECOMPUTE_STANDARD_UPDATE_NO_NUMERIC_OR_OLD_RESULT_CHANGE'
        )
        print('CROSS_TEMPLATE_LITERAL=' + repr(result.text()))


class TestFrozenRatingSnapshot(unittest.TestCase):
    def test_historical_tables_custom_projection_and_invalid_data(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = WeightProvider(folder)
            original = provider.active
            frozen = json.loads(json.dumps(original.to_dict()))
            old_template = original.template('1304')
            old_grade = loadout_rating((3000,) * 5, old_template)
            data = original.to_dict()
            data['templates']['1304']['default']['props_grade'] = [[0, 0.1, 0.2, 0.3, 0.4, 0.5]] * 3
            data['templates']['1304']['default']['total_grade'] = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
            provider.activate(WeightSnapshot(data['templates'], data['conditions']))
            self.assertEqual(
                old_grade, loadout_rating((3000,) * 5, rating_template(frozen, '1304'))
            )
            self.assertNotEqual(
                old_grade.standard_id,
                loadout_rating((3000,) * 5, provider.active.template('1304')).standard_id,
            )
            from tests.native_runtime import (
                editable_values,
                build_custom_template,
            )

            main, sub = editable_values(provider.active.template('1304'))
            sub['暴击'] += 0.25
            custom = build_custom_template(provider.active.template('1304'), main, sub)
            set_custom_template(provider, '1304', 'default', custom)
            frozen_custom = deepcopy(provider.active.to_dict())
            pinned = provider.active.template('1304')
            next_data = provider.upstream.to_dict()
            next_data['templates']['1304']['default']['props_grade'] = [
                [0, 0.15, 0.25, 0.35, 0.45, 0.55]
            ] * 3
            next_data['templates']['1304']['default']['total_grade'] = [
                0,
                0.15,
                0.25,
                0.35,
                0.45,
                0.55,
            ]
            next_data['templates']['1304']['default']['sub_props']['暴击'] = 99
            provider.activate(WeightSnapshot(next_data['templates'], next_data['conditions']))
            self.assertEqual(pinned, provider.active.template('1304'))
            self.assertEqual(
                pinned['score_max'], provider.active.scoring_template('1304')['score_max']
            )
            self.assertEqual(
                pinned['sub_props'], provider.active.scoring_template('1304')['sub_props']
            )
            fresh = single_rating(30, rating_template(provider.active, '1304'), cost=1)
            self.assertEqual((0, 7.5, 12.5, 17.5, 22.5, 27.5), fresh.thresholds)
            self.assertEqual(
                fresh, single_rating(30, rating_template(provider.active.to_dict(), '1304'), cost=1)
            )
            old_custom = single_rating(30, rating_template(frozen_custom, '1304'), cost=1)
            self.assertEqual((0, 5, 10, 15, 20, 25), old_custom.thresholds)
            self.assertEqual(
                old_custom,
                single_rating(
                    30, rating_template(WeightSnapshot.from_dict(frozen_custom), '1304'), cost=1
                ),
            )
            missing = provider.upstream.to_dict()
            missing['templates'].pop('1304')
            missing['conditions'].pop('1304', None)
            provider.activate(WeightSnapshot(missing['templates'], missing['conditions']))
            self.assertEqual(
                pinned['props_grade'], rating_template(provider.active, '1304')['props_grade']
            )
            legacy = deepcopy(frozen)
            legacy['templates']['1304']['default'].pop('props_grade')
            legacy['templates']['1304']['default'].pop('total_grade')
            legacy.pop('version')
            legacy_snapshot = WeightSnapshot(legacy['templates'], legacy['conditions'])
            self.assertIsNone(single_rating(30, legacy_snapshot.template('1304'), cost=1))
            self.assertIsNone(loadout_rating((3000,) * 5, legacy_snapshot.template('1304')))
            for bad in (
                [0, 0.2, 0.4, 0.6, 0.8],
                [0, 0.2, 0.2, 0.6, 0.8, 1],
                [0, 0.2, 0.4, 0.6, 0.8, True],
                [0, 0.2, 0.4, 0.6, 0.8, 1.01],
                [0, 0.2, 0.4, 0.6, 0.8, float('nan')],
            ):
                broken = deepcopy(frozen)
                broken['templates']['1304']['default']['total_grade'] = bad
                broken.pop('version')
                with self.assertRaises(ValueError):
                    WeightSnapshot(broken['templates'], broken['conditions'])
                self.assertIsNone(
                    loadout_rating((3000,) * 5, broken['templates']['1304']['default'])
                )
            self.assertEqual(frozen, json.loads(json.dumps(original.to_dict())))
        print(
            'FROZEN_RATING_OK: custom numbers pinned, standards updated, JSON history isolated, malformed/missing not invented'
        )


def custom_weight_edit(provider, cid='1304', variant='default', weight=3):
    from tests.native_runtime import editable_values, build_custom_template

    original = provider.active.template(cid, variant)
    main, sub = editable_values(original)
    sub['暴击'] = weight
    return build_custom_template(original, main, sub)


class TestWeights(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = WeightProvider(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_weight_bounds_prevent_finite_input_overflow_and_tiny_denominators(self):
        for field, value in (('sub_props', 1e308), ('score_max', 1e-300)):
            data = WeightSnapshot.builtin().to_dict()
            template = data['templates']['1304']['default']
            if field == 'sub_props':
                template[field]['暴击'] = value
            else:
                template[field][0] = value
            with self.assertRaises(ValueError):
                WeightSnapshot(data['templates'], data['conditions'])

    def test_snapshot_is_deeply_immutable_and_detached(self):
        data = WeightSnapshot.builtin().to_dict()
        snapshot = WeightSnapshot(data['templates'], data['conditions'])
        data['templates']['1304']['default']['sub_props']['暴击'] = 99
        self.assertNotEqual(99, snapshot.template('1304')['sub_props']['暴击'])
        with self.assertRaises(TypeError):
            snapshot.templates['1304']['default']['name'] = 'changed'
        with self.assertRaises(TypeError):
            snapshot.template('1304')['score_max'][0] = 1

    def test_version_tracks_content_and_conditions_not_provenance(self):
        original = self.provider.active
        self.assertEqual(
            original.version, replace(original, source='other', fetched_at='later').version
        )
        self.assertNotEqual(original.version, changed_snapshot().version)
        data = original.to_dict()
        data['conditions']['1109'][1]['value'] = ['another set']
        snapshot = WeightSnapshot(data['templates'], data['conditions'])
        self.assertNotEqual(original.version, snapshot.version)

    def test_unknown_condition_operators_and_missing_targets_are_rejected(self):
        data = self.provider.active.to_dict()
        for rule in (
            {'choose': 'calc.json', 'key': 'modal', 'op': 'eval', 'value': 'x'},
            {'choose': 'calc-removed.json', 'key': 'modal', 'op': '=', 'value': 'x'},
            {'choose': 3, 'op': '=', 'key': 'modal', 'value': 'x'},
        ):
            data['conditions']['1109'] = [rule]
            with self.assertRaises(ValueError):
                WeightSnapshot(data['templates'], data['conditions'])

    def test_invalid_numbers_shapes_and_future_schemas_are_rejected(self):
        for value in (float('nan'), float('inf'), -1, True):
            data = self.provider.active.to_dict()
            data['templates']['1304']['default']['score_max'][0] = value
            with self.assertRaises(ValueError):
                WeightSnapshot(data['templates'], data['conditions'])
        data = self.provider.active.to_dict()
        data['schema_version'] = 99
        with self.assertRaises(ValueError):
            WeightSnapshot.from_dict(data)


class TestCustomWeights(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.p = WeightProvider(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_fifty_point_normalization_skills_and_batch_scoring_cache(self):
        from tests.native_runtime import editable_values, build_custom_template
        from echo_workshop.core.models import EchoRecord, StatValue
        from tests.native_runtime import ScoreService
        from uuid import uuid4

        original = self.p.active.template('1304')
        main, sub = editable_values(original)
        sub['共鸣技能'] = 4.5
        sub['共鸣解放'] = 0
        sub['暴击'] = 3
        template = build_custom_template(original, main, sub)
        _, projected_sub = editable_values(template)
        self.assertEqual(4.5, projected_sub['共鸣技能'])
        self.assertEqual(0, projected_sub['共鸣解放'])
        profile = str(uuid4())
        record = EchoRecord(
            profile,
            3,
            (StatValue('衍射伤害加成', 30), StatValue('攻击', 100)),
            (StatValue('暴击', 6.3),),
            name='测试',
            sonata='套装',
            level=25,
            review='confirmed',
        )
        service = ScoreService(self.p)
        old = service.compare([record], [('1304', 'default')], inventory_revision=1)[0]
        set_custom_template(self.p, '1304', 'default', template)
        new = service.compare([record], [('1304', 'default')], inventory_revision=1)[0]
        self.assertNotEqual(old.score, new.score)
        self.assertEqual(
            calculate_echo_score(
                self.p.active.template('1304'), 3, '3', record.main_stats, record.sub_stats
            ),
            new.score,
        )
        inputs = json.loads(
            (Path(__file__).with_name('fixtures') / 'native_score_edges.json').read_text(
                encoding='utf-8'
            )
        )
        for case in inputs['custom_maxima']:
            cost = case['cost']
            mains = tuple(StatValue(**row) for row in case['main_rows'])
            subs = tuple(StatValue(**row) for row in case['sub_rows'])
            result = calculate_echo_score(template, cost, str(cost), mains, subs)
            self.assertGreaterEqual(result.current_score, 49.9)
            self.assertLessEqual(result.current_score, 50)

    def test_bad_values_all_zero_unknown_fields_and_tampering_rejected(self):
        from tests.native_runtime import editable_values, build_custom_template

        original = self.p.active.template('1304')
        main, sub = editable_values(original)
        for value in (-1, True, float('nan'), float('inf'), 1000001):
            with self.assertRaises(ValueError):
                build_custom_template(original, main, sub | {'暴击': value})
        with self.assertRaises(ValueError):
            build_custom_template(
                original, {c: {n: 0 for n in w} for c, w in main.items()}, {n: 0 for n in sub}
            )
        template = custom_weight_edit(self.p)
        template['score_max'][0] += 1
        with self.assertRaises(ValueError):
            set_custom_template(self.p, '1304', 'default', template)
        set_custom_template(self.p, '1304', 'default', custom_weight_edit(self.p))
        data = self.p.active.to_dict()
        data['local_state']['characters']['1304']['templates']['default']['sub_props']['暴击'] = 99
        with self.assertRaises(ValueError):
            WeightSnapshot.from_dict(data)
        data = self.p.active.to_dict()
        data['local_state']['upstream']['local_state'] = data['local_state']
        with self.assertRaises(ValueError):
            WeightSnapshot.from_dict(data)


class TestCharacterElements(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.p = WeightProvider(self.root)
        self.base = self.p.active
        self.known = replace(
            self.base, character_elements={'1304': '衍射', '1501': '衍射', '1604': '湮灭'}
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_scalar_batch_live_and_wrong_element_only_zero_that_row(self):
        rec = tuned(str(uuid4()), 3)
        template = self.known.scoring_template('1304')
        wrong = replace(rec, main_stats=(StatValue('湮灭伤害加成', 30), rec.main_stats[1]))
        good = calculate_echo_score(template, 3, '3', rec.main_stats, rec.sub_stats)
        bad = calculate_echo_score(template, 3, '3', wrong.main_stats, wrong.sub_stats)
        self.assertGreater(good.row_scores[0], 0)
        self.assertEqual(0, bad.row_scores[0])
        self.assertEqual(good.row_scores[1:], bad.row_scores[1:])
        live = resolve_live_selection(self.known, [], fixed=('1304', 'default'))
        self.assertEqual('衍射', live.template['_character_element'])
        self.assertEqual(self.base.template('1304'), self.known.template('1304'))
        # Unknown role in a metadata-bearing snapshot does not count six elements.
        unknown = self.known.scoring_template('1109')
        self.assertEqual(
            0, calculate_echo_score(unknown, 3, '3', rec.main_stats, rec.sub_stats).row_scores[0]
        )

    def test_matching_auto_element_overrides_stale_rule_and_refreshes_cache(self):
        record = tuned(str(uuid4()), 3)
        service = ScoreService(self.p)
        key = ('1304', 'default')
        stale = {key: CompatibilityRule('湮灭', ('测试套装',))}
        before = service.compare(
            [record], [key], inventory_revision=1, snapshot=self.known, rules=stale
        )[0]
        self.assertEqual('element_match', before.compatibility)
        self.assertEqual('衍射', before.explicit_rule.element)
        changed = replace(self.known, character_elements={'1304': '湮灭'})
        after = service.compare(
            [record], [key], inventory_revision=1, snapshot=changed, rules=stale
        )[0]
        self.assertEqual('element_mismatch', after.compatibility)
        self.assertEqual(0, after.score.row_scores[0])
        self.assertGreater(before.score.current_score, after.score.current_score)
        self.assertEqual('衍射', self.known.compatibility_rule('1501').element)
        self.assertEqual('湮灭', self.known.compatibility_rule('1604').element)
