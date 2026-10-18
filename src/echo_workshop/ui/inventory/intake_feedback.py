# 投影真实核对集合、候选选择范围及扫描失败后的重新获取路线。
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import BodyLabel, CaptionLabel, PushButton
from echo_workshop.ui.widgets.layout import fit_dialog


class InventoryIntakeFeedback:
    def selection_destination(self):
        session = self._match_session()
        return '配装' if session is not None and session.return_route == 'allocation' else '匹配'

    def _selection_feedback(self, session):
        visible = self._visible_match_ids()
        ready = self._reader.status.state == 'ready' and visible is not None
        if visible is None:
            # 原生筛选仍在作业中：只更新提示，不猜测可见集合。
            self.match_selection_note.setText(
                f'为{self.selection_destination()}选择 · 正在应用筛选…'
            )
            for button in (
                self.match_select_all_button,
                self.match_apply_filtered,
                self.match_apply_selected,
            ):
                button.setEnabled(False)
            return
        count = len(session.selected_ids)
        hidden = len(session.selected_ids - visible)
        clearing = bool(count and (not visible or visible <= session.selected_ids))
        ready = self._reader.status.state == 'ready'
        self.match_apply_selected.setEnabled(ready)
        self.match_select_all_button.setText('清空' if clearing else '全选')
        self.match_select_all_button.setToolTip(
            '清空全部勾选，包括被筛选隐藏的已选件。'
            if clearing
            else '全选当前可见声骸，保留被筛选隐藏的已选件。'
        )
        self.match_select_all_button.setEnabled(ready and bool(visible or count))
        filtered = self.has_candidate_filter()
        self.match_apply_filtered.setEnabled(ready and filtered)
        text = f'为{self.selection_destination()}选择 · 已选 {count} 件'
        if hidden:
            text += f'，其中 {hidden} 件在筛选外'
        self.match_selection_note.setText(text)
        self.match_apply_selected.setToolTip(
            f'采用全部 {count} 件勾选项，包含筛选外 {hidden} 件；返回{self.selection_destination()}，不会自动计算。'
        )
        self.match_apply_filtered.setToolTip(
            f'仅采用当前筛选结果中的 {len(visible)} 件声骸，不保留筛选外勾选；返回{self.selection_destination()}，不会自动计算。'
            if filtered
            else '尚未设置搜索或筛选条件；请手动勾选，或点击“全选”。'
        )
        self.match_apply_selected.setText(f'确定勾选 ({count})')
        self.match_apply_filtered.setText(f'使用筛选结果 ({len(visible)})')

    def show_scan_failures(self):
        scan = self.runtime.scan
        snapshot = scan.failure_snapshot()
        if snapshot['profile_id'] != self.profile_box.currentData():
            raise ValueError('失败记录属于其他档案，请先切回对应档案。')
        if snapshot['state'] in ('preparing', 'running', 'paused', 'draining'):
            raise ValueError('请先停止扫描，再查看未收录项。')
        dialog = QDialog(self)
        dialog.setWindowTitle('扫描未收录项')
        dialog.setPalette(self.palette())
        dialog.setAutoFillBackground(True)
        layout = QVBoxLayout(dialog)
        heading = BodyLabel(f'未收录失败 {snapshot["total"]} 件 · 已收录声骸保留', dialog)
        layout.addWidget(heading)
        note = CaptionLabel(
            '位置按本次扫描的原列表计数；请保持相同排序和筛选，重新定位后获取新画面。', dialog
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        tree = QTreeWidget(dialog)
        tree.setHeaderLabels(('原列表位置', '原因'))
        for failure in sorted(snapshot['details'], key=lambda value: (value.row, value.column)):
            tree.addTopLevelItem(
                QTreeWidgetItem(
                    (f'第 {failure.row + 1} 行 · 第 {failure.column + 1} 列', failure.message)
                )
            )
        tree.setRootIsDecorated(False)
        tree.setSelectionMode(QTreeWidget.SingleSelection)
        tree.header().setStretchLastSection(True)
        layout.addWidget(tree, 1)
        if snapshot['omitted']:
            overflow = CaptionLabel(
                f'另有 {snapshot["omitted"]} 件超过清单上限；建议重新扫描当前范围。', dialog
            )
            overflow.setWordWrap(True)
            layout.addWidget(overflow)
        row = QHBoxLayout()
        for label, mode in (('前往手动重新获取', 'manual'), ('前往重新扫描', 'automatic')):
            button = PushButton(label, dialog)

            def navigate(_checked=False, target=mode):
                try:
                    self.prepare_reacquisition(snapshot, target)
                except (ValueError, KeyError, OSError) as error:
                    note.setText(str(error))
                    return
                dialog.accept()

            button.clicked.connect(navigate)
            row.addWidget(button)
        layout.addLayout(row)
        close = QDialogButtonBox(QDialogButtonBox.Close, dialog)
        close.button(QDialogButtonBox.Close).setText('关闭')
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        fit_dialog(dialog, 620, 470)
        self._scan_failure_dialog = dialog
        dialog.open()
        return dialog

    def prepare_reacquisition(self, snapshot, mode):
        if mode not in ('manual', 'automatic'):
            raise ValueError('请选择重新获取方式。')
        current = self.runtime.scan.failure_snapshot()
        if (snapshot['profile_id'], snapshot['generation']) != (
            current['profile_id'],
            current['generation'],
        ):
            raise ValueError('扫描批次已变化，请重新打开失败清单。')
        if snapshot['profile_id'] != self.profile_box.currentData() or not self._selection_ready():
            raise ValueError('请先完成当前编辑或作业，再重新获取。')
        panel = self.task._panel
        panel.navigate('inventory')
        if panel.stack.currentWidget() is not self:
            raise ValueError('请先保存或取消当前编辑，再返回声骸库。')
        self.task.config['IntakeMode'] = mode
        self.refresh_capture_controls()
        self.start_button.setFocus(Qt.OtherFocusReason)
        panel.feedback.setText(
            '已切到手动收录；在游戏中定位失败声骸后重新收录，仍需新画面核对。'
            if mode == 'manual'
            else '已切到自动扫描；保持原筛选、回到列表顶部后点击开始扫描。'
        )
        panel.feedback.show()
