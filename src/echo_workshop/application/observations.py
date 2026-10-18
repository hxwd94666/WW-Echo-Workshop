# 将冻结收录词条批量提交 Rust，缓存数值投影而不缓存 OCR 几何或写入状态。
from collections import OrderedDict
from concurrent.futures import CancelledError
from dataclasses import dataclass
import threading

from echo_workshop.application.compute_bridge import plain, require_background, score_result
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled, checkpoint
from echo_workshop.core.models import canonical_json


@dataclass(frozen=True)
class ObservedTier:
    index: int
    total: int
    kind: str


@dataclass(frozen=True)
class ObservationResult:
    cost: int
    cost_key: str
    scores: tuple
    tiers: tuple

    def score_for(self, key):
        return next((score for identity, score in self.scores if identity == tuple(key)), None)


class ObservationService:
    def __init__(self, *, compute):
        self.compute = compute
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def evaluate(
        self,
        main_rows,
        sub_rows,
        *,
        selections,
        weight_version,
        cost=None,
        cost_key=None,
        cancel_event=None,
    ):
        require_background()
        checkpoint(cancel_event)
        # Only explicit stat names/values and frozen templates cross the process;
        # rectangles, screenshots, raw OCR, owners and storage are never sent.
        main, sub, selected = tuple(main_rows), tuple(sub_rows), tuple(selections)
        if len(main) > 16 or len(sub) > 16 or len(selected) > 128:
            raise ComputeFailure('input_limit', '观测输入超出单批规模限额')
        wire_selections = []
        for c, m, t in selected:
            checkpoint(cancel_event)
            wire_selections.append(dict(character_id=c, modal_id=m, template=plain(t)))
        payload = dict(
            weight_version=weight_version,
            cost=cost,
            cost_key=cost_key,
            main_rows=[dict(stat_name=r.stat_name, value=r.value) for r in main],
            sub_rows=[dict(stat_name=r.stat_name, value=r.value) for r in sub],
            selections=wire_selections,
        )
        checkpoint(cancel_event)
        key = canonical_json(payload)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
        if cached is not None:
            checkpoint(cancel_event)
            return cached
        job = begin_job('observation', metadata={'weight_version': weight_version})
        job.stage('computing')
        try:
            value = self.compute.submit(
                'observe_stats',
                lambda: payload,
                timeout=10,
                cancel_event=cancel_event,
            ).result(timeout=12)
            checkpoint(cancel_event)
            if value.get('weight_version') != weight_version:
                raise ComputeFailure('request_identity', '观测返回资料身份错误')
            rows, tiers = value['scores'], value['tiers']
            if len(rows) != len(selected) or len(tiers) != len(sub):
                raise ComputeFailure('protocol', '观测返回评分或档位数量错误')
            scores = []
            for row, (character, modal, _) in zip(rows, selected):
                checkpoint(cancel_event)
                identity = row['character_id'], row['modal_id']
                if identity != (character, modal):
                    raise ComputeFailure('request_identity', '观测返回角色身份或顺序错误')
                score = score_result(row['score'])
                if score is not None and (score.cost, score.cost_key) != (
                    value['cost'],
                    value['cost_key'],
                ):
                    raise ComputeFailure('protocol', '观测评分 COST 证据不一致')
                scores.append((identity, score))
            result = ObservationResult(
                value['cost'],
                value['cost_key'],
                tuple(scores),
                tuple(None if row is None else ObservedTier(**row) for row in tiers),
            )
            if (
                type(result.cost) is not int
                or result.cost not in (1, 3, 4)
                or not isinstance(result.cost_key, str)
            ):
                raise ComputeFailure('protocol', '观测返回 COST 结构错误')
            if (cost is not None and result.cost != cost) or (
                cost_key is not None and result.cost_key != cost_key
            ):
                raise ComputeFailure('request_identity', '观测返回显式 COST 或显示键错误')
            if any(
                t is not None
                and (
                    t.kind not in ('lowest', 'normal', 'highest')
                    or type(t.index) is not int
                    or type(t.total) is not int
                    or not 1 <= t.index <= t.total <= 32
                )
                for t in result.tiers
            ):
                raise ComputeFailure('protocol', '观测返回档位类型错误')
            checkpoint(cancel_event)
        except (CancelledError, OperationCancelled) as error:
            job.finish('cancelled')
            raise OperationCancelled('观测计算已取消') from error
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise OperationCancelled('观测计算已取消') from error
            job.finish('failed', error=error)
            raise
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            job.finish('failed', error=error)
            raise ComputeFailure('protocol', '观测计算返回结构错误') from error
        except Exception as error:
            job.finish('failed', error=error)
            raise
        # Eight small keys at most; a large frozen template is evaluated but
        # never retained as a multi-megabyte cache key in the host process.
        if len(key) <= 64 * 1024:
            with self._lock:
                self._cache[key] = result
                self._cache.move_to_end(key)
                while len(self._cache) > 8:
                    self._cache.popitem(last=False)
        job.finish('completed', counters={'roles': len(selected), 'stats': len(main) + len(sub)})
        return result
