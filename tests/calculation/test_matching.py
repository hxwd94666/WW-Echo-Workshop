# 验证多角色评分、明确筛选和候选范围。
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from tests.native_runtime import filter_records
from echo_workshop.application.matching.inventory import (
    InventoryViewState,
    MatchInventorySession,
    selectable_inventory_ids,
)
from echo_workshop.application.matching.scope import match_view, select_match_records
from tests.native_runtime import prepare_pin_catalog
from tests.native_runtime import RecommendationService
from echo_workshop.core.cancellation import ComparisonCancelled
from tests.native_runtime import InventoryFilters, ScoreService
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.compatibility import CompatibilityRule, compatibility
from echo_workshop.core.filters import (
    MAIN_STAT_KEYS,
    SUB_STAT_KEYS,
    sonata_options,
)
from echo_workshop.core.models import EchoRecord, StatValue
from echo_workshop.resources.weights import WeightProvider, WeightSnapshot
from tests.calculation.helpers import changed_snapshot, sample, sample_cost4, tuned
from types import SimpleNamespace
from uuid import uuid4


class TestComparison(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = WeightProvider(self.temp.name)
        self.service = ScoreService(self.provider)
        self.profile = str(uuid4())
        self.record = sample(self.profile, review='confirmed')

    def tearDown(self):
        self.temp.cleanup()

    def test_non_contiguous_cache_misses_keep_local_row_and_role_identity(self):
        records = tuple(replace(self.record, item_id=str(uuid4())) for _ in range(3))
        selections = [('1304', 'default'), ('1109', 'default')]
        for cached_rows, cached_roles in (
            (records[1:2], selections),
            (records[::2], selections[:1]),
        ):
            with self.subTest(cached_roles=cached_roles, cached_rows=len(cached_rows)):
                service = ScoreService(self.provider)
                service.compare(cached_rows, cached_roles, inventory_revision=7)
                actual = service.compare(records, selections, inventory_revision=7)
                baseline = ScoreService(self.provider, cache_entries=0).compare(
                    records, selections, inventory_revision=7
                )
                self.assertEqual(actual, baseline)
                self.assertEqual(
                    [(row.item_id, *key) for row in records for key in selections],
                    [(row.item_id, row.character_id, row.modal_id) for row in actual],
                )

    def test_unknown_is_not_a_match_or_inferred_from_template_name(self):
        result = self.service.compare([self.record], [('1304', 'default')], inventory_revision=0)[0]
        self.assertEqual(
            ('unknown', 'unknown', 'unknown'),
            (result.compatibility, result.sonata_compatibility, result.recommendation),
        )

    def test_element_and_set_rules_are_separate_from_identical_base_score(self):
        rules = {('1304', 'default'): CompatibilityRule('衍射', ('测试套装',))}
        correct, wrong_set = self.service.compare(
            [self.record, replace(self.record, item_id=str(uuid4()), sonata='别的套装')],
            [('1304', 'default')],
            inventory_revision=0,
            rules=rules,
        )
        self.assertEqual(correct.score, wrong_set.score)
        self.assertEqual(('match', 'match'), (correct.sonata_compatibility, correct.recommendation))
        self.assertEqual(
            ('mismatch', 'mismatch'), (wrong_set.sonata_compatibility, wrong_set.recommendation)
        )

    def test_rules_are_fixed_per_modal_not_highest_modal_wins(self):
        selections = [('1109', 'default'), ('1109', 'phantom')]
        rules = {
            selections[0]: CompatibilityRule('气动', ('测试套装',)),
            selections[1]: CompatibilityRule('衍射', ('测试套装',)),
        }
        results = self.service.compare([self.record], selections, inventory_revision=0, rules=rules)
        self.assertEqual(['default', 'phantom'], [value.modal_id for value in results])
        self.assertEqual(['mismatch', 'match'], [value.recommendation for value in results])

    def test_traditional_sonata_names_match_without_inventing_aliases(self):
        rule = CompatibilityRule('衍射', ('羽落空尘之歌',))
        record = replace(self.record, sonata='羽落空塵之歌')
        self.assertEqual(('match', 'match', 'match'), compatibility(record, rule))
        self.assertEqual('unknown', compatibility(replace(record, sonata=''), rule)[1])

    def test_changed_rule_invalidates_cache_without_rescanning_inventory(self):
        selection = ('1304', 'default')
        first = self.service.compare(
            [self.record],
            [selection],
            inventory_revision=0,
            rules={selection: CompatibilityRule('衍射', ('测试套装',))},
        )[0]
        second = self.service.compare(
            [self.record],
            [selection],
            inventory_revision=0,
            rules={selection: CompatibilityRule('湮灭', ('测试套装',))},
        )[0]
        self.assertEqual(first.score, second.score)
        self.assertEqual(('match', 'mismatch'), (first.recommendation, second.recommendation))

    def test_pending_remains_excluded_even_when_rule_matches(self):
        rule = {('1304', 'default'): CompatibilityRule('衍射', ('测试套装',))}
        results = self.service.compare(
            [replace(self.record, review='pending')],
            [('1304', 'default')],
            inventory_revision=0,
            rules=rule,
        )
        self.assertTrue(
            all(value.score is None and value.recommendation == 'excluded' for value in results)
        )

    def test_explicit_snapshot_stays_pinned_after_provider_switch(self):
        original = self.provider.active
        self.provider.activate(changed_snapshot())
        result = self.service.compare(
            [self.record], [('1304', 'default')], inventory_revision=0, snapshot=original
        )[0]
        self.assertEqual(original.version, result.weight_version)
        self.assertNotEqual(self.provider.active.version, result.weight_version)

    def test_cancellation_does_not_return_partial_matrix(self):
        cancel = threading.Event()

        def records():
            yield self.record
            cancel.set()
            yield replace(self.record, item_id=str(uuid4()))

        with self.assertRaises(ComparisonCancelled):
            self.service.compare(
                records(), [('1304', 'default')], inventory_revision=0, cancel_event=cancel
            )
        self.assertEqual(
            1, len(self.service.compare([self.record], [('1304', 'default')], inventory_revision=0))
        )

    def test_rule_validation_and_roundtrip(self):
        rule = CompatibilityRule('衍射', ('套装甲', '套装乙'))
        self.assertEqual(rule, CompatibilityRule.from_dict(rule.to_dict()))
        for element in ('乱猜', [], None):
            with self.assertRaises(ValueError):
                CompatibilityRule(element)
        for sonatas in (['甲'], ('甲', '甲'), ('',), (1,)):
            with self.assertRaises(ValueError):
                CompatibilityRule('', sonatas)

    def test_filters_cover_status_cost_text_tuning_and_age(self):
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        good = replace(self.record, captured_at='2026-10-06T00:00:00+00:00', owner='今汐')
        pending = replace(good, item_id=str(uuid4()), review='pending')
        old = replace(good, item_id=str(uuid4()), captured_at='2020-01-01T00:00:00+00:00')
        records = [good, pending, old]
        self.assertEqual(
            (good,), filter_records(records, text='今汐', cost=3, max_age_days=3, now=now)
        )
        self.assertEqual((pending,), filter_records(records, mode='pending'))
        self.assertEqual((), filter_records(records, cost=1))
        self.assertEqual((), filter_records(records, fully_tuned=True))
        self.assertEqual((good, pending, old), filter_records(records, mode='all'))

    def test_unknown_or_future_capture_age_is_not_treated_as_recent(self):
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        for captured in ('?', '2026-10-06T00:00:00', '2026-11-01T00:00:00+00:00'):
            self.assertEqual(
                (),
                filter_records(
                    [replace(self.record, captured_at=captured)], max_age_days=3, now=now
                ),
            )


class TestInventoryFilters(unittest.TestCase):
    def setUp(self):
        self.record = sample(str(uuid4()), game_locked=True, game_equipped=True)

    def test_defaults_keep_unknown_old_records(self):
        record = sample(str(uuid4()), review='confirmed')
        self.assertTrue(InventoryFilters().accepts(record))
        self.assertTrue(InventoryFilters(statuses=('other', 'discarded', 'locked')).accepts(record))
        self.assertFalse(InventoryFilters(statuses=('equipped',)).accepts(record))
        pending = replace(record, review='pending')
        self.assertTrue(InventoryFilters(statuses=('pending',)).accepts(pending))
        self.assertFalse(InventoryFilters(statuses=('other',)).accepts(pending))

    def test_equipment_is_independent_of_retired_status_filters(self):
        rule = InventoryFilters(statuses=('locked', 'equipped'))
        self.assertEqual(('locked', 'equipped'), rule.statuses)
        self.assertTrue(rule.accepts(self.record))
        for value in (False, None):
            self.assertFalse(rule.accepts(replace(self.record, game_equipped=value)))
            self.assertTrue(rule.accepts(replace(self.record, game_equipped=value, locked=True)))
        self.assertFalse(InventoryFilters(statuses=('locked', 'discarded')).accepts(self.record))

    def test_other_status_uses_review_not_retired_game_flags(self):
        rule = InventoryFilters(statuses=('other',))
        self.assertEqual(('other',), rule.statuses)
        for value in (False, True, None):
            self.assertTrue(
                rule.accepts(
                    replace(
                        self.record,
                        review='confirmed',
                        game_equipped=False,
                        game_locked=value,
                        game_discarded=value,
                    )
                )
            )
        self.assertFalse(rule.accepts(self.record))

    def test_local_protection_and_owner_never_imply_game_status(self):
        record = sample(str(uuid4()), locked=True, handled=True, owner='手动归属')
        self.assertFalse(InventoryFilters(statuses=('equipped',)).accepts(record))
        self.assertTrue(InventoryFilters(statuses=('locked', 'discarded')).accepts(record))

    def test_all_five_groups_are_intersection(self):
        rule = InventoryFilters(
            ('locked', 'equipped'), 3, '衍射伤害加成', ('暴击', '暴击伤害'), '测试套装'
        )
        self.assertTrue(rule.accepts(self.record))
        for change in (
            {'cost': 4},
            {'sonata': '其他套装'},
            {'sub_stats': self.record.sub_stats[:1]},
            {'main_stats': (StatValue('攻击%', 30), self.record.main_stats[1])},
        ):
            self.assertFalse(rule.accepts(replace(self.record, **change)))

    def test_primary_only_and_sub_only(self):
        self.assertFalse(InventoryFilters(main_stat='暴击').accepts(self.record))
        self.assertFalse(InventoryFilters(sub_stats=('攻击',)).accepts(self.record))
        with self.assertRaises(ValueError):
            InventoryFilters(main_stat='攻击')
        with self.assertRaises(ValueError):
            InventoryFilters(sub_stats=('衍射伤害加成',))

    def test_percent_flat_and_skill_aliases_are_distinct(self):
        record = replace(
            self.record,
            main_stats=(StatValue('攻击%', 30), StatValue('攻击', 100)),
            sub_stats=(StatValue('攻击%', 7.9), StatValue('普攻', 6.4)),
        )
        self.assertTrue(
            InventoryFilters(
                main_stat='攻击百分比', sub_stats=('普攻伤害加成', '攻击百分比')
            ).accepts(record)
        )
        self.assertFalse(InventoryFilters(sub_stats=('攻击',)).accepts(record))

    def test_choices_are_derived_and_bounded(self):
        self.assertEqual(13, len(MAIN_STAT_KEYS))
        self.assertEqual(13, len(SUB_STAT_KEYS))
        self.assertEqual(5, len(InventoryFilters(sub_stats=SUB_STAT_KEYS[:5]).sub_stats))
        with self.assertRaises(ValueError):
            InventoryFilters(sub_stats=SUB_STAT_KEYS[:6])
        for kwargs in (
            {'cost': True},
            {'cost': 2},
            {'statuses': ('bogus',)},
            {'sub_stats': ('bogus',)},
        ):
            with self.assertRaises(ValueError):
                InventoryFilters(**kwargs)

    def test_sonata_catalog_preserves_old_names_and_simplifies(self):
        snapshot = SimpleNamespace(icons={'1': SimpleNamespace(name='凝夜白霜')})
        records = (
            replace(self.record, sonata='凝夜白霜'),
            self.record,
            replace(self.record, sonata=''),
        )
        self.assertEqual(('凝夜白霜', '测试套装'), sonata_options(snapshot, records))

    def test_old_json_and_strict_new_fields(self):
        row = self.record.to_dict()
        for key in ('game_locked', 'game_discarded', 'game_equipped', 'game_status_updated_at'):
            row.pop(key)
        record = EchoRecord.from_dict(row)
        self.assertIsNone(record.game_locked)
        self.assertEqual(self.record.fingerprint, record.fingerprint)
        for value in (1, 0, '', 'true'):
            with self.assertRaises(ValueError):
                replace(record, game_locked=value)


class TestMultiInventoryFilters(unittest.TestCase):
    def test_legacy_single_values_plural_union_group_intersection_and_substat_all(self):
        profile = str(uuid4())
        base = sample_cost4(
            profile_id=profile,
            cost=4,
            main_stats=(StatValue('暴击', 22), StatValue('攻击', 150)),
            sonata='隐世回光',
            locked=True,
        )
        three = replace(
            base,
            cost=3,
            main_stats=(StatValue('衍射伤害加成', 30), StatValue('攻击', 100)),
            sonata='轻云出月',
            locked=False,
            owner='今汐',
            owner_character_id='1304',
            owner_state='equipped',
            game_equipped=True,
        )
        one = replace(
            base,
            cost=1,
            main_stats=(StatValue('攻击%', 18), StatValue('生命', 2280)),
            sonata='沉日劫明',
        )
        old = InventoryFilters(('locked',), 4, '暴击', (), '隐世回光')
        self.assertTrue(old.accepts(base))
        self.assertFalse(old.accepts(three))
        self.assertTrue(replace(old, cost=None).accepts(base))
        self.assertEqual((), replace(InventoryFilters(cost=3), cost=None).selected_costs)
        spec = InventoryFilters(
            statuses=('locked', 'equipped'),
            costs=(3, 4),
            main_stats=('暴击', '衍射伤害加成'),
            sonatas=('隐世回光', '轻云出月'),
        )
        self.assertTrue(spec.accepts(base))
        self.assertTrue(spec.accepts(three))
        self.assertFalse(spec.accepts(one))
        self.assertFalse(spec.accepts(replace(three, sonata='沉日劫明')))
        self.assertFalse(
            spec.accepts(
                replace(
                    three,
                    owner='',
                    owner_character_id='',
                    owner_state='unknown',
                    game_equipped=False,
                )
            )
        )
        self.assertFalse(
            spec.accepts(
                replace(three, main_stats=(StatValue('攻击%', 30), StatValue('攻击', 100)))
            )
        )
        names = tuple(value.stat_name for value in base.sub_stats[:2])
        self.assertTrue(replace(spec, sub_stats=names).accepts(base))
        self.assertFalse(
            replace(spec, sub_stats=names).accepts(replace(base, sub_stats=base.sub_stats[1:]))
        )
        mixed = InventoryFilters(
            cost=1,
            costs=(4,),
            main_stat='攻击百分比',
            main_stats=('暴击',),
            sonata='沉日劫明',
            sonatas=('隐世回光',),
        )
        self.assertTrue(mixed.accepts(base))
        self.assertTrue(mixed.accepts(one))
        self.assertEqual((1, 4), mixed.selected_costs)
        for arguments in (
            {'costs': (True,)},
            {'costs': (2,)},
            {'costs': '3'},
            {'main_stats': ('攻击',)},
            {'main_stats': ('不存在',)},
            {'sonatas': ('',)},
            {'sonatas': '套装'},
        ):
            with self.assertRaises(ValueError):
                InventoryFilters(**arguments)
        print(
            'FILTER_LEGACY_POSITIONAL_CLEAR_COMPATIBLE_PLURAL_OR_ACROSS_GROUP_AND_STATUS_UNION_SUBSTATS_ALL_AND_STRICT_BAD_CHOICES'
        )


class TestMatchScope(unittest.TestCase):
    def test_four_shapes_and_empty_are_determined_by_actual_counts(self):
        for counts, result in (
            ((1, 1), 'single'),
            ((1, 3), 'role'),
            ((3, 1), 'item'),
            ((3, 3), 'matrix'),
            ((0, 3), 'empty'),
            ((3, 0), 'empty'),
        ):
            self.assertEqual(result, match_view(*counts))

    def test_all_selected_missing_and_empty_never_broaden_scope(self):
        rows = tuple(SimpleNamespace(item_id=i) for i in ('a', 'b', 'c'))
        self.assertEqual(rows, select_match_records(rows))
        self.assertEqual((rows[0], rows[2]), select_match_records(rows, ['c', 'a', 'a']))
        self.assertEqual((), select_match_records(rows, ['removed']))
        self.assertEqual((), select_match_records(rows, []))

    def test_scope_filter_has_cancellation_checkpoint(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            select_match_records((SimpleNamespace(item_id='a'),), ['a'], cancel)


class TestMatchInventorySelection(unittest.TestCase):
    def state(self, profile='profile-a'):
        return InventoryViewState(
            profile,
            search='huolang',
            filters={'cost': 1},
            selected_ids={'library-original'},
            current_id='library-original',
            scroll=123,
        )

    def test_cancel_preserves_original_scope_filters_selection_and_position(self):
        filters = {'cost': 1, 'statuses': ['equipped']}
        view = InventoryViewState('profile-a', 'huolang', filters, {'original'}, 'original', 90)
        filters['statuses'].append('locked')
        self.assertEqual(['equipped'], view.filters['statuses'])
        for prior in (None, frozenset(), frozenset({'old'})):
            with self.subTest(prior=prior):
                session = MatchInventorySession('profile-a', prior, view)
                session.sync_visible('profile-a', {'new'}, {'new'})
                restored = session.cancel('profile-a')
                self.assertIs(view, restored)
                self.assertEqual(prior, session.previous_ids)
                self.assertEqual(frozenset({'original'}), restored.selected_ids)
                self.assertEqual(
                    ('huolang', 'original', 90),
                    (restored.search, restored.current_id, restored.scroll),
                )
                self.assertFalse(session.active)
                with self.assertRaises(ValueError):
                    session.apply_selected('profile-a')

    def test_hidden_and_missing_selections_survive_filter_projection_and_reload(self):
        session = MatchInventorySession('profile-a', frozenset({'a', 'b', 'missing'}), self.state())
        self.assertEqual(frozenset({'a'}), session.visible_selected('profile-a', {'a', 'c'}))
        session.sync_visible('profile-a', {'a', 'c'}, {'a', 'c'})
        self.assertEqual(frozenset({'a', 'b', 'c', 'missing'}), session.selected_ids)
        # A proxy/model restore is projection only, not a user clearing choices.
        self.assertEqual(frozenset(), session.visible_selected('profile-a', {'unrelated'}))
        self.assertEqual(frozenset({'a', 'b', 'c', 'missing'}), session.selected_ids)
        session.sync_visible('profile-a', {'b', 'd'}, {'d'})
        self.assertEqual(frozenset({'a', 'c', 'd', 'missing'}), session.apply_selected('profile-a'))

    def test_apply_filtered_is_uuid_frozen_and_empty_remains_explicit_empty(self):
        def record(ident, profile='profile-a'):
            return SimpleNamespace(item_id=ident, profile_id=profile)

        rows = [
            (record('a'), 'item', None),
            (record('pending'), 'pending', None),
            (record('a'), 'item', None),
        ]
        frozen = selectable_inventory_ids(rows, 'profile-a')
        self.assertEqual(frozenset({'a'}), frozen)
        session = MatchInventorySession('profile-a', None, self.state())
        result = session.apply_filtered('profile-a', frozen)
        rows.append((record('later'), 'item', None))
        self.assertEqual(frozenset({'a'}), result)
        for prior in (None, frozenset({'a'})):
            empty_selected = MatchInventorySession('profile-a', prior, self.state())
            empty_selected.sync_visible('profile-a', {'a'}, ())
            self.assertEqual(frozenset(), empty_selected.apply_selected('profile-a'))
            empty_filter = MatchInventorySession('profile-a', prior, self.state())
            self.assertEqual(frozenset(), empty_filter.apply_filtered('profile-a', ()))

    def test_profile_guards_and_invalid_ids_fail_without_changing_draft(self):
        session = MatchInventorySession('profile-a', frozenset({'a'}), self.state())
        calls = (
            lambda: session.sync_visible('profile-b', {'a'}, ()),
            lambda: session.visible_selected('profile-b', {'a'}),
            lambda: session.apply_selected('profile-b'),
            lambda: session.apply_filtered('profile-b', ()),
            lambda: session.cancel('profile-b'),
            lambda: session.sync_visible('profile-a', {'a'}, {'foreign'}),
            lambda: session.apply_filtered('profile-a', {None}),
        )
        for call in calls:
            with self.assertRaises(ValueError):
                call()
            self.assertTrue(session.active)
            self.assertEqual(frozenset({'a'}), session.selected_ids)
        with self.assertRaises(ValueError):
            MatchInventorySession('profile-b', None, self.state())
        with self.assertRaises(ValueError):
            selectable_inventory_ids(
                [(SimpleNamespace(item_id='x', profile_id='profile-b'), 'item', None)], 'profile-a'
            )


def preferences(profile, keys, rules=None):
    return json.dumps(
        dict(
            schema_version=1,
            profiles={
                profile: dict(
                    selections=keys,
                    rules=[
                        dict(character_id=key[0], modal_id=key[1], **rule.to_dict())
                        for key, rule in (rules or {}).items()
                    ],
                )
            },
        ),
        ensure_ascii=False,
    )


class TestRecommendations(unittest.TestCase):
    def setUp(self):
        self.profile = str(uuid4())
        self.record = tuned(self.profile, 3, review='pending')
        self.snapshot = WeightSnapshot.builtin()
        self.service = RecommendationService()
        self.keys = (('1304', 'default'), ('1109', 'default'))

    def compare(self, record=None, raw=None, snapshot=None):
        return self.service.compare(
            record or self.record,
            raw if raw is not None else preferences(self.profile, self.keys),
            snapshot or self.snapshot,
        )

    def test_unknown_rules_are_only_base_scores_not_confident_recommendations(self):
        result = self.compare()
        self.assertEqual(2, len(result.rows))
        self.assertTrue(all(row.state == 'unknown' for row in result.rows))
        self.assertIn('仅基础分', result.text())
        self.assertTrue(all(row.weight_version == self.snapshot.version for row in result.rows))

    def test_explicit_match_ranked_before_wrong_high_scoring_element(self):
        rules = {
            self.keys[0]: CompatibilityRule('湮灭', ('测试套装',)),
            self.keys[1]: CompatibilityRule('衍射', ('测试套装',)),
        }
        result = self.compare(raw=preferences(self.profile, self.keys, rules))
        self.assertEqual('1109', result.rows[0].character_id)
        self.assertEqual(('match', 'mismatch'), tuple(row.state for row in result.rows))
        self.assertIn('勿按高分推荐', result.text())

    def test_missing_metadata_downgrades_match_but_invalid_roll_blocks_scores(self):
        raw = preferences(
            self.profile,
            self.keys,
            {key: CompatibilityRule('衍射', ('测试套装',)) for key in self.keys},
        )
        result = self.compare(replace(self.record, level=None), raw)
        self.assertTrue(all(row.state == 'unknown' for row in result.rows))
        self.assertIn('元数据待核对', result.text())
        result = self.compare(replace(self.record, issues=('ocr:ambiguous_value',)), raw)
        self.assertFalse(result.rows)
        self.assertIn('待纠错', result.text())

    def test_uuid_changes_do_not_recalculate_same_page_preferences_or_version_do(self):
        first = self.compare()
        self.assertIs(first, self.compare(replace(self.record, item_id=str(uuid4()))))
        self.assertEqual(1, self.service.calculations)
        self.compare(raw=preferences(self.profile, (self.keys[0],)))
        self.assertEqual(2, self.service.calculations)
        self.compare(snapshot=changed_snapshot())
        self.assertEqual(3, self.service.calculations)

    def test_fixed_multiple_modals_remain_distinct_not_automatic_construct_switch(self):
        keys = (('1109', 'default'), ('1109', 'phantom'))
        result = self.compare(raw=preferences(self.profile, keys))
        self.assertEqual({'default', 'phantom'}, {row.modal_id for row in result.rows})
        self.assertEqual(2, len(result.rows))

    def test_deleted_ids_prompt_and_never_fall_back(self):
        result = self.compare(raw=preferences(self.profile, (('deleted', 'default'),)))
        self.assertFalse(result.rows)
        self.assertIn('失效', result.text())

    def test_limit_three_and_profile_scope_do_not_reuse_another_inventory_selection(self):
        keys = (('1304', 'default'), ('1109', 'default'), ('1606', 'default'), ('1109', 'phantom'))
        result = self.compare(raw=preferences(self.profile, keys))
        self.assertEqual(3, len(result.rows))
        result = self.compare(raw=preferences(str(uuid4()), keys))
        self.assertFalse(result.rows)
        self.assertIn('勾选', result.text())
        with self.assertRaises(ValueError):
            self.service.compare(self.record, '', self.snapshot, limit=4)

    def test_corrupt_future_and_oversized_preferences_are_feedback_not_exceptions(self):
        for raw in ('{', '{"schema_version":2,"profiles":{}}', 'x' * (1024 * 1024 + 1)):
            result = self.compare(raw=raw)
            self.assertFalse(result.rows)
            self.assertIn('待修正', result.text())
        self.assertFalse(self.service.compare(None, '', self.snapshot).rows)

    def test_new_snapshot_changes_current_scores_and_keeps_old_result_immutable(self):
        raw = preferences(self.profile, (self.keys[0],))
        first = self.compare(raw=raw)
        second = self.compare(raw=raw, snapshot=changed_snapshot())
        self.assertNotEqual(first.rows[0].current_score, second.rows[0].current_score)
        self.assertNotEqual(first.rows[0].weight_version, second.rows[0].weight_version)


class TestPinCatalog(unittest.TestCase):
    def setUp(self):
        self.profile = str(uuid4())
        self.record = tuned(self.profile)

    def test_full_ids_and_equal_fingerprints_are_not_merged(self):
        other = replace(self.record, item_id=str(uuid4()))
        catalog = prepare_pin_catalog((self.record, other))
        self.assertEqual(3, len(catalog.labels))
        self.assertEqual(1, catalog.positions[self.record.item_id])
        self.assertEqual(2, catalog.positions[other.item_id])
        self.assertIn(self.record.item_id, catalog.labels[1])

    def test_labels_contain_searchable_name_set_main_sub_and_flags(self):
        record = replace(
            self.record,
            name='轻云候选',
            sonata='轻云出月',
            locked=True,
            review='pending',
        )
        text = prepare_pin_catalog((record,)).labels[1]
        for value in (
            '轻云候选',
            '轻云出月',
            'C1',
            '攻击%',
            '暴击伤害',
            '6.3',
            '待确认',
            '保护',
        ):
            self.assertIn(value, text)

    def test_invalid_pending_remains_visible_not_implicitly_usable(self):
        record = replace(self.record, name='', issues=('ocr:unconfirmed',), review='pending')
        catalog = prepare_pin_catalog((record,))
        self.assertEqual((record,), catalog.records)
        self.assertIn('需纠错', catalog.labels[1])
        self.assertFalse(record.usable)

    def test_catalog_positions_and_labels_are_immutable(self):
        catalog = prepare_pin_catalog((self.record,))
        with self.assertRaises(TypeError):
            catalog.positions[self.record.item_id] = 7
        self.assertIsInstance(catalog.labels, tuple)

    def test_duplicate_id_is_an_error_not_last_record_wins(self):
        with self.assertRaisesRegex(ValueError, 'UUID 重复'):
            prepare_pin_catalog((self.record, self.record))

    def test_cancel_before_and_after_progress_never_returns_partial_catalog(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(OperationCancelled):
            prepare_pin_catalog((self.record,), cancel_event=stop)
        stop.clear()

        def cancel(stage, done, total):
            if done:
                stop.set()

        with self.assertRaises(OperationCancelled):
            prepare_pin_catalog((self.record,), cancel_event=stop, progress=cancel)

    def test_full_pool_progress_no_top_k_and_order_preserved(self):
        records = tuple(replace(self.record, item_id=str(uuid4())) for _ in range(10001))
        progress = []
        catalog = prepare_pin_catalog(records, progress=lambda *args: progress.append(args))
        self.assertEqual(records, catalog.records)
        self.assertEqual(10002, len(catalog.labels))
        self.assertEqual(10001, catalog.positions[records[-1].item_id])
        self.assertEqual(('准备完整固定实例候选', 10001, 10001), progress[-1])

    def test_empty_catalog_has_explicit_unpinned_row(self):
        catalog = prepare_pin_catalog(())
        self.assertEqual(('不固定',), catalog.labels)
        self.assertEqual({'': 0}, dict(catalog.positions))
