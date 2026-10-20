# 管理声骸核对表单待定关联及保存取消流程。
from dataclasses import replace
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from echo_workshop.ui.widgets.inputs import QComboBox
from qfluentwidgets import BodyLabel, PushButton
from echo_workshop.ui.widgets.inputs import ComboBox
from echo_workshop.ui.inventory.cards import EchoCardWidget


class InventoryReviewController:
    def _edit_context_record(self):
        rows = self._selection_rows()
        if len(rows) != 1 or not self._selection_ready():
            raise ValueError('请选中一件声骸，并等待当前作业结束。')
        self._mutation_revision()
        if rows[0][1] == 'item':
            self.edit_selected()
        else:
            self._edit_pending_capture()

    def _edit_inline(self, record, commit, *, confirm=False, previous=None, review_kind=None):
        if self._editor is not None:
            return
        from echo_workshop.ui.widgets.editors import InlineRecordEditor
        from echo_workshop.ui.inventory.review_fields import ReviewChoices

        editor = InlineRecordEditor(
            record,
            self,
            evidence=self.runtime.evidence,
            confirm=confirm,
            previous=previous,
            choices=ReviewChoices.from_runtime(self.runtime, record),
            quality_check=self.runtime.quality_checks.start,
            quality=self.inventory_model.quality(record),
            review_kind=review_kind,
        )
        self._editor = editor
        editor.feedback.setToolTip(editor.feedback.text())
        editor.feedback.setText(
            '核对信息后，勾选“已核对”，再保存并确认。' if confirm else '核对字段后保存。'
        )
        self.filter_drawer.close_panel()
        self.editor_area.setWidget(editor)
        self.inventory_body.hide()
        self.editor_area.show()
        self.profile_box.setEnabled(False)

        def save():
            if self.task._destroyed or self.runtime._closed:
                return
            try:
                commit(editor.result_record)
            except (ValueError, KeyError, OSError) as error:
                editor.feedback.setText('保存未完成：' + str(error))
                return  # Keep the draft and screenshot until explicitly cancelled.
            self._finish_edit()
            self.refresh(force=True)

        editor.saveRequested.connect(save)
        editor.cancelled.connect(self._finish_edit)
        return editor

    def _finish_edit(self):
        editor = self._editor
        self._editor = None
        if editor is not None:
            self.editor_area.takeWidget()
            editor.deleteLater()
        self.editor_area.hide()
        self.inventory_body.show()
        self.refresh()

    def edit_selected(self):
        selected = self.selected()
        if not selected:
            return
        record, kind, _ = selected
        if kind != 'item':
            raise ValueError('疑似重复或升级请明确选择另一件或更新旧件。')
        revision = self._mutation_revision()
        self._edit_inline(
            record,
            lambda value: self.runtime.inventory.update(
                replace(value, review='confirmed'), expected_revision=revision
            ),
            confirm=True,
        )

    def _edit_pending_capture(self):
        if self._editor is not None:
            return
        selected = self.selected()
        if not selected or selected[1] not in ('duplicate', 'upgrade'):
            raise ValueError('请选择待核对的重复或升级记录。')
        record, kind, candidates = selected
        revision = self._mutation_revision()

        def commit(value):
            choice = decision.currentData()
            if choice is None:
                raise ValueError('请选择另存一件或更新旧件。')
            action, target = choice
            self.runtime.inventory.resolve_capture(
                record.item_id,
                action,
                corrected=value,
                target_id=target,
                expected_revision=revision,
            )

        editor = self._edit_inline(record, commit, confirm=True, review_kind=kind)
        decision = ComboBox(editor)
        decision.setAccessibleName('重复或升级记录的处理方式')
        decision.addItem('请选择处理方式', userData=None)
        decision.addItem('另存一件（不同声骸）', userData=('new', None))
        action = 'refresh' if kind == 'duplicate' else 'upgrade'
        previous_records = {}
        for identity in candidates:
            old = self.runtime.inventory.get(identity)
            previous_records[identity] = old
            decision.addItem(
                f'更新旧件：{old.name} · +{old.level} · {identity[:8]}', userData=(action, identity)
            )
        choice_row = QHBoxLayout()
        choice_row.addWidget(BodyLabel('本次收录', editor))
        choice_row.addWidget(decision, 1)
        editor.layout().insertLayout(0, choice_row)
        editor.capture_resolution = decision
        previous_label = BodyLabel('将更新的旧件：请对照词条确认', editor.preview_panel)
        previous_card = EchoCardWidget(editor.preview_panel)
        editor.preview_panel.layout().addWidget(previous_label)
        editor.preview_panel.layout().addWidget(previous_card)
        previous_label.hide()
        previous_card.hide()

        def show_previous():
            choice = decision.currentData()
            target = choice[1] if choice is not None else None
            if target is not None:
                previous_card.set_record(previous_records[target])
            previous_label.setVisible(target is not None)
            previous_card.setVisible(target is not None)
            editor.feedback.setText('核对信息后，勾选“已核对”，再保存并确认。')

        decision.currentIndexChanged.connect(show_previous)

    def review_next(self):
        if self._editor is not None:
            return
        if self.filter_box.currentIndex() != 1:
            self.filter_box.setCurrentIndex(1)
            self._select_next_after = True
            return
        selected = self.selected()
        if selected is None or (
            selected[1] == 'item' and self.inventory_model.quality(selected[0]).usable
        ):
            index = next(
                (
                    i
                    for i, (record, kind, _) in enumerate(self._rows)
                    if (kind != 'item' or not self.inventory_model.quality(record).usable)
                    and self.table.proxy.mapFromSource(self.inventory_model.index(i, 0)).isValid()
                ),
                -1,
            )
            if index < 0:
                self.compact_status.setText('待核对已完成。')
                return
            self.table.selectRow(index)
            selected = self.selected()
        record, kind, _ = selected
        if kind != 'item':
            self.compact_status.setText('请选择另一件或更新旧件；不会自动合并。')
            return
        revision = self._mutation_revision()

        def commit(value):
            self.runtime.inventory.update(
                replace(value, review='confirmed'), expected_revision=revision
            )
            self._select_next_after = record.item_id

        self._edit_inline(record, commit, confirm=True)

    def confirm_selected(self):
        selected = self.selected()
        if not selected:
            return
        if selected[1] == 'item':
            super().confirm_selected()
            return
        revision = self._mutation_revision()
        record = selected[0]
        self._edit_inline(
            record,
            lambda value: self.runtime.inventory.resolve_capture(
                record.item_id, 'new', corrected=value, expected_revision=revision
            ),
            confirm=True,
            review_kind=selected[1],
        )

    def upgrade_selected(self):
        if self._editor is not None:
            return
        selected = self.selected()
        if not selected or selected[1] not in ('upgrade', 'duplicate'):
            raise ValueError('请选择待关联的升级或重复记录。')
        record, kind, candidates = selected
        revision = self._mutation_revision()
        # Explicit target chooser stays inside the page, never chooses a winner.
        chooser = QWidget()
        layout = QVBoxLayout(chooser)
        layout.addWidget(BodyLabel('选择要更新的旧件'))
        choice = QComboBox()
        choice.addItem('请选择旧件', None)
        for key in candidates:
            old = self.runtime.inventory.get(key)
            choice.addItem(f'{old.name} · +{old.level} · {key[:8]}', key)
        layout.addWidget(choice)
        proceed = PushButton('核对旧件与新观察')
        back = PushButton('取消')
        layout.addWidget(proceed)
        layout.addWidget(back)
        self.editor_area.setWidget(chooser)
        self._editor = chooser
        self.profile_box.setEnabled(False)
        self.inventory_body.hide()
        self.editor_area.show()

        def select():
            key = choice.currentData()
            if key is None:
                return
            previous = self.runtime.inventory.get(key)
            self.editor_area.takeWidget()
            self._editor = None
            chooser.deleteLater()
            self._edit_inline(
                record,
                lambda value: self.runtime.inventory.resolve_capture(
                    record.item_id,
                    'refresh' if kind == 'duplicate' else 'upgrade',
                    target_id=key,
                    corrected=value,
                    expected_revision=revision,
                ),
                confirm=True,
                previous=previous,
                review_kind=kind,
            )

        proceed.clicked.connect(select)
        back.clicked.connect(self._finish_edit)
