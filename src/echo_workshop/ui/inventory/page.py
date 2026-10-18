# 提供声骸库卡片筛选多选及内嵌核对页面。
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QMenu,
    QMessageBox,
)
from qfluentwidgets import CaptionLabel, PushButton, LineEdit
from echo_workshop.ui.widgets.inputs import ComboBox
from echo_workshop.ui.inventory.controller import InventoryContent
from echo_workshop.ui.widgets.layout import Disclosure
from echo_workshop.ui.inventory.cards import EchoCardGrid, EchoCardWidget
from echo_workshop.ui.inventory.review import InventoryReviewController
from echo_workshop.ui.inventory.intake_feedback import InventoryIntakeFeedback


def put(layout, widget, stretch=0):
    layout.addWidget(widget, stretch)
    return widget


class InventoryPage(InventoryIntakeFeedback, InventoryReviewController, InventoryContent, QWidget):
    _intake_ui = True
    _table_type = EchoCardGrid
    _combo_type = ComboBox
    _button_type = PushButton

    def __init__(self, task, parent=None):
        self._editor = None
        self._match_selection_restoring = False
        self._embedded_ready = False
        super().__init__(task, parent)
        self._accepted_profile = self.profile_box.currentData()
        self._embedded_ready = True
        self.show_details()
        self.refresh()

    def build_inventory_ui(self):
        from PySide6.QtWidgets import QTextEdit, QAbstractItemView
        from echo_workshop.ui.inventory.model import InventoryModel
        from echo_workshop.ui.widgets.state import ActionState, ChoiceState, TextState
        from echo_workshop.capture.scan import SCAN_LEVELS

        self.setWindowTitle('声骸库')
        self.setMinimumSize(0, 0)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        self.profile_box = ComboBox(self)
        self.filter_box = ChoiceState(self)
        self.filter_box.addItems(['全部', '待核对', '已确认'])
        self.profile_create = ActionState('新建档案')
        self.start_button = PushButton('开始收录', self)
        self.pause_button = PushButton('暂停 / 继续', self)
        self.stop_button = PushButton('结束收录', self)
        for button, fn in (
            (self.start_button, self.start_capture),
            (self.pause_button, self.pause_capture),
            (self.stop_button, self.finish_capture),
        ):
            button.clicked.connect(lambda checked=False, action=fn: self.action(action))
        self.intake_mode = ComboBox(self)
        self.intake_mode.addItem('手动收录', userData='manual')
        self.intake_mode.addItem('自动扫描', userData='automatic')
        self.scan_level = ComboBox(self)
        for level in SCAN_LEVELS:
            self.scan_level.addItem(f'+{level}', userData=str(level))
        self.intake_mode.currentIndexChanged.connect(self._intake_mode_changed)
        self.scan_level.currentIndexChanged.connect(self._scan_level_changed)
        self.capture_help = TextState()
        self.backup_label = TextState()
        self.status_label = TextState()
        self.reading_label = TextState()
        self.cancel_backup_button = PushButton('取消数据操作', self)
        self.cancel_backup_button.clicked.connect(self.runtime.backups.cancel)
        self.cancel_read_button = ActionState()
        self.clear_library_button = PushButton('清空声骸库', self)
        self.clear_library_button.clicked.connect(lambda: self.action(self.clear_library))
        self.evidence_button = PushButton('查看原图', self)
        self.evidence_button.clicked.connect(lambda: self.action(self.open_selected_evidence))
        self.table = EchoCardGrid(
            self, show_score_footer=True, queries=self.runtime.inventory_queries
        )
        # 原生筛选是后台作业：投影变化后刷新选择栏，不假设同步可见集合。
        self.table.proxy.stateChanged.connect(self._on_projection_changed)
        self.table.projectionChanged.connect(self._restore_match_selection)
        self.inventory_model = InventoryModel(self.table)
        self.table.setModel(self.inventory_model)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.details = QTextEdit(self)
        self.details.setReadOnly(True)
        self.confirm_button = PushButton('这是另一件', self)
        self.upgrade_button = PushButton('更新旧件', self)
        self.ignore_button = PushButton('忽略本次观察', self)
        for button, callback in (
            (self.confirm_button, self.confirm_selected),
            (self.upgrade_button, self.upgrade_selected),
            (self.ignore_button, self.discard_selected),
        ):
            button.clicked.connect(lambda checked=False, fn=callback: self.action(fn))
        self._action_buttons = [
            self.clear_library_button,
            self.confirm_button,
            self.upgrade_button,
            self.ignore_button,
        ]
        self._mutation_buttons = list(self._action_buttons)
        self.inventory_body = QWidget()
        outer = QVBoxLayout(self.inventory_body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        self.match_selection_bar = QWidget(self.inventory_body)
        select_row = QHBoxLayout(self.match_selection_bar)
        select_row.setContentsMargins(0, 0, 0, 0)
        self.match_selection_note = CaptionLabel('点击声骸可多选')
        self.match_selection_note.setWordWrap(True)
        select_row.addWidget(self.match_selection_note, 1)
        self.match_select_all_button = PushButton('全选')
        self.match_select_all_button.clicked.connect(
            lambda: self.action(self._select_all_match_items)
        )
        self.match_apply_selected = PushButton('确定选择')
        self.match_apply_filtered = PushButton('使用筛选结果')
        self.match_cancel = PushButton('取消选择')
        for button in (
            self.match_select_all_button,
            self.match_apply_filtered,
            self.match_cancel,
            self.match_apply_selected,
        ):
            select_row.addWidget(button)
        self.match_apply_selected.clicked.connect(
            lambda: self.action(lambda: self.task._panel.finish_inventory_selection())
        )
        self.match_apply_filtered.clicked.connect(
            lambda: self.action(lambda: self.task._panel.finish_inventory_selection(filtered=True))
        )
        self.match_cancel.clicked.connect(
            lambda: self.action(self.task._panel.cancel_inventory_selection)
        )
        outer.addWidget(self.match_selection_bar)
        self.match_selection_bar.hide()
        root.addWidget(self.inventory_body, 1)
        self.editor_area = QScrollArea()
        self.editor_area.setWidgetResizable(True)
        root.addWidget(self.editor_area, 1)
        self.editor_area.hide()
        row = QHBoxLayout()
        self.search_box = LineEdit()
        self.search_box.setPlaceholderText('搜索名称、套装、词条')
        self.search_box.setClearButtonEnabled(True)
        put(row, self.search_box, 1)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(150)
        self.search_box.textChanged.connect(lambda: self.search_timer.start())
        self.search_timer.timeout.connect(self._apply_search)
        self.filter_button = PushButton('筛选', self.inventory_body)
        self.filter_button.setAccessibleName('声骸库筛选')
        put(row, self.filter_button)
        put(row, self.clear_library_button)
        outer.addLayout(row)
        from echo_workshop.ui.inventory.filters import InventoryFilterDrawer

        self.filter_drawer = InventoryFilterDrawer(self.inventory_body)
        self.library_filters = self.filter_drawer.controls
        self.filter_button.clicked.connect(self.filter_drawer.open_panel)
        self.filter_drawer.applied.connect(self._apply_filters)
        self.compact_status = CaptionLabel('正在读取声骸…')
        status_row = QHBoxLayout()
        status_row.setSpacing(14)
        status_row.addWidget(self.compact_status)
        self.operation_hint = CaptionLabel('右键声骸可进行操作，支持多选批量操作')
        hint_font = self.operation_hint.font()
        hint_font.setPointSizeF(8)
        self.operation_hint.setFont(hint_font)
        self.operation_hint.setStyleSheet('color: #888888;')
        self.operation_hint.setWordWrap(True)
        status_row.addWidget(self.operation_hint, 1)
        outer.addLayout(status_row)
        self.content_splitter = QSplitter()
        self.table.setMinimumSize(0, 0)
        self.content_splitter.addWidget(self.table)
        self.table.show()
        detail = QWidget()
        body = QVBoxLayout(detail)
        body.setContentsMargins(10, 0, 0, 0)
        body.setSpacing(12)
        self.back_to_grid = PushButton('返回声骸库')
        self.back_to_grid.clicked.connect(self._return_grid)
        body.addWidget(self.back_to_grid)
        self.image = EchoCardWidget()
        put(body, self.image)
        put(body, self.evidence_button)
        self.evidence_button.setText('查看原图')
        diagnostic = Disclosure('识别详情')
        put(diagnostic.content_layout, self.details)
        body.addWidget(diagnostic)
        body.addStretch()
        self.detail_area = QScrollArea()
        self.detail_area.setWidgetResizable(True)
        self.detail_area.setWidget(detail)
        self.detail_area.setMinimumWidth(220)
        self._detail_requested = False
        self.content_splitter.addWidget(self.detail_area)
        self.detail_area.hide()
        from PySide6.QtWidgets import QAbstractItemView

        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_record_menu)
        self.table.selectionModel().selectionChanged.connect(self._sync_match_selection)
        self.table.lock_edit_enabled = True
        self.table.lockRequested.connect(
            lambda identity: self.action(lambda: self._toggle_record_lock(identity))
        )
        self.content_splitter.setSizes([620, 340])
        outer.addWidget(self.content_splitter, 1)
        actions = QHBoxLayout()
        for button in (self.confirm_button, self.upgrade_button, self.ignore_button):
            put(actions, button)
        actions.addStretch()
        outer.addLayout(actions)

    def profile_changed(self):
        if not getattr(self, '_embedded_ready', False):
            return super().profile_changed()
        target = self.profile_box.currentData()
        reason = self.task._panel.profile_switch_error(target)
        if reason:
            previous = self.profile_box.blockSignals(True)
            self.profile_box.setCurrentIndex(self.profile_box.findData(self._accepted_profile))
            self.profile_box.blockSignals(previous)
            self.task._panel.feedback.setText(reason)
            self.task._panel.feedback.show()
            self.task._panel.refresh_profile_guard()
            return
        self.table.clearSelection()
        from PySide6.QtCore import QModelIndex

        self.table.setCurrentIndex(QModelIndex())
        self._accepted_profile = target
        # Guard already excludes other-profile capture. Navigation to the
        # active capture profile must not stop that same-profile session.
        self.task.config['InventoryProfileID'] = target or ''
        self.refresh(force=True)
        panel = self.task._panel
        panel.feedback.hide()
        current = next(
            (
                name
                for name, page in panel._owned_pages.items()
                if page is panel.stack.currentWidget()
            ),
            None,
        )
        if current and current != 'inventory':
            # The shared selector is visible on every page. Do not leave the
            # current result/plan attached to its hidden legacy profile field.
            panel.navigate(current)

    def new_profile(self, *, name=None):
        panel = self.task._panel
        if panel.has_edit_draft() or not self.profile_box.isEnabled():
            panel.feedback.setText('请先保存或取消编辑，并结束当前作业，再新建档案。')
            panel.feedback.show()
            return
        super().new_profile(name=name)
        self._accepted_profile = self.profile_box.currentData()

    def start_capture(self):
        try:
            super().start_capture()
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            panel = self.task._panel
            panel.feedback.setText(str(error))
            panel.feedback.show()
            return
        self._show_capture_feedback()

    def pause_capture(self):
        super().pause_capture()
        self._show_capture_feedback()

    def finish_capture(self):
        super().finish_capture()
        self._show_capture_feedback()

    def _show_capture_feedback(self):
        panel = self.task._panel
        if not self.task._capture_ready() or self.runtime.capture.status.state == 'stopped':
            panel.feedback.setText(self.capture_help.text())
            panel.feedback.show()
        else:
            panel.feedback.hide()
        panel.refresh_status()

    def show_details(self):
        selected = self.selected()
        key = (
            (
                id(selected[0]),
                selected[1],
                tuple(selected[2]),
                self.inventory_model.claims.get(selected[0].item_id, ''),
            )
            if selected
            else None
        )
        if getattr(self, '_detail_projection_key', object()) == key:
            return
        self._detail_projection_key = key
        super().show_details()
        if not getattr(self, '_embedded_ready', False):
            return
        if selected is None:
            self.image.setText('暂无选中声骸')
            self.evidence_button.setEnabled(False)
            self._detail_requested = False
            self._update_detail_layout()
            return
        record = selected[0]
        self.evidence_button.setEnabled(bool(record.screenshot_ref))
        self._update_detail_layout()

    def show_pending_review(self):
        if self._match_session() is not None or self._editor is not None:
            raise ValueError('请先应用或取消当前选择、保存或取消编辑，再查看待核对。')
        from echo_workshop.core.filters import InventoryFilters

        self.search_timer.stop()
        self.search_box.clear()
        self.search_timer.stop()
        self.filter_drawer.close_panel()
        self.filter_box.setCurrentIndex(0)
        spec = InventoryFilters(statuses=('pending',))
        self.filter_drawer._applied = spec
        self.library_filters.set_filters(spec)
        self._apply_search()
        self._detail_requested = False
        self._update_detail_layout()

    def _apply_filters(self):
        self._apply_search()

    def _toggle_record_lock(self, identity):
        if self._match_session() is not None:
            return None
        if not self._selection_ready():
            raise ValueError('请先结束当前作业，再修改声骸保护。')
        row = next((row for row in self._rows if row[0].item_id == identity), None)
        if row is None or row[1] != 'item':
            raise ValueError('请先核对这件声骸，再修改本地保护。')
        record = row[0]
        revision = self._mutation_revision()
        profile = self.profile_box.currentData()
        repository = self.runtime.inventory
        impact = repository.selection_impact(profile, (identity,), expected_revision=revision)
        reservation = self.inventory_model.claims.get(identity, '')
        if reservation and not record.locked:
            QMessageBox.information(
                self,
                '配装占用',
                reservation.replace('已预留：', '占用角色：')
                + '。请在已保存配装中选择该声骸解除保护；此处不会释放配装占用。',
            )
            return
        repository.mutate_selection(
            profile,
            (identity,),
            'unlock' if record.locked else 'lock',
            expected_revision=revision,
            expected_loadout_revision=impact['loadout_revision'],
        )
        self.refresh(force=True)

    def _refresh(self, *, force=False):
        # Reader timers also publish outside refresh(); suppress transient model
        # selection notifications until the complete proxy view is available.
        previous = self._match_selection_restoring
        self._match_selection_restoring = True
        try:
            super()._refresh(force=force)
        finally:
            self._match_selection_restoring = previous
        if self._match_session() is not None:
            for button in self._mutation_buttons:
                button.setEnabled(False)
            if not previous:
                self._restore_match_selection()

    def _clear_ready(self):
        return self._match_session() is None and super()._clear_ready()

    def _mutation_revision(self):
        if self._match_session() is not None:
            raise ValueError('正在为匹配选择声骸，请先应用或取消选择，再编辑声骸库。')
        return super()._mutation_revision()

    def _match_session(self):
        panel = getattr(self.task, '_panel', None)
        session = getattr(panel, '_match_inventory_session', None)
        return session if session is not None and session.active else None

    def _on_projection_changed(self):
        session = self._match_session()
        if session is not None:
            self._selection_feedback(session)
        if hasattr(self, 'compact_status'):
            proxy = self.table.proxy
            self.compact_status.setText(
                f'显示 {proxy.rowCount()} / {len(self._rows)} 件'
                if proxy.projection_ready
                else proxy.error or '正在应用筛选…'
            )

    def _visible_match_ids(self):
        from echo_workshop.application.matching.inventory import selectable_inventory_ids

        proxy = self.table.proxy
        if not proxy.projection_ready:
            # 原生筛选仍在后台作业中：可见集合未确定，调用方须延后或提示。
            return None
        key = (
            id(self._rows),
            self.inventory_model.revision,
            proxy.generation,
            self.profile_box.currentData(),
        )
        if getattr(self, '_visible_ids_key', None) == key:
            return self._visible_ids
        rows = []
        for i in range(self.table.proxy.rowCount()):
            source = self.table.proxy.mapToSource(self.table.proxy.index(i, 0)).row()
            if 0 <= source < len(self._rows):
                rows.append(self._rows[source])
        self._visible_ids_key = key
        self._visible_ids = selectable_inventory_ids(rows, self.profile_box.currentData())
        return self._visible_ids

    def _sync_match_selection(self, *_):
        session = self._match_session()
        if session is None or self._match_selection_restoring:
            return
        visible = self._visible_match_ids()
        if visible is None:
            return
        session.sync_visible(
            self.profile_box.currentData(), visible, set(self.table.selected_ids()) & visible
        )
        self._selection_feedback(session)

    def _restore_match_selection(self):
        session = self._match_session()
        if session is None or not hasattr(self, 'match_selection_note'):
            return
        self._match_selection_restoring = True
        try:
            visible = self._visible_match_ids()
            if visible is None:
                return
            ids = session.visible_selected(self.profile_box.currentData(), visible)
            if frozenset(self.table.selected_ids()) != ids:
                current = self.table.currentIndex()
                self.table.restore_selection(
                    ids, current.data(Qt.UserRole) if current.isValid() else None
                )
            self._selection_feedback(session)
        finally:
            self._match_selection_restoring = False

    def _select_all_match_items(self):
        session = self._match_session()
        if session is None or self._reader.status.state != 'ready':
            raise ValueError('请先在匹配中打开声骸选择，并等待声骸库读取完成。')
        self.search_timer.stop()
        self._apply_search()
        visible = self._visible_match_ids()
        if visible is None:
            raise ValueError('正在应用筛选，请稍候再全选。')
        if session.selected_ids and (not visible or visible <= session.selected_ids):
            session.clear_selection(self.profile_box.currentData())
        else:
            session.sync_visible(self.profile_box.currentData(), visible, visible)
        self._restore_match_selection()

    def has_candidate_filter(self):
        from echo_workshop.ui.inventory.filters import filter_count

        return bool(self.search_box.text().strip() or filter_count(self.filter_drawer.filters()))

    def set_match_selection_mode(self, active):
        from PySide6.QtWidgets import QAbstractItemView

        self.table.setSelectionMode(
            QAbstractItemView.MultiSelection if active else QAbstractItemView.ExtendedSelection
        )
        self.match_selection_bar.setVisible(active)
        self.clear_library_button.setEnabled(not active)
        self.table.lock_edit_enabled = not active
        self.operation_hint.setText(
            '选择后返回' + self.selection_destination()
            if active
            else '右键声骸可进行操作，支持多选批量操作'
        )
        if active:
            self._return_grid()
            self._restore_match_selection()

    def _apply_search(self):
        self._match_selection_restoring = True
        try:
            self._search_inventory_controller()
        finally:
            self._match_selection_restoring = False
        self._restore_match_selection()

    def refresh(self, *, force=False):
        self._match_selection_restoring = True
        try:
            self._refresh_inventory_controller(force=force)
        finally:
            self._match_selection_restoring = False
        self._restore_match_selection()

    def _search_inventory_controller(self):
        self._refresh_sonata_options()
        self.table.search(self.search_box.text(), self.filter_drawer.filters())
        from echo_workshop.ui.inventory.filters import filter_count

        active_count = filter_count(self.filter_drawer.filters())
        self.filter_button.setText(f'筛选 ({active_count})' if active_count else '筛选')
        self.show_details()
        count = self.table.model().rowCount()
        text = f'显示 {count} / {len(self._rows)} 件'
        if count == 0:
            text += ' · 没有符合条件的声骸'
        self.compact_status.setText(text)

    def _selection_rows(self):
        selected = set(self.table.selected_ids())
        return tuple(row for row in self._rows if row[0].item_id in selected)

    def _selection_ready(self):
        return self._clear_ready() and self._editor is None and self._reader.status.state == 'ready'

    def _record_menu(self):
        if self._match_session() is not None:
            return None
        rows = self._selection_rows()
        if not rows:
            return None
        menu = QMenu(self)
        single = len(rows) == 1
        if single:
            edit = menu.addAction('核对并编辑')
            edit.setEnabled(self._selection_ready())
            edit.triggered.connect(lambda: self.action(self._edit_context_record))
            if rows[0][1] == 'item':
                analyze = menu.addAction('快速评分')
                analyze.setEnabled(
                    self._selection_ready() and self.inventory_model.quality(rows[0][0]).usable
                )
                analyze.triggered.connect(lambda: self.action(self.quick_score_selected))
        all_items = all(kind == 'item' for _, kind, _ in rows)
        states = {record.locked for record, _, _ in rows}
        actions = (
            [('解除手动保护' if single else '解除所选手动保护', 'unlock')]
            if states == {True}
            else [('手动保护' if single else '手动保护所选', 'lock')]
        )
        if not single and states == {False, True}:
            actions.append(('解除所选手动保护', 'unlock'))
        for label, operation in actions:
            action = menu.addAction(label)
            action.setEnabled(all_items and self._selection_ready())
            action.triggered.connect(
                lambda checked=False, op=operation: self.action(lambda: self._mutate_selection(op))
            )
        menu.addSeparator()
        delete_action = menu.addAction('删除' if single else f'删除所选（{len(rows)} 件）')
        delete_action.setEnabled(self._selection_ready())
        delete_action.triggered.connect(
            lambda: self.action(lambda: self._mutate_selection('delete'))
        )
        return menu

    def _show_record_menu(self, position):
        if self._match_session() is not None:
            return
        index = self.table.indexAt(position)
        if index.isValid() and not self.table.selectionModel().isSelected(index):
            from PySide6.QtCore import QItemSelectionModel

            self.table.selectionModel().setCurrentIndex(index, QItemSelectionModel.ClearAndSelect)
        menu = self._record_menu()
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(position))
            menu.deleteLater()

    def _mutate_selection(self, operation):
        if not self._selection_ready():
            raise ValueError('请先结束收录、扫描或备份，再操作所选声骸。')
        profile = self.profile_box.currentData()
        identities = tuple(record.item_id for record, _, _ in self._selection_rows())
        if not identities:
            return
        revision = self._mutation_revision()
        repository = self.runtime.inventory
        impact = repository.selection_impact(profile, identities, expected_revision=revision)
        if operation == 'delete':
            message = f'删除选中的 {len(identities)} 件声骸？仅删除本地记录，不改动游戏背包。'
            if impact['loadouts']:
                message += (
                    '\n涉及当前配装：'
                    + '、'.join(impact['loadouts'])
                    + '。将解除这些配装的预留，保留历史方案。'
                )
            if impact['related_pending']:
                message += (
                    f'\n同时移除 {impact["related_pending"]} 条依赖这些声骸的重复／升级待判定记录。'
                )
            answer = QMessageBox.question(
                self, '删除声骸', message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if answer != QMessageBox.Yes:
                return
        if not self._selection_ready() or profile != self.profile_box.currentData():
            raise ValueError('档案或当前作业已变化，请重新选择。')
        repository.mutate_selection(
            profile,
            identities,
            operation,
            expected_revision=revision,
            expected_loadout_revision=impact['loadout_revision'],
        )
        if operation == 'delete':
            self.table.clearSelection()
        self.refresh(force=True)

    def _return_grid(self):
        self._detail_requested = False
        self._update_detail_layout()
        self.table.verticalScrollBar().setValue(getattr(self, '_grid_scroll', 0))

    def _update_detail_layout(self):
        if not hasattr(self, 'detail_area'):
            return
        narrow = self.width() < 880
        self.detail_area.setVisible(self._detail_requested)
        self.table.setVisible(not (narrow and self._detail_requested))
        self.back_to_grid.setText('返回声骸库' if narrow else '收起详情')

    def resizeEvent(self, event):
        self._update_detail_layout()
        super().resizeEvent(event)

    def _refresh_inventory_controller(self, *, force=False):
        if getattr(self, '_embedded_ready', False):
            # Load all current records; the four status checkboxes filter one view.
            blocked = self.filter_box.blockSignals(True)
            self.filter_box.setCurrentIndex(0)
            self.filter_box.blockSignals(blocked)
        super().refresh(force=force)
        if getattr(self, '_embedded_ready', False):
            self._refresh_sonata_options()
            self.compact_status.setText(
                f'已载入 {len(self._rows)} 件'
                if self._reader.status.state == 'ready'
                else self._reader.status.message
            )
            if self._reader.status.state == 'ready':
                count = self.table.model().rowCount()
                self.compact_status.setText(
                    (
                        f'显示 {count} / {len(self._rows)} 件'
                        + (' · 没有符合条件的声骸' if not count else '')
                    )
                    if self._rows
                    else '先收录声骸，再核对和计算。'
                )
            count_selected = len(self.table.selected_ids())
            if count_selected:
                self.compact_status.setText(
                    self.compact_status.text() + f' · 已选 {count_selected} 件'
                )
            self.compact_status.setToolTip(self.reading_label.text())
            if self._editor is not None:
                self.profile_box.setEnabled(False)
            self.task._panel.refresh_profile_guard()

    def _refresh_sonata_options(self):
        snapshot = self.runtime.sonata.active
        key = id(self._rows), id(snapshot), getattr(snapshot, 'version', None)
        if getattr(self, '_sonata_options_key', None) != key:
            self.library_filters.refresh_sonatas(snapshot, (row[0] for row in self._rows))
            self._sonata_options_key = key
