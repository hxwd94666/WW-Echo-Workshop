# 为完整冻结匹配矩阵批量取得原生展示索引，Python 只校验身份和结构。
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from uuid import uuid4

from echo_workshop.application.compute_bridge import require_background
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled, checkpoint


@dataclass(frozen=True)
class MatchView:
    entries: tuple
    excluded: int
    unknown: int


@dataclass(frozen=True)
class MatchProjections:
    identity: str
    records: tuple = field(repr=False)
    selections: tuple
    scores: tuple = field(repr=False)
    flags: tuple
    views: tuple

    def matches(self, records, selections, scores):
        return (
            self.records is records
            and self.scores is scores
            and self.selections == tuple(selections)
        )


def _integer(value, limit):
    return type(value) is int and 0 <= value < limit


class MatchProjectionService:
    def __init__(self, *, compute):
        self.compute = compute

    def project(self, records, selections, scores, *, cancel_event=None):
        require_background()
        records, scores = tuple(records), tuple(scores)
        selections = tuple(tuple(key) for key in selections)
        width = len(selections)
        if len(records) > 20000 or width > 128 or len(scores) > 160000:
            raise ComputeFailure('input_limit', '匹配展示矩阵超过限额')
        if not width or len(scores) != len(records) * width:
            raise ValueError('匹配展示矩阵尺寸错误')
        identity = uuid4().hex
        job = begin_job('match-projection')
        try:
            job.stage('projecting')

            def payload():
                cells = []
                for row, record in enumerate(records):
                    checkpoint(cancel_event)
                    for column, key in enumerate(selections):
                        value = scores[row * width + column]
                        if (value.item_id, value.character_id, value.modal_id) != (
                            record.item_id,
                            *key,
                        ):
                            raise ComputeFailure('request_identity', '匹配投影输入身份错误')
                        cells.append(
                            [
                                value.score.current_score if value.score is not None else None,
                                value.exclusion,
                                value.recommendation,
                            ]
                        )
                return dict(
                    identity=identity,
                    ids=[row.item_id for row in records],
                    roles=selections,
                    cells=cells,
                )

            data = self.compute.submit(
                'match_projection', payload, timeout=15, cancel_event=cancel_event
            ).result(timeout=17)
            checkpoint(cancel_event)
            job.stage('decode')
            result = self._decode(data, identity, records, selections, scores)
        except (OperationCancelled, CancelledError):
            job.finish('cancelled')
            raise OperationCancelled('匹配展示已取消') from None
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise OperationCancelled('匹配展示已取消') from error
            job.finish('failed', error=error)
            raise
        except Exception as error:
            job.finish('failed', error=error)
            raise
        else:
            job.finish(counters=dict(records=len(records), roles=width))
            return result

    @staticmethod
    def _decode(data, identity, records, selections, scores):
        width, count = len(selections), len(records)
        if not isinstance(data, dict) or data.get('identity') != identity:
            raise ComputeFailure('request_identity', '匹配投影结果身份错误')
        flags, raw_views = data.get('flags'), data.get('views')
        if (
            not isinstance(flags, list)
            or len(flags) != len(scores)
            or any(not _integer(flag, 5) for flag in flags)
            or not isinstance(raw_views, list)
            or len(raw_views) != width + 1
        ):
            raise ComputeFailure('protocol', '匹配投影尺寸或状态错误')
        views = []
        for view_index, view in enumerate(raw_views):
            role = None if view_index == 0 else view_index - 1
            if not isinstance(view, dict) or view.get('role') != role:
                raise ComputeFailure('request_identity', '匹配投影角色身份错误')
            entries = view.get('entries')
            excluded, unknown = view.get('excluded'), view.get('unknown')
            if (
                not isinstance(entries, list)
                or len(entries) > count
                or not _integer(excluded, count + 1)
                or not _integer(unknown, len(entries) + 1)
                or excluded + len(entries) != count
            ):
                raise ComputeFailure('protocol', '匹配投影行数错误')
            seen, parsed = set(), []
            for entry in entries:
                if (
                    not isinstance(entry, list)
                    or len(entry) != 2
                    or not _integer(entry[0], count)
                    or not _integer(entry[1], width)
                    or entry[0] in seen
                    or (role is not None and entry[1] != role)
                ):
                    raise ComputeFailure('protocol', '匹配投影行映射错误')
                seen.add(entry[0])
                parsed.append(tuple(entry))
            views.append(MatchView(tuple(parsed), excluded, unknown))
        return MatchProjections(identity, records, selections, scores, tuple(flags), tuple(views))
