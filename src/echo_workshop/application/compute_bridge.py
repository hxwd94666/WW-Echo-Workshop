# 将冻结业务输入和 Rust 结果在应用模型之间转换，不重复求值。
from collections.abc import Mapping
import sys

from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.rating import LABELS, Rating, _rating_tooltip, rating_result
from echo_workshop.core.scoring import EchoScoreResult


def require_background():
    qt = sys.modules.get('PySide6.QtCore')
    if qt is not None:
        instance = qt.QCoreApplication.instance()
        if instance is not None and qt.QThread.currentThread() == instance.thread():
            raise ComputeFailure('gui_thread', '计算等待应在后台作业中执行')


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


def record_payload(record):
    return {
        'item_id': record.item_id,
        'profile_id': record.profile_id,
        'cost': record.cost,
        'main_stats': [{'stat_name': s.stat_name, 'value': s.value} for s in record.main_stats],
        'sub_stats': [{'stat_name': s.stat_name, 'value': s.value} for s in record.sub_stats],
        'name': record.name,
        'sonata': record.sonata,
        'level': record.level,
        'review': record.review,
        'issues': list(record.issues),
        'locked': record.locked,
        'owner': record.owner,
        'owner_state': record.owner_state,
        'game_equipped': record.game_equipped,
    }


def _rating_fields(standard):
    thresholds = tuple(standard['thresholds'])
    cost = standard['cost']
    return (
        thresholds,
        standard['scale'],
        cost,
        standard['standard_id'],
        standard['source'],
        _rating_tooltip(cost, thresholds),
    )


def prepare_rating_fields(standards):
    """Decode the per-batch standard table once instead of once per cell."""
    return tuple(_rating_fields(row) if isinstance(row, dict) else None for row in standards)


def compact_rating(standard, label, score, fields=None):
    """Build a frozen rating from a shared standard entry without copying a dict."""
    if not isinstance(standard, dict) or not isinstance(label, str):
        return None
    thresholds, scale, cost, standard_id, source, tooltip = (
        fields if fields is not None else _rating_fields(standard)
    )
    return Rating(label, score, scale, thresholds, cost, standard_id, source, tooltip)


def compact_score(data, standards, prepared=None):
    """Decode a columnar cell score: ``[cost, key, rows, current, potential, rating]``."""
    if data is None:
        return None
    if not isinstance(data, list) or len(data) != 6:
        raise ValueError('匹配分数字段错误')
    cost, cost_key, rows, current, potential, rating_entry = data
    rating = None
    if isinstance(rating_entry, list) and len(rating_entry) == 3:
        label_index, score_value, standard_index = rating_entry
        standard = (
            standards[standard_index]
            if type(standard_index) is int and 0 <= standard_index < len(standards)
            else None
        )
        label = (
            LABELS[label_index]
            if type(label_index) is int and 0 <= label_index < len(LABELS)
            else ''
        )
        fields = (
            prepared[standard_index]
            if prepared is not None
            and type(standard_index) is int
            and 0 <= standard_index < len(prepared)
            else None
        )
        rating = compact_rating(standard, label, score_value, fields)
    return EchoScoreResult(cost, cost_key, tuple(rows or ()), current, potential, rating)


def score_result(data):
    if data is None:
        return None
    return EchoScoreResult(
        data['cost'],
        data['cost_key'],
        tuple(data.get('row_scores') or ()),
        data['current_score'],
        data['potential_score'],
        rating_result(data['rating']),
    )
