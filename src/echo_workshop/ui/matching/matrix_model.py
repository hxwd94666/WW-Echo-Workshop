# 提供评分矩阵的有界数据投影。
"""Lazy read-only Qt matrix: row-major results, bounded text cache, UUID-safe sorting."""

from collections import OrderedDict

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from echo_workshop.ui.widgets.rating_style import rating_colour
from echo_workshop.application.match_projection import MatchProjections

LABELS = {
    'unknown': '未知',
    'match': '匹配',
    'mismatch': '不匹配',
    'element_match': '匹配',
    'element_mismatch': '不匹配',
}


class MatrixModel(QAbstractTableModel):
    TEXT_CACHE_LIMIT = 2048

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records, self.selections, self.scores, self.labels = (), (), (), ()
        self.claims = {}
        self.projections = None
        self._order = []
        self._rows_by_id, self._columns_by_key = {}, {}
        self._text_cache = OrderedDict()
        self.compact_labels = False
        self.publication_revision = 0

    def reset_matrix(self, records=(), selections=(), scores=(), labels=(), *, projections=None):
        if len(scores) != len(records) * len(selections) or len(labels) != len(selections):
            raise ValueError('Incomplete matrix payload')
        if (records or scores) and (
            not isinstance(projections, MatchProjections)
            or not projections.matches(records, selections, scores)
        ):
            raise ValueError('匹配矩阵缺少同一冻结输入的原生投影')
        self.beginResetModel()
        self.publication_revision += 1
        self.records, self.selections, self.scores, self.labels = (
            records,
            selections,
            scores,
            labels,
        )
        self.projections = projections
        self._order = list(range(len(records)))
        self._text_cache.clear()
        self._index_identities()
        self.endResetModel()

    def _index_identities(self):
        self._rows_by_id = {
            self.records[source].item_id: row for row, source in enumerate(self._order)
        }
        self._columns_by_key = {tuple(key): i + 1 for i, key in enumerate(self.selections)}

    def index_for_identity(self, item_id, character_id=None, modal_id=None):
        row = self._rows_by_id.get(item_id)
        column = (
            0
            if character_id is None and modal_id is None
            else self._columns_by_key.get((character_id, modal_id))
        )
        return self.index(row, column) if row is not None and column is not None else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 1 + len(self.selections)

    def entry(self, index):
        if (
            not index.isValid()
            or not 0 <= index.row() < self.rowCount()
            or not 0 <= index.column() < self.columnCount()
        ):
            return None, None
        source = self._order[index.row()]
        score = (
            self.scores[source * len(self.selections) + index.column() - 1]
            if index.column()
            else None
        )
        return self.records[source], score

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return section + 1
        if 0 <= section < self.columnCount():
            if self.compact_labels:
                return '声骸 / 词条' if section == 0 else self.labels[section - 1].split('\n', 1)[0]
            return '具体实例 / 词条' if section == 0 else self.labels[section - 1]
        return None

    def data(self, index, role=Qt.DisplayRole):
        record, score = self.entry(index)
        if record is None:
            return None
        if role == Qt.UserRole:
            return (
                (record.item_id, score.character_id, score.modal_id)
                if score
                else (record.item_id, None, None)
            )
        if role == Qt.UserRole + 1:
            return score.score.current_score if score and score.score else float('-inf')
        if role == Qt.ForegroundRole and score:
            from qfluentwidgets import isDarkTheme

            dark = isDarkTheme()
            rating = getattr(score.score, 'rating', None)
            if rating is not None:
                return QColor(rating_colour(rating, dark))
            if score.recommendation in ('mismatch', 'excluded'):
                return QColor('#f18a8a' if dark else '#bb3030')
            if score.recommendation == 'unknown':
                return QColor('#eab86a' if dark else '#8a6200')
        if role == Qt.ToolTipRole:
            if score:
                text = (
                    ', '.join(score.issues)
                    or f'显式元素：{score.explicit_rule.element or "未知"}\n候选套装：{", ".join(score.explicit_rule.sonatas) or "未知"}'
                )
                rating = getattr(score.score, 'rating', None)
                return text
            return record.item_id + (
                '\n' + self.claims[record.item_id] if record.item_id in self.claims else ''
            )
        if role != Qt.DisplayRole:
            return None
        # Format only requested cells, never allocate widgets/strings for the whole Cartesian product.
        key = (self._order[index.row()], index.column())
        text = self._text_cache.get(key)
        if text is None:
            if score is None:
                identity = '' if self.compact_labels else f' · {record.item_id[:8]}'
                text = f'{record.name}{identity}\n{record.sonata} | COST {record.cost} | +{record.level}\n'
                if self.compact_labels:
                    from echo_workshop.ui.inventory.card_data import DAMAGE_LABELS

                    text += ' / '.join(
                        f'{DAMAGE_LABELS.get(stat.stat_name, stat.stat_name)} {stat.value:g}'
                        for stat in record.sub_stats
                    )
                else:
                    text += ' / '.join(
                        f'{stat.stat_name} {stat.value:g}' for stat in record.sub_stats
                    )
                if record.item_id in self.claims:
                    text += '\n' + self.claims[record.item_id]
            else:
                rating = getattr(score.score, 'rating', None)
                grade = f' · {rating.label}' if rating is not None else ''
                text = (
                    '隔离 / 不评分'
                    if score.score is None
                    else f'{score.score.current_score:.2f}{grade}'
                )
                text += f'\n元素{LABELS[score.compatibility]} / 套装{LABELS[score.sonata_compatibility]}'
            self._text_cache[key] = text
            if len(self._text_cache) > self.TEXT_CACHE_LIMIT:
                self._text_cache.popitem(last=False)
        else:
            self._text_cache.move_to_end(key)
        return text
