# 将原生质量结论绑定到原始冻结记录，展示层不重新求值。
from dataclasses import dataclass

from echo_workshop.application.compute_protocol import ComputeFailure


@dataclass(frozen=True)
class RecordQuality:
    record: object
    issues: tuple[str, ...]
    usable: bool

    @classmethod
    def from_response(cls, record, row):
        if not isinstance(row, dict) or row.get('item_id') != record.item_id:
            raise ComputeFailure('request_identity', '库存质量返回 UUID 错误')
        if (
            set(row) != {'item_id', 'issues', 'usable'}
            or not isinstance(row['issues'], list)
            or any(not isinstance(issue, str) for issue in row['issues'])
            or type(row['usable']) is not bool
        ):
            raise ComputeFailure('protocol', '库存质量返回结构错误')
        return cls(record, tuple(row['issues']), row['usable'])


def quality_for(record, quality):
    """Require the exact immutable observation, not merely the same UUID."""
    if not isinstance(quality, RecordQuality) or quality.record is not record:
        raise ValueError('库存质量结论与当前冻结记录不一致，请重新读取。')
    return quality
