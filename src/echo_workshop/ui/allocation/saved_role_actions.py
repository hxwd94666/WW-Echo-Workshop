# 管理已保存角色配装的声骸多选和明确范围操作。
from dataclasses import replace
from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, QTimer

from echo_workshop.core.loadout import protected_item_ids


class SavedRoleActions:
    def _live_inventory(self):
        snapshot = self.page._current_loadouts
        if snapshot is not getattr(self, '_live_inventory_source', None):
            self._live_inventory_source = snapshot
            self._live_records = (
                {record.item_id: record for record in snapshot.records} if snapshot else {}
            )
            self._live_claims = snapshot.owners if snapshot else {}
            self._role_record_projections = {}
        return getattr(self, '_live_records', {}), getattr(self, '_live_claims', {})

    def selected_item_ids(self):
        return tuple(self.detail_cards.cards.selected_ids()) if self.current_detail() else ()

    def selected_item_id(self):
        values = self.selected_item_ids()
        return values[0] if len(values) == 1 else None

    def protected_selection(self):
        row = self.selection()
        if self.mode != 'current' or row is None:
            return ()
        allowed = set(protected_item_ids(row))
        live, owners = self._live_inventory()
        allowed.update(
            item
            for item in row['item_ids']
            if item in live
            and live[item].locked
            and owners.get(item) in (None, row['character_id'])
        )
        return tuple(value for value in self.selected_item_ids() if value in allowed)

    def _release_selected(self):
        context, values = self.detail_context(), self.protected_selection()
        if not self._busy() and context is not None and values:
            self.page.action(
                lambda: self.page.saved_role_operation('release-items', context, values)
            )

    def _set_saved_role(self, role, records, snapshot, identity):
        grid = self.detail_cards.cards
        previous = (
            self.selected_item_ids()
            if getattr(self, '_rendered_saved_identity', None) == identity
            else ()
        )
        same_role = getattr(self, '_rendered_saved_identity', None) == identity
        scroll = (
            (grid.horizontalScrollBar().value(), grid.verticalScrollBar().value())
            if same_role
            else (0, 0)
        )
        self._saved_scroll_generation = getattr(self, '_saved_scroll_generation', 0) + 1
        scroll_generation = self._saved_scroll_generation
        selection = grid.selectionModel()
        blocked = selection.blockSignals(True)
        try:
            live, claims = self._live_inventory()
            cache = getattr(self, '_role_record_projections', {})
            key = (identity, tuple(role['item_ids']), id(records))
            cached = cache.get(key)
            if cached is None or cached[0] is not records:
                wanted = set(role['item_ids'])
                projected = tuple(
                    replace(
                        record,
                        locked=live[record.item_id].locked or record.item_id in claims,
                        owner=live[record.item_id].owner,
                        owner_character_id=live[record.item_id].owner_character_id,
                        owner_state=live[record.item_id].owner_state,
                    )
                    if record.item_id in live
                    and (
                        record.locked != (live[record.item_id].locked or record.item_id in claims)
                        or record.owner != live[record.item_id].owner
                        or record.owner_character_id != live[record.item_id].owner_character_id
                        or record.owner_state != live[record.item_id].owner_state
                    )
                    else record
                    for record in records
                    if record.item_id in wanted
                )
                cache[key] = (records, projected)
                while len(cache) > 96:
                    del cache[next(iter(cache))]
                self._role_record_projections = cache
            self.detail_cards.set_role(role, cache[key][1], snapshot=snapshot)
            grid.clearSelection()
            grid.setCurrentIndex(QModelIndex())
            for index in range(self.detail_cards.model.rowCount()):
                source = self.detail_cards.model.index(index, 0)
                if source.data(Qt.UserRole) in previous:
                    selection.select(grid.proxy.mapFromSource(source), QItemSelectionModel.Select)
        finally:
            selection.blockSignals(blocked)
        self._rendered_saved_identity = identity

        def restore_scroll():
            if (
                self._lifecycle_active()
                and self._rendered_saved_identity == identity
                and self._saved_scroll_generation == scroll_generation
            ):
                grid.doItemsLayout()
                grid.horizontalScrollBar().setValue(scroll[0])
                grid.verticalScrollBar().setValue(scroll[1])

        restore_scroll()
        QTimer.singleShot(0, grid, restore_scroll)

    def selection_note(self):
        if not self.current_detail():
            return ''
        if self._drafts.get(self._draft_key()) is not None:
            return (
                '替换预览：另存备份不改变占用；设为当前配装才生效。'
                if self.mode == 'history'
                else '替换预览，更新当前配装后才生效。'
            )
        return f'已选 {len(self.selected_item_ids())} 件 · 点击声骸可多选，单选时可替换。'

    def publish_role_operation(self, kind, token, count, *, readback=True):
        self._detail_signature = None
        self.project(force=True)
        if token != self.view_token:
            return
        self.status.setText(
            '操作已提交，正在刷新声骸库，请勿重复操作。'
            if not readback
            else f'已解除 {count} 件声骸的保护。'
        )
        self.status.show()
