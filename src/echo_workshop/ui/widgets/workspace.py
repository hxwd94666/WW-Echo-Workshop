# 提供共用工作区状态和配装卡片。
"""Small embedded presentation widgets; no persistence, inventory IO or solver."""

import re
from dataclasses import replace
from echo_workshop.ui.widgets.rating_style import score_text
from echo_workshop.ui.widgets.rating import (
    RatedCardDelegate,
    FOOTER_RATING_ROLE,
    apply_rating_colour,
)
from PySide6.QtCore import QModelIndex, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, isDarkTheme, qconfig


class BriefStatus(CaptionLabel):
    """Short normal-state copy, retaining full worker messages as tooltips.

    Unrecognized errors are not shortened. Key stale/limited/unknown facts stay
    visible rather than being hidden by a blanket truncation rule.
    """

    def setText(self, text):
        full = str(text)
        visible = full
        count = re.match(r'仅已收录：(\d+) 件 × (\d+) 模态', full)
        if count:
            visible = f'已匹配 {count[1]} 件 · {count[2]} 个角色模版'
        elif full.startswith('勾选角色 / 固定模态'):
            visible = '选择角色，生成声骸排行。'
        elif full.startswith('选择或条件已变'):
            visible = (
                '选择已更新，请点击开始计算。'
                if '请点击计算' in full
                else '选择已更新，正在准备匹配…'
            )
        elif full.startswith('库存复核完成'):
            visible = '库存已复核，排行可用。'
        elif full.startswith('已请求取消；等待读取/计算'):
            visible = '正在取消匹配…'
        elif full.startswith('添加/编辑角色约束后生成方案'):
            visible = '选角色 → 确认装备要求 → 生成方案'
        elif full.startswith('先加入参与角色；'):
            visible = '先加入角色，再确认装备要求。'
        elif full.startswith('约束已保存'):
            visible = '装备要求已保存，请生成新方案。'
        elif full.startswith('库存/权重/约束已变化：'):
            visible = '数据或装备要求已变化，请重新生成；旧方案保留。'
        elif full.startswith('候选检查完成；尚未搜索'):
            visible = '候选检查完成，尚未生成方案。'
        elif full.startswith('主词条按 +25') or full.startswith('按当前分排序；'):
            visible = '主词条按 +25 评分；未知适配不作推荐。'
        elif full.startswith('请先选择有效角色并刷新比较'):
            visible = '选择角色，生成排行；没有已确认件时先核对。'
        elif full.startswith('先刷新比较，再从已收录声骸选择一件'):
            visible = '生成排行后选择声骸，可逐个加入参与角色。'
        # Normal allocation progress is a short next-action hint. Keep full
        # diagnostics in the tooltip, and never truncate unfamiliar errors.
        normal = {
            '后台搜索中；': '正在生成方案…',
            '后台检查完整候选池；': '正在检查可用声骸…',
            '后台准备角色固定实例；': '正在准备装备要求…',
            '逐个配置带入的角色；': '请逐个确认装备要求。',
            '搜索完成：': '方案已生成，请确认清单。',
            '限时/节点受限：': '搜索已到上限，当前方案未保证最优。',
            '配装已保存并保护，': '已保存并保护，请在游戏中手动装备。',
            '方案/手动进度已提交；': '方案或手动装备标记已保存。',
            '当前配装已保存；': '当前配装已保存；保护不代表已装备。',
        }
        for prefix, message in normal.items():
            if full.startswith(prefix):
                visible = message
                break
        self.full_text = full
        super().setText(visible)
        self.setToolTip(full)
        self.setWordWrap(not bool(count))


class LoadoutCards(QWidget):
    """Fixed-role frozen cards, shared by grouped live and historical results."""

    selected = Signal(str)

    def __init__(self, parent=None, *, grid_type=None, summary_layout=None):
        super().__init__(parent)
        from echo_workshop.ui.inventory.cards import EchoCardGrid

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.result_hint = CaptionLabel(self)
        self.result_hint.setWordWrap(True)
        font = self.result_hint.font()
        font.setBold(True)
        if font.pixelSize() > 0:
            font.setPixelSize(font.pixelSize() + 3)
        else:
            font.setPointSize(max(10, font.pointSize()) + 2)
        self.result_hint.setFont(font)
        if summary_layout is None:
            layout.addWidget(self.result_hint)
        else:
            summary_layout.addWidget(self.result_hint)
        self.issue_hint = CaptionLabel(self)
        self.issue_hint.setWordWrap(True)
        self.issue_hint.hide()
        layout.addWidget(self.issue_hint)
        self.cards = (grid_type or EchoCardGrid)(
            self, show_score_footer=True, show_owner_avatar=False
        )
        self.cards.delegate = RatedCardDelegate(self.cards)
        self.cards.setItemDelegate(self.cards.delegate)
        self.model = QStandardItemModel(self)
        self.cards.setModel(self.model)
        layout.addWidget(self.cards, 1)
        self.rating = None
        self.item_ratings = {}
        self._row = None
        self._rating_snapshot = None
        self._records = {}
        self._completed = frozenset()
        self.cards.selectionModel().currentChanged.connect(self.select_card)
        qconfig.themeChanged.connect(self.update_theme)
        qconfig.themeChangedFinished.connect(self.update_theme)
        self.update_theme()

    def update_theme(self, *_):
        surface = '#202020' if isDarkTheme() else '#f9f9f9'
        palette = self.cards.palette()
        for role in (QPalette.WindowText, QPalette.Text):
            palette.setColor(role, QColor('#f4f4f4' if isDarkTheme() else '#202020'))
        for role in (QPalette.Window, QPalette.Base):
            palette.setColor(role, QColor(surface))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.cards.setPalette(palette)
        self.cards.viewport().setPalette(palette)
        self.cards.viewport().setStyleSheet('background-color: ' + surface + ';')
        self.cards.viewport().update()
        apply_rating_colour(self.result_hint, self.rating)

    def set_role(self, row, records=(), completed=(), *, snapshot=None):
        """Render this row's original cents/slots, without scoring or live lookups."""
        self._row = dict(row) if row is not None else None
        self._rating_snapshot = snapshot
        self._records = {record.item_id: record for record in records}
        self._completed = frozenset(completed)
        previous = self.cards.currentIndex().data(Qt.UserRole)
        selection = self.cards.selectionModel()
        blocked = selection.blockSignals(True)
        try:
            self._render_role(previous)
        finally:
            selection.blockSignals(blocked)
        self.select_card(self.cards.currentIndex(), QModelIndex())

    def _render_role(self, previous):
        from echo_workshop.application.compute_bridge import rating_result
        from echo_workshop.ui.inventory.cards import RECORD_ROLE, FOOTER_ROLE

        self._protection_source = None
        self.model.clear()
        self.cards.setCurrentIndex(QModelIndex())
        self.rating = None
        self.item_ratings = {}
        self.result_hint.setToolTip('')
        self.issue_hint.hide()
        apply_rating_colour(self.result_hint, None)
        row = self._row
        if row is None:
            self.result_hint.clear()
            return
        identities, scores = tuple(row.get('item_ids', ())), tuple(row.get('scores', ()))
        missing = [
            slot for slot, identity in enumerate(identities, 1) if identity not in self._records
        ]
        valid_scores = len(scores) == len(identities) and all(
            type(value) is int and value >= 0 for value in scores
        )
        complete = (
            len(identities) == 5 and len(set(identities)) == 5 and not missing and valid_scores
        )
        self.rating = rating_result(row.get('rating')) if complete else None
        total = row.get('total_cents')
        valid_total = type(total) is int and total >= 0
        label = getattr(self.rating, 'label', '') or (row.get('rating_label') or '')
        text = score_text(total / 100) if valid_total else '总分待核对'
        if label:
            text += f' · {label}'
        if not complete:
            text += f' · {len(identities) - len(missing)}/5 件'
        self.result_hint.setText(text)
        apply_rating_colour(self.result_hint, label or None)
        issues = []
        if missing:
            issues.append('记录缺失：槽' + '、'.join(str(slot) for slot in missing))
        if not valid_scores:
            issues.append('部分冻结评分缺失，暂不评定整套等级')
        if len(set(identities)) != len(identities):
            issues.append('实例重复，暂不评定整套等级')
        self.issue_hint.setText('；'.join(issues))
        self.issue_hint.setVisible(bool(issues))
        item_ratings = tuple(row.get('item_ratings') or ())
        for slot, identity in enumerate(identities, 1):
            record = self._records.get(identity)
            if record is None:
                continue
            value = scores[slot - 1] if slot <= len(scores) else None
            valid_score = type(value) is int and value >= 0
            rating = rating_result(item_ratings[slot - 1]) if slot <= len(item_ratings) else None
            self.item_ratings[identity] = rating
            item = QStandardItem(record.name)
            item.setData(identity, Qt.UserRole)
            item.setData((record, 'item', ()), RECORD_ROLE)
            points = score_text(value / 100, rating) if valid_score else '评分待核对'
            item.setData(f'槽{slot} · {points}', FOOTER_ROLE)
            item.setData(rating, FOOTER_RATING_ROLE)
            self.model.appendRow(item)
            if identity == previous:
                self.cards.selectRow(self.model.rowCount() - 1)

    def set_protection(self, snapshot):
        """Overlay live protection only; frozen records and scores remain unchanged."""
        from echo_workshop.ui.inventory.cards import RECORD_ROLE, RESERVATION_ROLE

        if snapshot is getattr(self, '_protection_source', None):
            return
        self._protection_source = snapshot
        live = {record.item_id: record for record in snapshot.records} if snapshot else {}
        claims = snapshot.owners if snapshot else {}
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            identity = index.data(Qt.UserRole)
            frozen = self._records[identity]
            current = live.get(identity)
            projected = (
                replace(frozen, locked=current.locked)
                if current is not None and current.profile_id == frozen.profile_id
                else frozen
            )
            self.model.setData(index, (projected, 'item', ()), RECORD_ROLE)
            self.model.setData(index, claims.get(identity, ''), RESERVATION_ROLE)

    @Slot(QModelIndex, QModelIndex)
    def select_card(self, index, _previous):
        self.selected.emit(index.data(Qt.UserRole) or '')
