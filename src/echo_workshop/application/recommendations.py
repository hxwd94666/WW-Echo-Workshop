# 生成实时单件声骸的评分投影。
"""Small current-page comparisons with explicit rules, never inventory writes."""

from dataclasses import dataclass
import json

from echo_workshop.core.compatibility import CompatibilityRule
from concurrent.futures import CancelledError
from echo_workshop.application.compute_bridge import (
    plain,
    record_payload,
    require_background,
    score_result,
)
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.operations import checkpoint, OperationCancelled
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.resources.weights import ALGORITHM_VERSION
from echo_workshop.resources.weights import WeightSnapshot


@dataclass(frozen=True)
class CurrentComparison:
    character_id: str
    modal_id: str
    label: str
    current_score: float
    potential_score: float
    state: str
    weight_version: str
    algorithm_version: str = ALGORITHM_VERSION
    rating: object = None


@dataclass(frozen=True)
class RecommendationResult:
    rows: tuple[CurrentComparison, ...]
    message: str = ''

    def text(self):
        lines = ['当前件跨模版基础分（+25口径，非配装方案）：']
        states = {
            'match': '元素/套装匹配，当前页待核对',
            'unknown': '适配未知，仅基础分',
            'mismatch': '不匹配，勿按高分推荐',
        }
        for row in self.rows:
            label = row.label if len(row.label) <= 24 else row.label[:23] + '…'
            modal = row.modal_id if len(row.modal_id) <= 20 else row.modal_id[:19] + '…'
            grade = ' · ' + row.rating.label if row.rating is not None else ''
            lines.append(
                f'{label} [{row.character_id}/{modal}] {row.current_score:.2f}{grade} · {states[row.state]}'
            )
            if row.rating is not None:
                bounds = ' '.join(
                    f'{level}≥{lower:g}'
                    for level, lower in zip(
                        ('C', 'B', 'A', 'S', 'SS', 'SSS'), row.rating.thresholds
                    )
                )
                lines.append('评级标准：' + bounds)
        if self.message:
            lines.append(self.message)
        return '\n'.join(lines) if self.rows else self.message


def comparison_choices(raw, profile):
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise ValueError('比较配置格式/大小无效')
    if not raw:
        return (), {}
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or type(data.get('schema_version')) is not int
        or data['schema_version'] != 1
        or not isinstance(data.get('profiles'), dict)
    ):
        raise ValueError('比较配置不兼容，原配置保留')
    row = data['profiles'].get(profile, {})
    selected = row.get('selections', [])
    if (
        not isinstance(selected, (list, tuple))
        or len(selected) > 128
        or any(
            not isinstance(key, (list, tuple))
            or len(key) != 2
            or not all(isinstance(part, str) and part for part in key)
            for key in selected
        )
    ):
        raise ValueError('当前页比较选择格式无效或超过 128 个模版')
    keys = tuple(dict.fromkeys(tuple(key) for key in selected))
    rules = {}
    for rule in row.get('rules', []):
        key = rule['character_id'], rule['modal_id']
        rules[key] = CompatibilityRule.from_dict(
            {name: rule[name] for name in ('element', 'sonatas')}
        )
    return keys, rules


class RecommendationService:
    def __init__(self, *, compute):
        self.compute = compute
        self._key = None
        self._result = None
        self._live_key = self._live_result = None
        self.calculations = 0

    def _evaluate(
        self, record, raw, profile, snapshot, *, analysis=None, limit=3, cancel_event=None
    ):
        require_background()
        checkpoint(cancel_event)
        if not isinstance(snapshot, WeightSnapshot):
            raise ValueError('当前件比较需要冻结权重快照')
        config_error = ''
        try:
            choices, rules = comparison_choices(raw, profile)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            if analysis is not None:
                raise
            choices, rules = (), {}
            config_error = '当前页比较配置待修正：' + str(error)
        selections, missing = [], False
        for character, modal in choices:
            checkpoint(cancel_event)
            try:
                template = snapshot.scoring_template(character, modal)
            except ValueError:
                missing = True
                continue  # A deleted selection never silently substitutes another modal.
            selections.append(
                dict(
                    character_id=character,
                    modal_id=modal,
                    template=plain(template),
                    rule=snapshot.compatibility_rule(
                        character, rules.get((character, modal))
                    ).to_dict(),
                )
            )
            # Live mode displays the first three existing selections, not a score ranking.
            # WeightSnapshot validates positive maxima for every COST, so a later
            # template cannot replace one of these by having a valid normalization.
            if analysis is not None and len(selections) == limit:
                break
        mode = 'live' if analysis is not None else 'record'

        def build():
            checkpoint(cancel_event)
            observation = (
                None
                if analysis is None
                else dict(
                    cost=analysis.cost,
                    main_rows=[
                        dict(stat_name=s.stat_name, value=s.value) for s in analysis.main_rows
                    ],
                    sub_rows=[
                        dict(stat_name=s.stat_name, value=s.value) for s in analysis.sub_rows
                    ],
                    observed_cost=analysis.observed_cost is not None,
                )
            )
            return dict(
                mode=mode,
                profile_id=profile,
                weight_version=snapshot.version,
                record=record_payload(record) if record is not None else None,
                observation=observation,
                selections=selections,
                limit=limit,
                has_choices=bool(choices),
                missing_choices=missing,
                config_error=config_error,
            )

        job = begin_job('current-comparison', metadata={'weight_version': snapshot.version})
        job.stage('comparing')
        try:
            value = self.compute.submit(
                'current_comparison',
                build,
                timeout=10,
                cancel_event=cancel_event,
            ).result(timeout=12)
            checkpoint(cancel_event)
            if any(
                value.get(k) != v
                for k, v in (
                    ('mode', mode),
                    ('profile_id', profile),
                    ('weight_version', snapshot.version),
                )
            ):
                raise ComputeFailure('request_identity', '当前件比较返回身份不一致')
            raw_rows = value.get('rows')
            if not isinstance(raw_rows, list) or len(raw_rows) > limit:
                raise ComputeFailure('protocol', '当前件比较结果数量错误')
            allowed = {(s['character_id'], s['modal_id']) for s in selections}
            seen, rows = set(), []
            for row in raw_rows:
                checkpoint(cancel_event)
                key = row['character_id'], row['modal_id']
                if (
                    key not in allowed
                    or key in seen
                    or row['state'] not in ('match', 'unknown', 'mismatch')
                ):
                    raise ComputeFailure('request_identity', '当前件比较返回角色身份错误')
                seen.add(key)
                score = score_result(row['score'])
                rows.append(
                    CurrentComparison(
                        *key,
                        row['label'],
                        score.current_score,
                        score.potential_score,
                        row['state'],
                        snapshot.version,
                        rating=score.rating,
                    )
                )
            result = RecommendationResult(tuple(rows), value['message'])
            checkpoint(cancel_event)
        except (CancelledError, OperationCancelled) as error:
            job.finish('cancelled')
            raise OperationCancelled('当前件比较已取消') from error
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise OperationCancelled('当前件比较已取消') from error
            job.finish('failed', error=error)
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            job.finish('failed', error=error)
            raise ComputeFailure('protocol', '当前件比较返回数据格式错误') from error
        except Exception as error:
            job.finish('failed', error=error)
            raise
        job.finish('completed', counters={'roles': len(selections), 'rows': len(rows)})
        return result

    def compare_live(
        self, analysis, raw_preferences, profile, snapshot, record=None, *, cancel_event=None
    ):
        """Preserve matching-page order; uncertain metadata never asserts a match."""
        require_background()
        checkpoint(cancel_event)
        if analysis.current_score is None:
            return ()
        key = (
            self._content(record),
            analysis.cost,
            analysis.observed_cost is not None,
            tuple((s.stat_name, s.value) for s in analysis.main_rows),
            tuple((s.stat_name, s.value) for s in analysis.sub_rows),
            profile,
            raw_preferences,
            snapshot.version,
            ALGORITHM_VERSION,
        )
        if key == self._live_key:
            return self._live_result
        rows = self._evaluate(
            record,
            raw_preferences,
            profile,
            snapshot,
            analysis=analysis,
            cancel_event=cancel_event,
        ).rows
        checkpoint(cancel_event)
        self._live_key, self._live_result = key, rows
        return rows

    @staticmethod
    def _content(record):
        if record is None:
            return None
        return (
            record.name,
            record.sonata,
            record.cost,
            record.level,
            tuple((s.stat_name, s.value) for s in record.main_stats),
            tuple((s.stat_name, s.value) for s in record.sub_stats),
            record.issues,
        )

    def compare(self, record, raw_preferences, snapshot, *, limit=3, cancel_event=None):
        require_background()
        checkpoint(cancel_event)
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError('当前页最多显示三个模版')
        if record is None:
            return RecommendationResult(())
        # Physical content, never UUID/owner/lock or unbounded OCR/screenshots.
        key = (
            self._content(record),
            record.profile_id,
            raw_preferences,
            snapshot.version,
            ALGORITHM_VERSION,
            limit,
        )
        if key == self._key:
            return self._result
        try:
            result = self._evaluate(
                record,
                raw_preferences,
                record.profile_id,
                snapshot,
                limit=limit,
                cancel_event=cancel_event,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            result = RecommendationResult((), '当前页比较配置待修正：' + str(error))
        checkpoint(cancel_event)
        self.calculations += 1
        self._key, self._result = key, result
        return result
