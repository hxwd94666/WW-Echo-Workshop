# 构建策略请求并管理缓存、在途请求与结果解析，不触碰 Qt 或宿主控件。
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeout
import hashlib
import json
import math
import threading

from ww_echo_workshop.application.compute_bridge import plain
from ww_echo_workshop.application.compute_protocol import ComputeFailure
from ww_echo_workshop.core.policy import (
    DEFAULT_COSTS,
    DEFAULT_LIMITS,
    RESOURCE_PRESETS,
    PolicyError,
    PolicyResult,
    check_target,
    mode_for_record,
)
from ww_echo_workshop.core.policy_presets import SYSTEMS

POLICY_LANE = 'policy'
POLICY_TIMEOUT = 60.0
_DEFAULT_COSTS = tuple(DEFAULT_COSTS)
_DEFAULT_LIMITS = dict(DEFAULT_LIMITS)


def build_payload(
    template,
    record,
    *,
    target,
    mode='auto',
    costs=_DEFAULT_COSTS,
    limits=_DEFAULT_LIMITS,
    scoring='workshop_xwuid',
    weights=None,
    fixed_weights=None,
    main_score=0.0,
    max_score=100.0,
    target_internal=None,
    candidate=None,
):
    """把一条声骸与明确目标转换为策略请求；缺目标或缺权重时拒绝而不是猜。"""
    resolved = mode_for_record(record, mode)
    if scoring != 'workshop_xwuid' and scoring not in SYSTEMS:
        raise PolicyError('invalid_scoring', '未知评分体系')
    if scoring == 'workshop_xwuid':
        target = check_target(target)
        if template is None:
            raise PolicyError('invalid_template', '没有可用的评分模板')
    else:
        maximum = 65535.0 if scoring == 'fixed' else float(max_score)
        target = check_target(target, maximum)
        if not math.isfinite(float(main_score)) or float(main_score) < 0:
            raise PolicyError('invalid_scoring', '主词条分必须是非负有限数')
        if not math.isfinite(float(max_score)) or float(max_score) <= 0:
            raise PolicyError('invalid_scoring', '归一满分必须是正有限数')
    if type(record.cost) is not int or record.cost not in (1, 3, 4):
        raise PolicyError('invalid_record', '声骸 COST 无效')
    main = [{'stat_name': stat.stat_name, 'value': stat.value} for stat in record.main_stats]
    sub = [{'stat_name': stat.stat_name, 'value': stat.value} for stat in record.sub_stats]
    if len(main) != 2:
        raise PolicyError('invalid_record', '策略需要两条主词条')
    if len(sub) > 5:
        raise PolicyError('invalid_record', '副词条不能超过五条')
    if resolved == 'reroll' and len(sub) != 5:
        raise PolicyError('invalid_record', '重抽需要完整的五条副词条')
    candidate_rows = None
    if candidate is not None:
        if resolved != 'reroll':
            raise PolicyError('invalid_record', '只有重抽支持候选比较')
        rows = getattr(candidate, 'sub_stats', candidate)
        if not isinstance(rows, (list, tuple)) or len(rows) != 5:
            raise PolicyError('invalid_record', '候选需要完整的五条副词条')
        candidate_rows = [{'stat_name': stat.stat_name, 'value': stat.value} for stat in rows]
    normalized_costs = _costs(costs)
    normalized_limits = _limits(limits)
    payload = {
        'cost': record.cost,
        'main': main,
        'sub': sub,
        'mode': resolved,
        'target': target,
        'costs': list(normalized_costs),
        'limits': normalized_limits,
        'scoring': scoring,
    }
    if candidate_rows is not None:
        payload['candidate'] = candidate_rows
    if scoring == 'workshop_xwuid':
        payload['template'] = plain(template)
        return payload
    if scoring == 'fixed':
        values = _fixed_weights(fixed_weights)
        payload['fixed_weights'] = list(values)
    else:
        payload['weights'] = list(_weights(weights))
    if target_internal is None:
        internal = (
            round(float(target))
            if scoring == 'fixed'
            else round((target - float(main_score)) * 100.0)
        )
    else:
        if type(target_internal) is not int:
            raise PolicyError('invalid_target', '内部目标分必须是整数')
        internal = target_internal
    if internal < 0 or internal > 65535:
        raise PolicyError('invalid_target', '目标分超出策略支持范围')
    payload.update(
        {
            'main_score': float(main_score),
            'max_score': float(max_score),
            'target_internal': internal,
        }
    )
    return payload


def costs_from_config(config):
    """读取四个资源的相对价值；旧配置回退到预设。"""
    raw = config.get('强化策略资源权重')
    values = None
    if isinstance(raw, str) and raw.strip():
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            values = None
    elif isinstance(raw, (list, tuple)):
        values = raw
    if isinstance(values, (list, tuple)) and len(values) == 4:
        try:
            costs = tuple(float(value) for value in values)
        except (TypeError, ValueError):
            costs = ()
        if len(costs) == 4 and all(math.isfinite(value) and value >= 0 for value in costs):
            return costs
    preset = config.get('强化策略资源预设', 'tuner')
    return tuple(RESOURCE_PRESETS.get(preset, RESOURCE_PRESETS['tuner'])[1])


def request_key(payload):
    """请求内容哈希；模板、目标、资源权重或词条变化都会失效。"""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    )
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _weights(values):
    if not isinstance(values, (list, tuple)) or len(values) != 13:
        raise PolicyError('invalid_scoring', '评分权重需要 13 项')
    result = []
    for value in values:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise PolicyError('invalid_scoring', '评分权重必须是非负有限数')
        result.append(float(value))
    return tuple(result)


def _fixed_weights(values):
    if not isinstance(values, (list, tuple)) or len(values) != 13:
        raise PolicyError('invalid_scoring', '固定评分需要 13 个整数权重')
    result = []
    for value in values:
        if type(value) is float and value.is_integer():
            value = int(value)
        if type(value) is not int or value < 0 or value > 65535:
            raise PolicyError('invalid_scoring', '固定评分权重必须是 0–65535 的整数')
        result.append(value)
    return tuple(result)


def _costs(values):
    try:
        costs = tuple(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise PolicyError('invalid_costs', '资源权重无效') from error
    if len(costs) != 4 or any(not math.isfinite(value) or value < 0 for value in costs):
        raise PolicyError('invalid_costs', '资源权重必须是非负有限数')
    return costs


def _limits(values):
    limits = dict(_DEFAULT_LIMITS)
    if values is None:
        return limits
    if not isinstance(values, dict):
        raise PolicyError('invalid_limits', '策略规模限制无效')
    for name in ('max_states', 'max_transitions'):
        if name in values:
            value = values[name]
            if type(value) is not int or value <= 0:
                raise PolicyError('invalid_limits', '策略规模限制必须是正整数')
            limits[name] = value
    return limits


class PolicyService:
    """共享一个 ComputeJobs；缓存成功结果，同一请求只发起一次计算。"""

    def __init__(self, compute, *, cache_entries=8):
        if type(cache_entries) is not int or not 0 <= cache_entries <= 64:
            raise ValueError('策略缓存条目须为 0–64')
        self.compute = compute
        self._cache_entries = cache_entries
        self._cache = OrderedDict()
        self._pending = {}
        self._lock = threading.RLock()

    def close(self):
        with self._lock:
            self._cache.clear()
            self._pending.clear()

    def clear(self):
        with self._lock:
            self._cache.clear()

    def submit(self, payload, *, lane=POLICY_LANE, timeout=POLICY_TIMEOUT, cancel_event=None):
        key = request_key(payload)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return _completed(cached)
            pending = self._pending.get(key)
            if pending is not None:
                return pending
            future = Future()
            self._pending[key] = future
        try:
            raw = self.compute.submit(
                'policy_solve',
                lambda: payload,
                lane=lane,
                timeout=timeout,
                cancel_event=cancel_event,
            )
        except BaseException as error:
            with self._lock:
                self._pending.pop(key, None)
            future.set_exception(_failure(error))
            return future

        def finish(source):
            try:
                value = PolicyResult.from_mapping(source.result())
            except BaseException as error:
                future.set_exception(_failure(error))
            else:
                with self._lock:
                    self._store(key, value)
                future.set_result(value)
            finally:
                with self._lock:
                    self._pending.pop(key, None)

        raw.add_done_callback(finish)
        return future

    def solve(self, payload, *, lane=POLICY_LANE, timeout=POLICY_TIMEOUT, cancel_event=None):
        future = self.submit(payload, lane=lane, timeout=timeout, cancel_event=cancel_event)
        try:
            return future.result(timeout=timeout + 5.0)
        except FutureTimeout as error:
            raise PolicyError('timeout', '策略计算超时') from error

    def _store(self, key, value):
        if not self._cache_entries:
            return
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_entries:
            self._cache.popitem(last=False)


def _completed(value):
    future = Future()
    future.set_result(value)
    return future


def _failure(error):
    if isinstance(error, PolicyError):
        return error
    if isinstance(error, ComputeFailure):
        return PolicyError(getattr(error, 'kind', 'compute'), str(error))
    return PolicyError('compute', str(error))
