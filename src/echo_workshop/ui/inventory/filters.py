# 绘制库存多选筛选与套装搜索。
"""Library filter controls and a draft-based, non-modal right-side drawer."""

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)
from echo_workshop.resources.search import search_matches
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    FlowLayout,
    LineEdit,
    PushButton,
    PrimaryPushButton,
    isDarkTheme,
    qconfig,
    themeColor,
)
from echo_workshop.core.filters import (
    InventoryFilters,
    MAIN_STAT_KEYS,
    SUB_STAT_KEYS,
    STATUS_OPTIONS,
    STAT_LABELS,
    sonata_options,
)


def filter_count(spec):
    return (
        len(spec.statuses)
        + len(spec.sub_stats)
        + len(spec.selected_costs)
        + len(spec.selected_main_stats)
        + len(spec.selected_sonatas)
        + bool(spec.review_mode)
    )


class SonataCheckDelegate(QStyledItemDelegate):
    """Native single-pass checkbox/text paint; the whole row toggles once."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(38, size.height()))

    def editorEvent(self, event, model, option, index):
        if not index.isValid() or not (
            index.flags() & Qt.ItemIsEnabled and index.flags() & Qt.ItemIsUserCheckable
        ):
            return False
        kind = event.type()
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick, QEvent.MouseButtonRelease):
            if event.button() != Qt.LeftButton or not option.rect.contains(
                event.position().toPoint()
            ):
                return False
            if kind != QEvent.MouseButtonRelease:
                return True
        elif kind == QEvent.KeyPress and event.key() in (Qt.Key_Space, Qt.Key_Select):
            if event.isAutoRepeat():
                return True
        else:
            return False
        state = (
            Qt.Unchecked
            if Qt.CheckState(index.data(Qt.CheckStateRole)) == Qt.Checked
            else Qt.Checked
        )
        return model.setData(index, state, Qt.CheckStateRole)


class CheckableSonataList(QListWidget):
    """Searchable callers keep item text/checkState; native paint has one owner."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(SonataCheckDelegate(self))
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setMouseTracking(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)


class SubstatBox(QFrame):
    """One equal-sized, independently checkable tile per substat."""

    toggled = Signal(bool)

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName('EchoSubstatTile')
        self.setFixedSize(190, 38)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        self.box = CheckBox(text, self)
        layout.addWidget(self.box)
        self.box.toggled.connect(self.toggled.emit)
        self.box.toggled.connect(self.update_theme)
        qconfig.themeChanged.connect(self.update_theme)
        qconfig.themeColorChanged.connect(self.update_theme)
        self.update_theme()

    def isChecked(self):
        return self.box.isChecked()

    def setChecked(self, checked):
        self.box.setChecked(checked)
        self.update_theme()

    def text(self):
        return self.box.text()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setChecked(not self.isChecked())
            event.accept()
            return
        super().mousePressEvent(event)

    def update_theme(self, *_):
        border = (
            themeColor().name() if self.isChecked() else ('#555555' if isDarkTheme() else '#d6d6d6')
        )
        bg = '#303030' if isDarkTheme() else '#ffffff'
        self.setStyleSheet(
            f'#EchoSubstatTile {{border:1px solid {border};border-radius:5px;background:{bg};}}'
        )


class InventoryFilterBar(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._sonatas, self._sonata_catalog = (), ()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(18)

        def section(title, hint=''):
            group = QWidget(self)
            layout = QVBoxLayout(group)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            heading = CaptionLabel(title, group)
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            layout.addWidget(heading)
            if hint:
                heading.setToolTip(hint)
            root.addWidget(group)
            return group, layout

        def choices(group, layout, values):
            options = QWidget(group)
            flow = FlowLayout(options, needAni=False)
            flow.setContentsMargins(0, 0, 0, 0)
            boxes = {}
            for key, text in values:
                control = SubstatBox(text, options)
                boxes[key] = control
                flow.addWidget(control)
                control.toggled.connect(self._changed)
            layout.addWidget(options)
            return boxes

        status, layout = section(
            '状态', '多选显示任一状态；已保护包括手动保护和配装占用，不改游戏锁定。'
        )
        options = QWidget(status)
        flow = FlowLayout(options, needAni=False)
        flow.setContentsMargins(0, 0, 0, 0)
        self.status_boxes = {}
        for key, text in STATUS_OPTIONS:
            control = CheckBox(text, options)
            self.status_boxes[key] = control
            flow.addWidget(control)
            control.toggled.connect(self._changed)
        layout.addWidget(options)
        cost, layout = section('COST（可多选）', '符合任一所选 COST；不选为全部。')
        self.cost_boxes = choices(cost, layout, ((value, f'COST {value}') for value in (1, 3, 4)))
        main, layout = section('主词条（可多选）', '符合任一所选主词条；不选为全部。')
        self.main_boxes = choices(
            main, layout, ((key, STAT_LABELS.get(key, key)) for key in MAIN_STAT_KEYS)
        )
        subs, layout = section('副词条', '所选副词条需全部包含，最多选择 5 个。')
        self.sub_count = CaptionLabel('已选 0/5', subs)
        layout.addWidget(self.sub_count)
        options = QWidget(subs)
        flow = FlowLayout(options, needAni=False)
        flow.setContentsMargins(0, 0, 0, 0)
        self.sub_boxes = {}
        for key in SUB_STAT_KEYS:
            control = SubstatBox(STAT_LABELS.get(key, key), options)
            self.sub_boxes[key] = control
            flow.addWidget(control)
            control.toggled.connect(lambda checked, k=key: self._sub_changed(k, checked))
        layout.addWidget(options)
        sonata, layout = section('套装（可多选）', '符合任一所选套装；搜索只隐藏选项，不取消勾选。')
        self.sonata_search = LineEdit(sonata)
        self.sonata_search.setPlaceholderText('搜索套装（中文 / 拼音 / 首字母）')
        self.sonata_search.setAccessibleName('套装搜索')
        self.sonata_search.textChanged.connect(self._search_sonatas)
        layout.addWidget(self.sonata_search)
        self.sonata_list = CheckableSonataList(sonata)
        self.sonata_list.setAccessibleName('套装多选筛选')
        self.sonata_list.setMinimumHeight(160)
        self.sonata_list.setMaximumHeight(240)
        self.sonata_list.itemChanged.connect(self._sonata_changed)
        layout.addWidget(self.sonata_list)
        self.feedback = CaptionLabel('', self)
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        root.addWidget(self.feedback)
        self.clear_button = PushButton('重置', self)
        self.clear_button.clicked.connect(self.clear)
        root.addWidget(self.clear_button)

    def _changed(self, *_):
        if not self._loading:
            self.changed.emit()

    def _sub_changed(self, key, checked):
        if self._loading:
            return
        count = sum(control.isChecked() for control in self.sub_boxes.values())
        if count > 5:
            control = self.sub_boxes[key]
            blocked = control.blockSignals(True)
            control.setChecked(False)
            control.blockSignals(blocked)
            self.feedback.setText('最多选择 5 个副词条')
            self.feedback.show()
            return
        self.feedback.hide()
        self.sub_count.setText(f'已选 {count}/5')
        self._changed()

    def _sonata_changed(self, *_):
        if self._loading:
            return
        self._sonatas = tuple(
            self.sonata_list.item(i).data(Qt.UserRole)
            for i in range(self.sonata_list.count())
            if self.sonata_list.item(i).checkState() == Qt.Checked
        )
        self._changed()

    def _search_sonatas(self, *_):
        query = self.sonata_search.text()
        for i in range(self.sonata_list.count()):
            item = self.sonata_list.item(i)
            item.setHidden(not search_matches(item.text(), query))

    def _reload_sonatas(self):
        options = tuple(dict.fromkeys((*self._sonata_catalog, *self._sonatas)))
        previous = tuple(
            self.sonata_list.item(i).data(Qt.UserRole) for i in range(self.sonata_list.count())
        )
        blocked = self.sonata_list.blockSignals(True)
        try:
            if previous != options:
                self.sonata_list.clear()
                for name in options:
                    item = QListWidgetItem(name, self.sonata_list)
                    item.setData(Qt.UserRole, name)
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            for i in range(self.sonata_list.count()):
                item = self.sonata_list.item(i)
                item.setCheckState(
                    Qt.Checked if item.data(Qt.UserRole) in self._sonatas else Qt.Unchecked
                )
        finally:
            self.sonata_list.blockSignals(blocked)
        self._search_sonatas()

    def filters(self):
        return InventoryFilters(
            statuses=tuple(key for key, box in self.status_boxes.items() if box.isChecked()),
            costs=tuple(key for key, box in self.cost_boxes.items() if box.isChecked()),
            main_stats=tuple(key for key, box in self.main_boxes.items() if box.isChecked()),
            sub_stats=tuple(key for key, box in self.sub_boxes.items() if box.isChecked()),
            sonatas=self._sonatas,
        )

    def refresh_sonatas(self, snapshot, records):
        self._sonata_catalog = sonata_options(snapshot, records)
        self._reload_sonatas()

    def set_filters(self, spec):
        self._loading = True
        try:
            for key, box in self.status_boxes.items():
                box.setChecked(key in spec.statuses)
            for key, box in self.sub_boxes.items():
                box.setChecked(key in spec.sub_stats)
            for key, box in self.cost_boxes.items():
                box.setChecked(key in spec.selected_costs)
            for key, box in self.main_boxes.items():
                box.setChecked(key in spec.selected_main_stats)
            self._sonatas = spec.selected_sonatas
            self._reload_sonatas()
            self.sub_count.setText(f'已选 {len(spec.sub_stats)}/5')
            self.feedback.hide()
        finally:
            self._loading = False

    def clear(self, *_):
        self.set_filters(InventoryFilters())
        self.changed.emit()


class InventoryFilterDrawer(QFrame):
    """Draft changes are applied only by Confirm; closing leaves the view intact."""

    applied = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('EchoInventoryFilterDrawer')
        self.setFrameShape(QFrame.NoFrame)
        self._applied = InventoryFilters()
        self._open = False
        self._animation = QPropertyAnimation(self, b'geometry', self)
        self._animation.setDuration(170)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.finished.connect(self._animation_finished)
        parent.installEventFilter(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(CaptionLabel('筛选', self))
        self.summary = CaptionLabel('0 项条件', self)
        head.addWidget(self.summary)
        head.addStretch()
        self.close_button = PushButton('×', self)
        self.close_button.setFixedWidth(36)
        self.close_button.setToolTip('关闭，不应用本次修改')
        self.close_button.clicked.connect(self.close_panel)
        head.addWidget(self.close_button)
        root.addLayout(head)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName('EchoInventoryFilterScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.controls = InventoryFilterBar(self.scroll)
        self.controls.setObjectName('EchoInventoryFilterControls')
        self.controls.setAttribute(Qt.WA_StyledBackground, True)
        self.scroll.setWidget(self.controls)
        self.controls.layout().removeWidget(self.controls.clear_button)
        self.controls.layout().addStretch()
        self.controls.changed.connect(self._draft_changed)
        root.addWidget(self.scroll, 1)
        foot = QHBoxLayout()
        foot.addWidget(self.controls.clear_button, 1)
        self.apply_button = PrimaryPushButton('确认', self)
        self.apply_button.clicked.connect(self.apply)
        foot.addWidget(self.apply_button, 1)
        root.addLayout(foot)
        self.escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.escape.setContext(Qt.WidgetWithChildrenShortcut)
        self.escape.activated.connect(self.close_panel)
        qconfig.themeChanged.connect(self.update_theme)
        self.update_theme()
        self.hide()

    def update_theme(self, *_):
        bg, border, fg, field = (
            ('#252525', '#444444', '#f4f4f4', '#333333')
            if isDarkTheme()
            else ('#f9f9f9', '#d8d8d8', '#202020', '#ffffff')
        )
        palette = self.palette()
        for role, color in (
            (QPalette.Window, bg),
            (QPalette.Base, bg),
            (QPalette.Text, fg),
            (QPalette.WindowText, fg),
            (QPalette.Button, field),
            (QPalette.ButtonText, fg),
        ):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.scroll.viewport().setPalette(palette)
        self.controls.setPalette(palette)
        self.setStyleSheet(
            f'#EchoInventoryFilterDrawer {{background:{bg};border-left:1px solid {border};}}'
            f'#EchoInventoryFilterScroll {{background:{bg};border:0;}}'
            f'#EchoInventoryFilterControls {{background:{bg};}}'
        )
        for control in (self.controls.sonata_search, self.controls.sonata_list):
            control.setPalette(palette)
        self.controls.sonata_list.setStyleSheet(
            f'QListWidget {{background:{field};color:{fg};border:1px solid {border};border-radius:5px;}}'
        )

    def filters(self):
        return self._applied

    def _draft_changed(self):
        self.summary.setText(f'{filter_count(self.controls.filters())} 项条件')

    def _rect(self):
        parent = self.parentWidget()
        width = min(440, parent.width())
        return QRect(parent.width() - width, 0, width, parent.height())

    def open_panel(self):
        if self._open:
            return
        self.controls.set_filters(self._applied)
        self._draft_changed()
        self.scroll.verticalScrollBar().setValue(0)
        self._open = True
        self._animation.stop()
        end = self._rect()
        start = QRect(self.parentWidget().width(), 0, end.width(), end.height())
        self.setGeometry(start)
        self.show()
        self.raise_()
        self._animation.setStartValue(start)
        self._animation.setEndValue(end)
        self._animation.start()
        self.close_button.setFocus(Qt.OtherFocusReason)

    def close_panel(self):
        if not self._open:
            return
        self._open = False
        self._animation.stop()
        # Confirmation can immediately rebuild/resize the inventory grid.
        # Closing must not depend on animation ticks while that work is running:
        # otherwise the closed drawer stays visible and intercepts card clicks.
        self.hide()

    def _animation_finished(self):
        if not self._open:
            self.hide()

    def apply(self):
        self._applied = self.controls.filters()
        self.close_panel()
        self.applied.emit()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Hide:
            self._open = False
            self._animation.stop()
            self.hide()
        if watched is self.parentWidget() and event.type() == QEvent.Resize and self._open:
            self._animation.stop()
            self.setGeometry(self._rect())
        return super().eventFilter(watched, event)
