# 投影库存记录与稳定选择键，未变发布不重置视图。
"""Lazy cells, row-local invalidation, and stable UUID lookup."""

from collections import OrderedDict
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from echo_workshop.application.view_state import review_checked_reason
from echo_workshop.application.record_quality import quality_for
from echo_workshop.core.filters import is_protected
from echo_workshop.ui.inventory.card_data import DAMAGE_LABELS, checked_protection_text


class InventoryModel(QAbstractTableModel):
    TEXT_CACHE_LIMIT = 2048
    HEADERS = ('状态', '名称 / 套装', 'COST / 等级', '主词条', '副词条', '工坊装备者')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = ()
        self.claims = {}
        self._quality = {}
        self._ids = {}
        self._text_cache = OrderedDict()
        self._search_cache = {}
        self._sort_cache = {}
        self.revision = 0

    def reset_rows(self, rows=(), *, claims=(), quality=()):
        rows, claims = tuple(rows), dict(claims)
        quality = tuple(quality)
        if len(quality) != len(rows):
            raise ValueError('库存展示需要完整的原生质量结论。')
        facts = {
            record.item_id: quality_for(record, fact) for (record, _, _), fact in zip(rows, quality)
        }
        if len(facts) != len(rows):
            raise ValueError('库存展示 UUID 重复。')
        if rows == self.rows and claims == self.claims and facts == self._quality:
            return False
        same_ids = len(rows) == len(self.rows) and all(
            a[0].item_id == b[0].item_id for a, b in zip(rows, self.rows)
        )
        if same_ids:
            changed = [
                i
                for i, (old, new) in enumerate(zip(self.rows, rows))
                if old != new
                or self.claims.get(old[0].item_id) != claims.get(new[0].item_id)
                or self._quality.get(old[0].item_id) != facts.get(new[0].item_id)
            ]
            self.rows, self.claims = rows, claims
            self._quality = facts
            self.revision += 1
            for row in changed:
                self._search_cache.pop(row, None)
                self._sort_cache.pop(row, None)
                for col in range(6):
                    self._text_cache.pop((row, col), None)
            for row in changed:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 5), [])
        else:
            self.beginResetModel()
            self.rows, self.claims = rows, claims
            self._quality = facts
            self._ids = {r.item_id: i for i, (r, _, _) in enumerate(rows)}
            self._text_cache.clear()
            self._search_cache.clear()
            self._sort_cache.clear()
            self.revision += 1
            self.endResetModel()
        return True

    def row_for_id(self, item_id):
        return self._ids.get(item_id, -1)

    def quality(self, record):
        return quality_for(record, self._quality.get(record.item_id))

    def filter_record(self, row):
        return self.rows[row][:2]

    def search_text(self, row):
        if row not in self._search_cache:
            record = self.rows[row][0]
            stats = record.main_stats + record.sub_stats
            self._search_cache[row] = ' '.join(
                (
                    record.name,
                    record.sonata,
                    record.owner,
                    record.note,
                    *(DAMAGE_LABELS.get(s.stat_name, s.stat_name) for s in stats),
                    *(s.stat_name for s in stats),
                )
            )
        return self._search_cache[row]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 6

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Vertical:
                return section + 1
            if 0 <= section < 6:
                return self.HEADERS[section]

    def data(self, index, role=Qt.DisplayRole):
        if (
            not index.isValid()
            or not 0 <= index.row() < len(self.rows)
            or not 0 <= index.column() < 6
        ):
            return None
        row = index.row()
        record, kind, _ = self.rows[row]
        if role == Qt.UserRole:
            return record.item_id
        if role == int(Qt.UserRole) + 1:
            return self.rows[row]
        if role == int(Qt.UserRole) + 2:
            return self.search_text(row)
        if role == int(Qt.UserRole) + 4:
            return checked_protection_text(
                record, self.quality(record), self.claims.get(record.item_id, '')
            )
        if role == int(Qt.UserRole) + 7:
            return self.quality(record)
        if role == int(Qt.UserRole) + 6:
            return self.claims.get(record.item_id, '')
        if role == int(Qt.UserRole) + 3:
            if row not in self._sort_cache:
                self._sort_cache[row] = record.name.casefold()
            return self._sort_cache[row]
        if role == Qt.ToolTipRole:
            return record.item_id + '\n' + str(self.data(index, Qt.DisplayRole))
        if role != Qt.DisplayRole:
            return None
        key = row, index.column()
        if key in self._text_cache:
            self._text_cache.move_to_end(key)
            return self._text_cache[key]
        col = index.column()
        if col == 0:
            from echo_workshop.application.inventory import game_status_labels

            text = review_checked_reason(record, self.quality(record), kind)
            if is_protected(record, self.claims.get(record.item_id)):
                text += ' / 已保护'
            text += ''.join(' / ' + label for label in game_status_labels(record))
        elif col == 1:
            text = f'{record.name or "名称待核对"}\n{record.sonata or "套装待核对"}'
        elif col == 2:
            text = f'{record.cost} / +{record.level if record.level is not None else "?"}'
        elif col in (3, 4):
            text = '\n'.join(
                f'{DAMAGE_LABELS.get(s.stat_name, s.stat_name)} {s.value:g}'
                for s in (record.main_stats if col == 3 else record.sub_stats)
            )
        else:
            text = (record.owner or '未分配') + (
                '\n' + self.claims[record.item_id] if record.item_id in self.claims else ''
            )
        self._text_cache[key] = text
        if len(self._text_cache) > self.TEXT_CACHE_LIMIT:
            self._text_cache.popitem(last=False)
        return text
