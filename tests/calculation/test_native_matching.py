# 使用冻结证据验证 Rust 匹配矩阵及库存边界判断。
import copy
import json
from pathlib import Path
import unittest

from echo_workshop.application.compute_protocol import ComputeFailure, ComputeWorker

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


class TestNativeMatching(unittest.TestCase):
    def test_frozen_precheck_order_filter_chain_and_unique_set_names(self):
        data = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_precheck.json').read_text(encoding='utf-8')
        )
        with ComputeWorker(ENGINE) as worker:
            result = worker.call('allocation_precheck', data['payload'])
        self.assertEqual(data['expected'], result)

    def test_frozen_multi_role_scores_compatibility_and_exclusion_reasons(self):
        data = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_matching.json').read_text(encoding='utf-8')
        )
        with ComputeWorker(ENGINE) as worker:
            result = worker.call('match_batch', data['payload'])
        self.assertEqual(data['expected'], result)
        print('RUST_MATCHING_GOLDEN_OK:', len(result['cells']))

    def test_duplicate_uuid_and_unknown_rule_fail_explicitly(self):
        data = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_matching.json').read_text(encoding='utf-8')
        )
        duplicate = copy.deepcopy(data['payload'])
        duplicate['records'].append(duplicate['records'][0])
        with ComputeWorker(ENGINE) as worker:
            with self.assertRaises(ComputeFailure) as error:
                worker.call('match_batch', duplicate)
            self.assertEqual('invalid_request', error.exception.kind)
        bad = copy.deepcopy(data['payload'])
        bad['selections'][0]['rule']['element'] = '未知属性'
        with ComputeWorker(ENGINE) as worker:
            with self.assertRaises(ComputeFailure) as error:
                worker.call('match_batch', bad)
            self.assertEqual('invalid_rule', error.exception.kind)
