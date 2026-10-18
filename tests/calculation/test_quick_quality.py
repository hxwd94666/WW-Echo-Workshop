# 验证快速评分只用原生质量结论、结构失效与后台刷新边界。
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from echo_workshop.application.quick_score import (
    QuickScoreStale,
    refresh_record,
    score_one,
    validate_context,
)
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.models import EchoRecord
from tests.native_runtime import ScoreService, WeightProvider, inventory_queries

FIXTURE = Path(__file__).with_name('fixtures') / 'review_quality.json'


def record_fixture():
    return EchoRecord.from_dict(
        json.loads(FIXTURE.read_text(encoding='utf-8'))['cases'][0]['record']
    )


def runtime_fixture(record, directory):
    weights = WeightProvider(directory)
    repository = SimpleNamespace(record=record, revision=7)
    repository.get = Mock(side_effect=lambda _: repository.record)
    return SimpleNamespace(
        repository=repository,
        weights=weights,
        scores=ScoreService(weights),
        inventory_queries=inventory_queries,
        stop_event=threading.Event(),
        _closed=False,
        inventory=SimpleNamespace(
            try_revision=lambda: repository.revision, get=lambda _: repository.record
        ),
        avatars=None,
    )


class QuickQuality(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.record = record_fixture()
        self.runtime = runtime_fixture(self.record, self.directory.name)
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
            result = score_one(self.runtime, self.record, ('1304', 'default'))
            self.assertEqual(29.96, result.comparison.score.current_score)
            self.assertEqual('', result.notice)
            validate_context(self.runtime, self.record, self.runtime.weights.active)
            for bad in (
                replace(self.record, review='pending'),
                replace(self.record, main_stats=()),
            ):
                self.runtime.repository.record = bad
                with self.assertRaisesRegex(ValueError, '核对'):
                    score_one(self.runtime, bad, ('1304', 'default'))
            with self.assertRaises(QuickScoreStale):
                validate_context(self.runtime, self.record, self.runtime.weights.active)

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
            self.runtime.repository.record = replace(self.record)
            state = refresh_record(self.runtime, self.record, self.quality, revision=7)
            self.assertIs(self.quality, state.quality)
            submit.assert_not_called()
            bad = replace(self.record, main_stats=())
            self.runtime.repository.record, self.runtime.repository.revision = bad, 8
            state = refresh_record(self.runtime, self.record, self.quality, revision=8)
            self.assertIs(bad, state.quality.record)
            self.assertFalse(state.quality.usable)
            self.assertIn('main:expected_two_slots', state.quality.issues)
            self.assertEqual(1, submit.call_count)

    def test_revision_race_cancellation_and_missing_record_do_not_publish_stale_quality(self):
        with self.assertRaises(QuickScoreStale):
            refresh_record(self.runtime, self.record, self.quality, revision=6)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            refresh_record(self.runtime, self.record, self.quality, revision=7, cancel_event=cancel)
        self.runtime.repository.record = replace(self.record, note='changed')
        original = inventory_queries.review

        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.runtime.repository.revision = 8
            return result

        with patch.object(inventory_queries, 'review', side_effect=race):
            with self.assertRaises(QuickScoreStale):
                refresh_record(self.runtime, self.record, self.quality, revision=7)
        self.runtime.repository.record = None
        state = refresh_record(self.runtime, self.record, self.quality, revision=8)
        self.assertIsNone(state.quality)
