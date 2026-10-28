# 将实时识别结果转换为策略请求，并为浮层生成最多两行摘要。
import json
from types import SimpleNamespace
import time

from ww_echo_workshop.application.policy import build_payload, costs_from_config, request_key
from ww_echo_workshop.core.policy import PolicyError, format_probability
from ww_echo_workshop.core.policy_presets import default_scoring

DECISION_LABELS = {
    'upgrade': '继续强化',
    'abandon': '放弃',
    'keep': '已达标',
    'reroll': '重抽',
}
LAYOUTS = ('enhancement', 'tuning', 'bag', 'display', 'single')


class LivePolicyHint:
    """有界防抖与单请求在途；旧请求结果按当前签名丢弃。"""

    def __init__(self, *, debounce=0.3):
        if type(debounce) not in (int, float) or not 0 <= debounce <= 5:
            raise ValueError('策略防抖须为 0–5 秒')
        self._debounce = float(debounce)
        self._signature = None
        self._future = None
        self._future_signature = None
        self._result = None
        self._error = ''
        self._error_signature = None
        self._changed_at = 0.0
        self._pending_signature = None
        self._pending_since = 0.0

    @property
    def last_result(self):
        return self._result

    def detail_lines(self):
        result = self._result
        if result is None:
            return ()
        lines = ['策略详情']
        for lock in result.locks[:5]:
            names = '、'.join(lock.types) or '不锁'
            lines.append(f'{names} 期望{lock.expected:.0f}频 {lock.probability:.0%}')
        if result.resources is not None:
            resources = result.resources
            lines.append(
                f'胚子{resources.echoes:.0f} 调谐{resources.tuners:.0f} '
                f'筒{resources.tubes:.0f} 频{resources.frequencies:.0f}'
            )
        return tuple(lines)

    def clear(self):
        self._signature = None
        self._future = None
        self._future_signature = None
        self._result = None
        self._error = ''
        self._error_signature = None
        self._changed_at = 0.0
        self._pending_signature = None
        self._pending_since = 0.0

    def observe(self, analysis, settings, snapshot, runtime, *, now=None):
        if not settings.get('启用强化策略', False):
            return ''
        if analysis is None or getattr(analysis, 'current_score', None) is None:
            return ''
        if getattr(analysis, 'layout', '') not in LAYOUTS:
            return ''
        target = settings.get('强化策略目标分', 0)
        if not isinstance(target, (int, float)) or target <= 0:
            return '策略：请先在设置目标分'
        if runtime is None or getattr(runtime, 'policy', None) is None:
            return '策略：计算服务未就绪'
        try:
            payload, signature = self._build(analysis, settings, snapshot)
        except PolicyError as error:
            return '策略：' + error.message
        now = time.monotonic() if now is None else now
        if signature != self._signature:
            if signature != self._pending_signature:
                self._pending_signature = signature
                self._pending_since = now
            if now - self._pending_since < self._debounce:
                if self._result is not None:
                    return self._format(self._result)
                return '策略：分析中…'
            self._signature = signature
            self._pending_signature = None
            self._result = None
            self._error = ''
            self._error_signature = None
            self._changed_at = now - self._debounce
        else:
            self._pending_signature = None
        if self._future is not None:
            if not self._future.done():
                return '策略：计算中…'
            future, future_signature = self._future, self._future_signature
            self._future = None
            self._future_signature = None
            if future_signature == signature:
                try:
                    self._result = future.result()
                except BaseException as error:
                    self._error = getattr(error, 'message', str(error))
                    self._error_signature = signature
        if self._result is not None:
            return self._format(self._result)
        if self._error_signature == signature:
            return '策略：' + self._error
        if now - self._changed_at < self._debounce:
            return '策略：分析中…'
        try:
            self._future = runtime.policy.submit(payload)
            self._future_signature = signature
        except BaseException as error:
            self._error = getattr(error, 'message', str(error))
            self._error_signature = signature
            return '策略：' + self._error
        return '策略：计算中…'

    def _build(self, analysis, settings, snapshot):
        system = settings.get('策略评分体系', 'workshop_xwuid')
        template = None
        if system == 'workshop_xwuid':
            key = self._selection(analysis, settings)
            if snapshot is None:
                raise PolicyError('invalid_template', '权重尚未就绪')
            try:
                template = snapshot.scoring_template(*key)
            except (KeyError, ValueError) as error:
                raise PolicyError('invalid_template', '评分模版已失效') from error
        cost = getattr(analysis, 'observed_cost', None) or getattr(analysis, 'cost', 0)
        if cost not in (1, 3, 4):
            raise PolicyError('invalid_record', 'COST 尚未识别')
        main = tuple(
            SimpleNamespace(stat_name=row.stat_name, value=float(row.value))
            for row in analysis.main_rows
        )
        sub = tuple(
            SimpleNamespace(stat_name=row.stat_name, value=float(row.value))
            for row in analysis.sub_rows
        )
        if len(main) != 2:
            raise PolicyError('invalid_record', '主词条未识别完整')
        record = SimpleNamespace(cost=cost, main_stats=main, sub_stats=sub)
        mode = settings.get('强化策略模式', 'auto')
        if mode not in ('auto', 'upgrade', 'reroll', 'joint'):
            mode = 'auto'
        if system == 'workshop_xwuid':
            payload = build_payload(
                template,
                record,
                target=float(settings.get('强化策略目标分', 0)),
                mode=mode,
                costs=costs_from_config(settings),
            )
        else:
            fixed = system == 'fixed'
            weights = self._weights(settings, system, fixed=fixed)
            payload = build_payload(
                None,
                record,
                target=float(settings.get('强化策略目标分', 0)),
                mode=mode,
                costs=costs_from_config(settings),
                scoring=system,
                weights=None if fixed else weights,
                fixed_weights=weights if fixed else None,
                main_score=float(settings.get('策略主词条分', 0.0) or 0.0),
                max_score=float(settings.get('策略归一满分', 100.0) or 100.0),
            )
        return payload, request_key(payload)

    @staticmethod
    def _weights(settings, system, *, fixed=False):
        raw = settings.get('策略评分权重')
        if raw:
            try:
                values = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError) as error:
                raise PolicyError('invalid_scoring', '评分权重不是有效 JSON') from error
        else:
            values = default_scoring(system)['weights']
        if not isinstance(values, (list, tuple)) or len(values) != 13:
            raise PolicyError('invalid_scoring', '评分权重需要 13 项')
        result = []
        for value in values:
            if type(value) not in (int, float) or value < 0 or value != value:
                raise PolicyError('invalid_scoring', '评分权重必须是非负有限数')
            if fixed and (not float(value).is_integer() or value > 65535):
                raise PolicyError('invalid_scoring', '固定评分权重必须是整数')
            result.append(int(value) if fixed else float(value))
        return result

    @staticmethod
    def _selection(analysis, settings):
        ids = tuple(getattr(analysis, 'selected_ids', ()) or ())
        if settings.get('自动匹配评分模板', False) and len(ids) == 2:
            return ids
        configured = (
            settings.get('TemplateCharacterID'),
            settings.get('TemplateModalID'),
        )
        if all(isinstance(value, str) and value for value in configured):
            return configured
        if len(ids) == 2:
            return ids
        raise PolicyError('invalid_template', '请先选择角色和模版')

    @staticmethod
    def _format(result):
        decision = DECISION_LABELS.get(result.decision, result.decision)
        probability = result.current_probability
        if probability is None and result.resources is not None:
            probability = result.resources.attempt_probability
        if result.mode == 'reroll' and result.decision == 'reroll':
            lock_types = result.lock_types
            locks = '、'.join(lock_types)
            if len(lock_types) > 2:
                locks = f'{len(lock_types)}条'
            first = f'策略：锁 {locks}' if locks else '策略：不锁'
            if probability is not None:
                first += f' · {format_probability(probability)}'
            if result.resources is None:
                return first
            return first + f'\n期望：{result.resources.frequencies:.0f} 频整器'
        first = f'策略：{decision}'
        if probability is not None:
            first += f' · {format_probability(probability)}'
        if result.resources is None:
            return first
        return first + f'\n期望：{result.resources.tuners:.0f}调 · {result.resources.tubes:.0f}筒'
