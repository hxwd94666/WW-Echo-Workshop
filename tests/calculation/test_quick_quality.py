# 验证快速评分只用原生质量结论、结构失效与后台刷新边界。
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from ww_echo_workshop.application.quick_score import (
    QuickScoreStale,
    QuickScoreService,
)
from ww_echo_workshop.core.cancellation import OperationCancelled
from ww_echo_workshop.core.models import EchoRecord
from tests.native_runtime import ScoreService, WeightProvider, inventory_queries

FIXTURE = Path(__file__).with_name('fixtures') / 'review_quality.json'


def record_fixture():
    return EchoRecord.from_dict(
        json.loads(FIXTURE.read_text(encoding='utf-8'))['cases'][0]['record']
    )


def service_fixture(record, directory):
    weights = WeightProvider(directory)
    repository = SimpleNamespace(record=record, revision=7)
    repository.get = Mock(side_effect=lambda _: repository.record)
    repository.try_revision = lambda: repository.revision
    stopped = threading.Event()
    scorer = QuickScoreService(
        repository,
        inventory_queries,
        ScoreService(weights),
        weight_snapshot=lambda: weights.active,
        closed=stopped.is_set,
    )
    return repository, scorer


class QuickQuality(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.record = record_fixture()
        self.repository, self.scorer = service_fixture(self.record, self.directory.name)
        _, _, facts = inventory_queries.review(((self.record, 'item', ()),))
        self.quality = facts[0]

    def test_native_score_keeps_baseline_and_invalid_records_never_return_a_score(self):
        with (
            patch.object(
                EchoRecord,
                'validation_issues',
                new_callable=PropertyMock,
                side_effect=AssertionError('Python quality'),
            ),
            patch.object(
                EchoRecord,
                'usable',
                new_callable=PropertyMock,
                side_effect=AssertionError('Python usability'),
            ),
        ):
            result = self.scorer.score_one(self.record, ('1304', 'default'))
            self.assertEqual(29.96, result.comparison.score.current_score)
            self.assertEqual('', result.notice)
            self.scorer.validate_context(self.record, self.scorer.weight_snapshot())
            for bad in (
                replace(self.record, review='pending'),
                replace(self.record, main_stats=()),
            ):
                self.repository.record = bad
                with self.assertRaisesRegex(ValueError, '核对'):
                    self.scorer.score_one(bad, ('1304', 'default'))
            with self.assertRaises(QuickScoreStale):
                self.scorer.validate_context(self.record, self.scorer.weight_snapshot())

    def test_refresh_reuses_identical_frozen_facts_and_queries_changed_record_once(self):
        with (
            patch.object(
                EchoRecord,
                'validation_issues',
                new_callable=PropertyMock,
                side_effect=AssertionError('Python quality'),
            ),
            patch.object(
                inventory_queries.compute, 'submit', wraps=inventory_queries.compute.submit
            ) as submit,
        ):
            self.repository.record = replace(self.record)
            state = self.scorer.refresh_record(self.record, self.quality, revision=7)
            self.assertIs(self.quality, state.quality)
            submit.assert_not_called()
            bad = replace(self.record, main_stats=())
            self.repository.record, self.repository.revision = bad, 8
            state = self.scorer.refresh_record(self.record, self.quality, revision=8)
            self.assertIs(bad, state.quality.record)
            self.assertFalse(state.quality.usable)
            self.assertIn('main:expected_two_slots', state.quality.issues)
            self.assertEqual(1, submit.call_count)

    def test_revision_race_cancellation_and_missing_record_do_not_publish_stale_quality(self):
        with self.assertRaises(QuickScoreStale):
            self.scorer.refresh_record(self.record, self.quality, revision=6)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            self.scorer.refresh_record(self.record, self.quality, revision=7, cancel_event=cancel)
        self.repository.record = replace(self.record, note='changed')
        original = inventory_queries.review

        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.repository.revision = 8
            return result

        with patch.object(inventory_queries, 'review', side_effect=race):
            with self.assertRaises(QuickScoreStale):
                self.scorer.refresh_record(self.record, self.quality, revision=7)
        self.repository.record = None
        state = self.scorer.refresh_record(self.record, self.quality, revision=8)
        self.assertIsNone(state.quality)
