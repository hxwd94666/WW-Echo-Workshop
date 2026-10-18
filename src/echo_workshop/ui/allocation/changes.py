# 紧凑展示当前角色的冻结配装变动，不查询库存或重新评分。
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, SubtitleLabel, TitleLabel, isDarkTheme, qconfig

from echo_workshop.ui.widgets.layout import fit_dialog
from echo_workshop.ui.allocation.changes_cards import (
    ChangePairsGrid,
    points,
    set_delta,
)
from echo_workshop.ui.widgets.rating import apply_rating_colour, lookup_avatar


class _RoleChanges(QFrame):
    def __init__(self, change, parent, avatar_cache):
        super().__init__(parent)
        self.change = change
        self.setObjectName('loadoutChangeGroup')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(7)
        header = QHBoxLayout()
        self.avatar = QLabel(self)
        self.avatar.setFixedSize(30, 30)
        self.avatar.setAlignment(Qt.AlignCenter)
        pixmap = lookup_avatar(avatar_cache, change.key[0])
        if pixmap is not None:
            self.avatar.setPixmap(
                pixmap.scaled(30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self.avatar.setText(change.label[:1])
        header.addWidget(self.avatar)
        self.heading = SubtitleLabel(change.label, self)
        self.heading.setWordWrap(True)
        header.addWidget(self.heading, 1)
        layout.addLayout(header)
        totals = QHBoxLayout()
        totals.setSpacing(12)
        self.old_total = TitleLabel(points(change.old_total_cents, change.old_rating), self)
        self.new_total = TitleLabel(points(change.new_total_cents, change.new_rating), self)
        for title, score in (('原总分', self.old_total), ('新总分', self.new_total)):
            column = QVBoxLayout()
            column.setSpacing(1)
            column.addWidget(CaptionLabel(title, self))
            column.addWidget(score)
            totals.addLayout(column)
            if score is self.old_total:
                totals.addWidget(SubtitleLabel('→', self))
        totals.addStretch(1)
        self.delta = SubtitleLabel(self)
        totals.addWidget(self.delta)
        layout.addLayout(totals)
        if change.notice:
            notice = CaptionLabel(change.notice, self)
            notice.setWordWrap(True)
            layout.addWidget(notice)
        kept = [pair.old for pair in change.pairs if pair.kind == 'retained']
        if kept:
            names = '、'.join(item.record.name if item.record else '缺失记录' for item in kept)
            self.retained = CaptionLabel(f'保留 {len(kept)} 件：' + names, self)
            self.retained.setWordWrap(True)
            layout.addWidget(self.retained)
        pairs = [pair for pair in change.pairs if pair.kind != 'retained']
        self.pairs_grid = ChangePairsGrid(pairs, self)
        self.columns = [(panel.old, panel.new) for panel in self.pairs_grid.panels]
        if pairs:
            layout.addWidget(self.pairs_grid)
        else:
            layout.addWidget(CaptionLabel('声骸均保留，无需换装。', self))
            self.pairs_grid.hide()
        self.update_colours()

    def update_colours(self):
        old, new = self.change.old_total_cents, self.change.new_total_cents
        apply_rating_colour(self.old_total, self.change.old_rating if old is not None else None)
        apply_rating_colour(self.new_total, self.change.new_rating if new is not None else None)
        delta = self.change.delta_cents if old is not None and new is not None else None
        set_delta(self.delta, delta, prefix='总分 ')
        for panel in self.pairs_grid.panels:
            panel.update_colours()


class LoadoutChangesDialog(QDialog):
    """Read-only current-role DTO; one outer scroll, no scorer or storage calls."""

    def __init__(self, changes, parent=None, *, avatar_cache=None):
        super().__init__(parent)
        self.changes = tuple(changes)
        if len(self.changes) > 1:
            raise ValueError('请选择一个角色查看配装变动。')
        self.groups = {}
        self.setObjectName('EchoLoadoutChanges')
        self.setWindowTitle('配装变动')
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumSize(600, 420)
        self.resize(1100, 790)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)
        layout.addWidget(SubtitleLabel('配装变动', self))
        note = CaptionLabel('同一份评分资料，仅对比，不会保存或更改装备者。', self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setObjectName('loadoutChangesViewport')
        self.body = QWidget(self.scroll)
        self.body.setObjectName('loadoutChangesBody')
        self.body.setMinimumWidth(0)
        self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        rows = QVBoxLayout(self.body)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        if not self.changes:
            rows.addWidget(CaptionLabel('尚无本次计算的配装变动。', self.body))
        for change in self.changes:
            group = _RoleChanges(change, self.body, avatar_cache)
            self.groups[change.key] = group
            rows.addWidget(group)
        rows.addStretch(1)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        pairing = CaptionLabel('同 COST 并排对比，不代表固定槽位。', self)
        pairing.setWordWrap(True)
        layout.addWidget(pairing)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.buttons.button(QDialogButtonBox.Close).setText('关闭')
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        qconfig.themeChanged.connect(self._update_theme)
        qconfig.themeChangedFinished.connect(self._update_theme)
        self._update_theme()
        fit_dialog(self, 1100, 790)

    def _update_theme(self, *_):
        dark = isDarkTheme()
        surface = QColor('#202020' if dark else '#fafafa')
        foreground = QColor('#f4f4f4' if dark else '#202020')
        palette = self.palette()
        for role in (QPalette.Window, QPalette.Base):
            palette.setColor(role, surface)
        for role in (QPalette.WindowText, QPalette.Text):
            palette.setColor(role, foreground)
        for widget in (
            self,
            self.scroll,
            self.scroll.viewport(),
            self.body,
            *self.body.findChildren(QWidget),
        ):
            widget.setPalette(palette)
        for widget in (self, self.scroll, self.scroll.viewport(), self.body, *self.groups.values()):
            widget.setAutoFillBackground(True)
        self.setStyleSheet(
            '#EchoLoadoutChanges, #loadoutChangesBody, #loadoutChangesViewport {background:'
            + surface.name()
            + ';}'
            '#loadoutChangeGroup {background:' + surface.name() + ';border:none;}'
            '#changePairPanel {border:1px solid '
            + ('#414141' if dark else '#dedede')
            + ';border-radius:7px;}'
            '#compactChangeCard {background:'
            + ('#292929' if dark else '#ffffff')
            + ';border:none;border-radius:5px;}'
        )
        for group in self.groups.values():
            group.avatar.setStyleSheet('color:' + foreground.name() + ';background:transparent;')
            group.update_colours()
