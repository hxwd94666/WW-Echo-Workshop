# 校验策略请求构建、结果解析、缓存去重与 policy 队列入口。
from concurrent.futures import Future
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import unittest

from ww_echo_workshop.application.compute_protocol import ComputeWorker
from ww_echo_workshop.application.policy import (
    PolicyService,
    build_payload,
    costs_from_config,
    request_key,
)
from ww_echo_workshop.core.policy import PolicyError, PolicyResult


def _stat(name, value):
    return SimpleNamespace(stat_name=name, value=value)


def _record(count=2, cost=4):
    subs = (
        _stat('暴击', 6.3),
        _stat('暴击伤害', 12.6),
        _stat('攻击%', 6.4),
        _stat('共鸣效率', 6.8),
        _stat('攻击', 30.0),
    )[:count]
    return SimpleNamespace(
        cost=cost,
        main_stats=(_stat('暴击', 22.0), _stat('攻击', 150.0)),
        sub_stats=subs,
    )


def _template():
    return MappingProxyType(
        {
            'score_max': [67.96, 71.509, 75.509],
            'sub_props': MappingProxyType({'暴击': 1.0, '暴击伤害': 1.0, '攻击%': 1.0}),
            'skill_weight': [0.0, 0.0, 0.0, 0.0],
            'main_props': {4: {'暴击': 0.5}},
        }
    )


def _result():
    return {
        'mode': 'upgrade',
        'decision': 'upgrade',
        'current_score': 21.4,
        'main_score': 7.28,
        'sub_score': 14.12,
        'target_internal': 2800,
        'lock_mask': None,
        'lock_types': [],
        'resources': {
            'echoes': 3.0,
            'tuners': 42.0,
            'tubes': 5.0,
            'frequencies': 0.0,
            'weighted': 42.0,
            'attempt_probability': 0.2,
        },
        'current_probability': 0.2,
        'locks': [],
        'comparison': None,
        'allocated_bytes': 1024,
        'elapsed_ms': 12,
    }


class FakeCompute:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def submit(self, op, builder, *, lane, timeout, cancel_event=None):
        self.calls.append((op, lane, builder(), timeout))
        future = Future()
        future.set_result(self.result)
        return future


class DeferredCompute:
    def __init__(self):
        self.calls = 0
        self.future = None

    def submit(self, op, builder, *, lane, timeout, cancel_event=None):
        self.calls += 1
        self.future = Future()
        return self.future


class PolicyServiceContract(unittest.TestCase):
    def test_payload_maps_record_and_auto_mode(self):
        payload = build_payload(_template(), _record(2), target=35.0)
        self.assertEqual(payload['mode'], 'upgrade')
        self.assertEqual(payload['cost'], 4)
        self.assertEqual(payload['target'], 35.0)
        self.assertEqual(payload['main'][0]['stat_name'], '暴击')
        self.assertEqual(payload['sub'][1]['value'], 12.6)
        self.assertIsInstance(payload['template'], dict)

    def test_non_workshop_payload_carries_weights_and_internal_target(self):
        weights = [1.0] * 13
        payload = build_payload(
            None,
            _record(2),
            target=70.0,
            scoring='linear',
            weights=weights,
            main_score=0.0,
            max_score=100.0,
        )
        self.assertEqual(payload['scoring'], 'linear')
        self.assertEqual(payload['target_internal'], 7000)
        self.assertEqual(payload['weights'], weights)
        self.assertNotIn('template', payload)

    def test_reroll_candidate_is_carried(self):
        candidate = _record(5)
        payload = build_payload(
            _template(), _record(5), target=35.0, mode='reroll', candidate=candidate
        )
        self.assertEqual(len(payload['candidate']), 5)
        self.assertEqual(payload['candidate'][0]['stat_name'], '暴击')

    def test_fixed_payload_uses_integer_weights_and_target(self):
        payload = build_payload(
            None,
            _record(2),
            target=7.0,
            scoring='fixed',
            fixed_weights=[3, 3, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0],
        )
        self.assertEqual(payload['fixed_weights'][0], 3)
        self.assertEqual(payload['target_internal'], 7)

    def test_auto_mode_switches_to_reroll_at_five(self):
        self.assertEqual(build_payload(_template(), _record(5), target=35.0)['mode'], 'reroll')

    def test_missing_target_is_rejected_before_compute(self):
        with self.assertRaises(PolicyError) as error:
            build_payload(_template(), _record(2), target=0)
        self.assertEqual(error.exception.kind, 'target_unset')

    def test_joint_mode_is_exposed(self):
        payload = build_payload(_template(), _record(2), target=35.0, mode='joint')
        self.assertEqual(payload['mode'], 'joint')

    def test_result_parsing_and_invalid_decision(self):
        result = PolicyResult.from_mapping(_result())
        self.assertEqual(result.decision, 'upgrade')
        self.assertEqual(result.scoring, 'workshop_xwuid')
        self.assertEqual(result.resources.tuners, 42.0)
        bad = _result()
        bad['decision'] = 'guess'
        with self.assertRaises(PolicyError):
            PolicyResult.from_mapping(bad)

    def test_service_caches_and_reuses_request(self):
        compute = FakeCompute(_result())
        service = PolicyService(compute, cache_entries=2)
        payload = build_payload(_template(), _record(2), target=35.0)
        first = service.solve(payload)
        second = service.solve(payload)
        self.assertEqual(first.decision, 'upgrade')
        self.assertIs(first, second)
        self.assertEqual(len(compute.calls), 1)
        self.assertEqual(compute.calls[0][0], 'policy_solve')
        self.assertEqual(compute.calls[0][1], 'policy')

    def test_pending_request_is_deduplicated(self):
        compute = DeferredCompute()
        service = PolicyService(compute)
        payload = build_payload(_template(), _record(2), target=35.0)
        first = service.submit(payload)
        second = service.submit(payload)
        self.assertIs(first, second)
        self.assertEqual(compute.calls, 1)
        compute.future.set_result(_result())
        self.assertEqual(first.result().decision, 'upgrade')

    def test_resource_weights_parse_and_fallback_to_preset(self):
        self.assertEqual(costs_from_config({'强化策略资源权重': '[2,3,4,5]'}), (2.0, 3.0, 4.0, 5.0))
        self.assertEqual(
            costs_from_config({'强化策略资源权重': 'bad', '强化策略资源预设': 'balanced'}),
            (1.0, 1.0, 1.0, 10.0),
        )

    def test_request_key_is_stable_and_changes_with_target(self):
        template = _template()
        first = build_payload(template, _record(2), target=35.0)
        same = build_payload(template, _record(2), target=35.0)
        changed = build_payload(template, _record(2), target=36.0)
        self.assertEqual(request_key(first), request_key(same))
        self.assertNotEqual(request_key(first), request_key(changed))

    def test_policy_lane_is_accepted_by_worker(self):
        worker = ComputeWorker(executable=Path('missing.exe'), lane='policy')
        self.assertEqual(worker.lane, 'policy')
