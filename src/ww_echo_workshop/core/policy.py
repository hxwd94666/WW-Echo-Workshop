# 定义强化/重抽策略的不可变结果与输入边界，不依赖宿主、UI 或计算进程。
from collections.abc import Mapping
from dataclasses import dataclass
import math

POLICY_SOURCE = 'Echo Studio / echo_policy 72ec29f · MIT'
DEFAULT_COSTS = (0.0, 1.0, 0.0, 1000.0)
DEFAULT_LIMITS = {'max_states': 2_000_000, 'max_transitions': 64_000_000}
RESOURCE_PRESETS = {
    'tuner': ('只看调谐器', (0.0, 1.0, 0.0, 1000.0)),
    'balanced': ('均衡', (1.0, 1.0, 1.0, 10.0)),
    'frequency': ('频整器稀缺', (0.0, 1.0, 0.0, 10000.0)),
}
MODES = ('auto', 'upgrade', 'reroll')
DECISIONS = ('upgrade', 'abandon', 'keep', 'reroll')


class PolicyError(Exception):
    """策略不可用或结果不可信；kind 供 UI 选择文案。"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class PolicyResources:
    echoes: float
    tuners: float
    tubes: float
    frequencies: float
    weighted: float
    attempt_probability: float


@dataclass(frozen=True)
class PolicyLock:
    mask: int
    types: tuple[str, ...]
    cost: int
    expected: float
    probability: float
    regret: float


@dataclass(frozen=True)
class PolicyComparison:
    accept: bool
    current_cost: float
    candidate_cost: float
    candidate_score: float


@dataclass(frozen=True)
class PolicyResult:
    mode: str
    decision: str
    current_score: float
    main_score: float
    sub_score: float
    target_internal: int
    lock_mask: int | None
    lock_types: tuple[str, ...]
    resources: PolicyResources | None
    current_probability: float | None
    locks: tuple[PolicyLock, ...]
    comparison: PolicyComparison | None
    allocated_bytes: int
    elapsed_ms: int
    note: str = ''
    scoring: str = 'workshop_xwuid'

    @classmethod
    def from_mapping(cls, data):
        if not isinstance(data, Mapping):
            raise PolicyError('protocol', '策略结果不是对象')
        mode = data.get('mode')
        decision = data.get('decision')
        scoring = data.get('scoring', 'workshop_xwuid')
        if not isinstance(scoring, str) or not scoring:
            raise PolicyError('protocol', '策略评分体系无效')
        if mode not in ('upgrade', 'reroll', 'joint'):
            raise PolicyError('protocol', '策略模式无效')
        if decision not in DECISIONS:
            raise PolicyError('protocol', '策略决策无效')
        target = _integer(data.get('target_internal'), 'target_internal', 0, 65535)
        lock_mask = data.get('lock_mask')
        if lock_mask is not None:
            lock_mask = _integer(lock_mask, 'lock_mask', 0, 0x1FFF)
        lock_types = _texts(data.get('lock_types'), 'lock_types')
        resources = _resources(data.get('resources'))
        comparison = _comparison(data.get('comparison'))
        locks = _locks(data.get('locks'))
        probability = _probability(data.get('current_probability'), 'current_probability')
        return cls(
            mode=mode,
            decision=decision,
            current_score=_number(data.get('current_score'), 'current_score', 0.0),
            main_score=_number(data.get('main_score'), 'main_score', 0.0),
            sub_score=_number(data.get('sub_score'), 'sub_score', 0.0),
            target_internal=target,
            lock_mask=lock_mask,
            lock_types=lock_types,
            resources=resources,
            current_probability=probability,
            locks=locks,
            comparison=comparison,
            allocated_bytes=_integer(data.get('allocated_bytes', 0), 'allocated_bytes', 0, None),
            elapsed_ms=_integer(data.get('elapsed_ms', 0), 'elapsed_ms', 0, None),
            note=str(data.get('note', '')),
            scoring=scoring,
        )


def mode_for_record(record, mode):
    """把 auto 解析为当前词条数对应的模式；其余值必须明确支持。"""
    count = len(record.sub_stats)
    if mode == 'auto':
        return 'upgrade' if count < 5 else 'reroll'
    if mode not in ('upgrade', 'reroll', 'joint'):
        raise PolicyError('unsupported_mode', '只支持强化、重抽与联合策略')
    if count > 5:
        raise PolicyError('invalid_record', '副词条不能超过五条')
    if mode == 'reroll' and count != 5:
        raise PolicyError('invalid_record', '重抽需要完整的五条副词条')
    return mode


def format_probability(value):
    """把 0–1 概率格式化为不易误导的百分比。"""
    if value is None:
        return '—'
    value = max(0.0, min(float(value), 1.0))
    if value == 0:
        return '0%'
    if value < 0.001:
        return '<0.1%'
    if value < 0.01:
        return '<1%'
    if value < 0.1:
        return f'{value * 100:.1f}%'
    return f'{value * 100:.0f}%'


def check_target(value, maximum=50.0):
    """目标分是含主词条的总分，用户必须显式设置。"""
    if type(value) not in (int, float) or isinstance(value, bool):
        raise PolicyError('target_unset', '请先设置目标分')
    if not math.isfinite(value):
        raise PolicyError('invalid_target', '目标分必须是有限数')
    if value <= 0:
        raise PolicyError('target_unset', '请先设置目标分')
    if value > maximum + 1e-9:
        raise PolicyError('invalid_target', f'目标分超出 0–{maximum:g}')
    return float(value)


def _number(value, name, minimum=None):
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value):
        raise PolicyError('protocol', f'策略结果字段无效：{name}')
    if minimum is not None and value < minimum:
        raise PolicyError('protocol', f'策略结果字段越界：{name}')
    return float(value)


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int:
        raise PolicyError('protocol', f'策略结果字段无效：{name}')
    if value < minimum or (maximum is not None and value > maximum):
        raise PolicyError('protocol', f'策略结果字段越界：{name}')
    return value


def _probability(value, name):
    if value is None:
        return None
    number = _number(value, name, 0.0)
    if number > 1.0 + 1e-9:
        raise PolicyError('protocol', f'策略结果字段越界：{name}')
    return min(number, 1.0)


def _texts(value, name):
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise PolicyError('protocol', f'策略结果字段无效：{name}')
    return tuple(value)


def _resources(value):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PolicyError('protocol', '策略资源字段无效')
    return PolicyResources(
        echoes=_number(value.get('echoes'), 'echoes', 0.0),
        tuners=_number(value.get('tuners'), 'tuners', 0.0),
        tubes=_number(value.get('tubes'), 'tubes', 0.0),
        frequencies=_number(value.get('frequencies'), 'frequencies', 0.0),
        weighted=_number(value.get('weighted'), 'weighted', 0.0),
        attempt_probability=_probability(value.get('attempt_probability'), 'attempt_probability')
        or 0.0,
    )


def _locks(value):
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PolicyError('protocol', '策略锁法字段无效')
    result = []
    for row in value:
        if not isinstance(row, Mapping):
            raise PolicyError('protocol', '策略锁法条目无效')
        result.append(
            PolicyLock(
                mask=_integer(row.get('mask'), 'lock.mask', 0, 0x1FFF),
                types=_texts(row.get('types'), 'lock.types'),
                cost=_integer(row.get('cost'), 'lock.cost', 1, 3),
                expected=_number(row.get('expected'), 'lock.expected', 0.0),
                probability=_probability(row.get('probability'), 'lock.probability') or 0.0,
                regret=_number(row.get('regret'), 'lock.regret', 0.0),
            )
        )
    return tuple(result)


def _comparison(value):
    if value is None:
        return None
    if not isinstance(value, Mapping) or type(value.get('accept')) is not bool:
        raise PolicyError('protocol', '策略候选字段无效')
    return PolicyComparison(
        accept=value['accept'],
        current_cost=_number(value.get('current_cost'), 'comparison.current', 0.0),
        candidate_cost=_number(value.get('candidate_cost'), 'comparison.candidate', 0.0),
        candidate_score=_number(value.get('candidate_score'), 'comparison.score', 0.0),
    )
