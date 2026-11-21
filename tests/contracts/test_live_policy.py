# 校验实时策略摘要的防抖、请求构建、结果格式与旧状态丢弃。
from concurrent.futures import Future
from types import MappingProxyType, SimpleNamespace
import unittest

from ww_echo_workshop.adapters.okww.live_policy import LivePolicyHint
from ww_echo_workshop.adapters.okww.live_presentation import live_summary
from ww_echo_workshop.core.policy import PolicyResult


def _stat(name, value):
    return SimpleNamespace(stat_name=name, value=value)


def _template():
    return MappingProxyType(
        {
            'score_max': [67.96, 71.509, 75.509],
            'sub_props': MappingProxyType({'暴击': 1.0, '暴击伤害': 1.0, '攻击%': 1.0}),
            'skill_weight': [0.0, 0.0, 0.0, 0.0],
            'main_props': {4: {'暴击': 0.5}},
            'name': '测试模版',
        }
    )


class Snapshot:
    def scoring_template(self, character, modal):
        return _template()


class DeferredPolicy:
    def __init__(self):
        self.calls = []
        self.future = None

    def submit(self, payload):
        self.calls.append(payload)
        self.future = Future()
        return self.future


class FakePolicy:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def submit(self, payload):
        self.calls.append(payload)
        future = Future()
        future.set_result(self.result)
        return future


def _analysis(count=2):
    subs = (
        _stat('暴击', 6.3),
        _stat('暴击伤害', 12.6),
        _stat('攻击%', 6.4),
        _stat('共鸣效率', 6.8),
        _stat('攻击', 30.0),
    )[:count]
    return SimpleNamespace(
        current_score=21.4,
        layout='enhancement',
        cost=4,
        observed_cost=4,
        main_rows=(_stat('暴击', 22.0), _stat('攻击', 150.0)),
        sub_rows=subs,
        selected_ids=('1601', 'default'),
    )


def _result(mode='upgrade', decision='upgrade'):
    return PolicyResult.from_mapping(
        {
            'mode': mode,
            'decision': decision,
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
    )


def _settings(**overrides):
    values = {
        '启用强化策略': True,
        '强化策略目标分': 35.0,
        '强化策略模式': 'auto',
        '强化策略资源预设': 'tuner',
        '自动匹配评分模板': False,
        'TemplateCharacterID': '1601',
        'TemplateModalID': 'default',
    }
    values.update(overrides)
    return values


class LivePolicyHintContract(unittest.TestCase):
    def test_non_workshop_build_uses_manual_weights(self):
        hint = LivePolicyHint()
        weights = '[' + ','.join(['1'] * 13) + ']'
        settings = _settings(策略评分体系='linear', 策略评分权重=weights)
        payload, _ = hint._build(_analysis(2), settings, None)
        self.assertEqual(payload['scoring'], 'linear')
        self.assertEqual(payload['target_internal'], 3500)
        self.assertEqual(payload['weights'], [1.0] * 13)
        self.assertNotIn('template', payload)

    def test_target_missing_is_reported_without_request(self):
        policy = FakePolicy(_result())
        runtime = SimpleNamespace(policy=policy)
        hint = LivePolicyHint()
        text = hint.observe(_analysis(), _settings(**{'强化策略目标分': 0}), Snapshot(), runtime)
        self.assertIn('目标分', text)
        self.assertEqual(policy.calls, [])

    def test_debounce_then_formats_upgrade(self):
        policy = FakePolicy(_result())
        runtime = SimpleNamespace(policy=policy)
        hint = LivePolicyHint(debounce=0.3)
        self.assertIn(
            '计算中', hint.observe(_analysis(), _settings(), Snapshot(), runtime, now=0.0)
        )
        text = hint.observe(_analysis(), _settings(), Snapshot(), runtime, now=1.0)
        self.assertIn('计算中', text)
        self.assertEqual(len(policy.calls), 1)
        self.assertEqual(policy.calls[0]['mode'], 'upgrade')
        text = hint.observe(_analysis(), _settings(), Snapshot(), runtime, now=1.1)
        self.assertIn('继续强化', text)
        self.assertIn('调 ·', text)
        self.assertIn('筒', text)

    def test_five_stats_use_reroll_and_lock_line(self):
        result = PolicyResult.from_mapping(
            {
                'mode': 'reroll',
                'decision': 'reroll',
                'current_score': 21.4,
                'main_score': 7.28,
                'sub_score': 14.12,
                'target_internal': 2800,
                'lock_mask': 3,
                'lock_types': ['暴击', '暴击伤害'],
                'resources': {
                    'echoes': 0.0,
                    'tuners': 0.0,
                    'tubes': 0.0,
                    'frequencies': 6.5,
                    'weighted': 6.5,
                    'attempt_probability': 0.21,
                },
                'current_probability': 0.21,
                'locks': [],
                'comparison': None,
                'allocated_bytes': 1024,
                'elapsed_ms': 12,
            }
        )
        policy = FakePolicy(result)
        runtime = SimpleNamespace(policy=policy)
        hint = LivePolicyHint(debounce=0)
        hint.observe(_analysis(5), _settings(), Snapshot(), runtime, now=0.0)
        text = hint.observe(_analysis(5), _settings(), Snapshot(), runtime, now=0.1)
        self.assertIn('锁', text)
        self.assertIn('暴击', text)
        self.assertEqual(policy.calls[0]['mode'], 'reroll')

    def test_only_one_request_is_in_flight_across_state_changes(self):
        policy = DeferredPolicy()
        runtime = SimpleNamespace(policy=policy)
        hint = LivePolicyHint(debounce=0)
        hint.observe(_analysis(2), _settings(), Snapshot(), runtime, now=0.0)
        self.assertEqual(len(policy.calls), 1)
        hint.observe(_analysis(5), _settings(), Snapshot(), runtime, now=0.1)
        self.assertEqual(len(policy.calls), 1)
        policy.future.set_result(_result())
        hint.observe(_analysis(5), _settings(), Snapshot(), runtime, now=0.2)
        self.assertEqual(len(policy.calls), 2)

    def test_new_item_does_not_inherit_previous_advice_during_debounce(self):
        policy = FakePolicy(_result())
        runtime = SimpleNamespace(policy=policy)
        hint = LivePolicyHint(debounce=0.3)
        hint.observe(_analysis(2), _settings(), Snapshot(), runtime, now=0.0)
        hint.observe(_analysis(2), _settings(), Snapshot(), runtime, now=0.31)
        self.assertIn(
            '继续强化', hint.observe(_analysis(2), _settings(), Snapshot(), runtime, now=0.32)
        )
        text = hint.observe(_analysis(5), _settings(), Snapshot(), runtime, now=0.40)
        self.assertEqual('策略：计算中…', text)
        self.assertIsNone(hint.last_result)
        self.assertEqual(len(policy.calls), 1)

    def test_summary_appends_policy_lines(self):
        analysis = SimpleNamespace(
            current_score=21.0,
            potential_score=30.0,
            selected_template='测试模版',
            summary='',
            score_text='',
            observed_cost=4,
            rating_color=None,
        )
        text, colours = live_summary(analysis, policy_text='策略：继续强化\n期望：42 调谐器')
        self.assertIn('策略：继续强化', text)
        self.assertEqual(len(colours), len(text.splitlines()))
        self.assertEqual(colours[-2], ((125, 211, 252),))
        self.assertEqual(colours[-1], ((125, 211, 252),))

    def test_detail_lines_include_locks_and_resources(self):
        result = PolicyResult.from_mapping(
            {
                'mode': 'reroll',
                'decision': 'reroll',
                'current_score': 9.19,
                'main_score': 0.0,
                'sub_score': 9.19,
                'target_internal': 3000,
                'lock_mask': 3,
                'lock_types': ['暴击', '暴伤'],
                'resources': {
                    'echoes': 0.0,
                    'tuners': 0.0,
                    'tubes': 0.0,
                    'frequencies': 12.4,
                    'weighted': 12.4,
                    'attempt_probability': 0.2,
                },
                'current_probability': 0.2,
                'locks': [
                    {
                        'mask': 3,
                        'types': ['暴击', '暴伤'],
                        'cost': 3,
                        'expected': 12.4,
                        'probability': 0.2,
                        'regret': 0.0,
                    }
                ],
            }
        )
        hint = LivePolicyHint()
        hint._result = result
        lines = hint.detail_lines()
        self.assertEqual(lines[0], '策略详情')
        self.assertIn('暴击', lines[1])
        self.assertIn('12', lines[-1])

    def test_comparison_has_left_and_right_policy_pairs(self):
        analysis = SimpleNamespace(
            current_score=14.71,
            potential_score=30.0,
            selected_template='右侧',
            summary='',
            score_text='',
            observed_cost=4,
            rating_color=None,
        )
        reference = SimpleNamespace(
            current_score=14.71,
            potential_score=30.0,
            selected_template='左侧',
            summary='',
            score_text='',
            observed_cost=4,
            rating_color=None,
        )
        left = '策略：继续强化 · 5%\n期望：100调 · 2筒'
        right = '策略：锁 暴击、暴伤 · 7%\n期望：12 频整器'
        text, colours = live_summary(analysis, reference, right, left)
        lines = text.splitlines()
        self.assertEqual(
            lines[-4:],
            [
                '左策略：继续强化 · 5%',
                '期望：100调 · 2筒',
                '右策略：锁 暴击、暴伤 · 7%',
                '期望：12 频整器',
            ],
        )
        self.assertEqual(colours[-4], ((125, 211, 252),))

    def test_header_and_row_budget_are_constant_even_with_one_comparison_side_missing(self):
        analysis = SimpleNamespace(
            current_score=21.0,
            potential_score=30.0,
            selected_template='测试模版',
            summary='',
            score_text='',
            observed_cost=4,
            rating_color=None,
            layout='display',
            comparison=True,
        )
        waiting, wait_colors = live_summary(analysis, policy_enabled=True)
        ready, ready_colors = live_summary(
            analysis,
            analysis,
            '策略：继续强化\n期望：10调',
            '策略：已达标',
            policy_enabled=True,
        )
        self.assertEqual(len(ready.splitlines()), len(waiting.splitlines()))
        self.assertEqual(
            waiting.splitlines().index('强化策略'), ready.splitlines().index('强化策略')
        )
        self.assertEqual(len(wait_colors), len(ready_colors))
        self.assertIn('-- / 21.00', waiting)
        self.assertEqual(
            waiting.splitlines()[-4:],
            ['左策略：计算中…', '期望：计算中…', '右策略：计算中…', '期望：计算中…'],
        )
        self.assertIn('期望：—', ready.splitlines())
        blocked, _ = live_summary(
            analysis, policy_text='策略：请先在设置目标分', policy_enabled=True
        )
        self.assertEqual(blocked.splitlines()[-2:], ['右策略：请先在设置目标分', '期望：—'])
        self.assertNotIn('强化策略', live_summary(analysis, policy_enabled=False)[0])
        analysis.current_score = None
        identifying, _ = live_summary(analysis, policy_enabled=True)
        self.assertIn('强化策略', identifying)
        self.assertEqual(len(waiting.splitlines()), len(identifying.splitlines()))
        analysis.layout = ''
        self.assertEqual(('', ()), live_summary(analysis, policy_enabled=True))

    def test_request_uses_fixed_template_when_auto_match_disabled(self):
        policy = FakePolicy(_result())
        runtime = SimpleNamespace(policy=policy)
        hint = LivePolicyHint(debounce=0)
        hint.observe(_analysis(), _settings(), Snapshot(), runtime, now=0.0)
        self.assertEqual(policy.calls[0]['template']['name'], '测试模版')
