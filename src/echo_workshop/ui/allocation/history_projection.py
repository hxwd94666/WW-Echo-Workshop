# 缓存不可变配装来源到角色侧栏的投影，并复用每个角色的头像图标。
from echo_workshop.application.compute_bridge import rating_result
from echo_workshop.ui.allocation.workspace import avatar_icon
from qfluentwidgets import isDarkTheme


class SavedListProjection:
    def _rows(self):
        p = self.page
        snapshot = p._current_loadouts
        summaries = getattr(p, '_saved_plan_summaries', {})
        key = (
            self.mode,
            p.profile.currentData(),
            id(snapshot),
            id(summaries),
            getattr(p, '_saved_summaries_profile', None),
            p.saved.count(),
            self.show_versions.isChecked(),
            getattr(self, '_details_revision', 0) if self.mode == 'current' else 0,
        )
        if key != getattr(self, '_rows_source_key', None):
            self._rows_sources = (snapshot, summaries)
            self._rows_source_key = key
            self._projected_rows = self._build_rows()
            self._rows_revision = getattr(self, '_rows_revision', 0) + 1
        return self._projected_rows

    def _icon_revision(self):
        avatars = self.page.runtime.avatars
        return id(avatars), avatars.revision, isDarkTheme()

    def _role_icon(self, character_id):
        revision = self._icon_revision()
        if revision != getattr(self, '_icon_cache_revision', None):
            self._icon_cache_revision = revision
            self._role_icons = {}
        if character_id not in self._role_icons:
            self._role_icons[character_id] = avatar_icon(self.page.runtime.avatars, character_id)
        return self._role_icons[character_id]

    def _refresh_icons(self):
        revision = self._icon_revision()
        if revision == getattr(self, '_list_icon_revision', None):
            return
        for index in range(self.list.count()):
            item = self.list.item(index)
            from PySide6.QtCore import Qt

            row = self._entries.get(item.data(Qt.UserRole))
            if row is not None:
                item.setIcon(self._role_icon(row['character_id']))
        self._list_icon_revision = revision

    def _build_rows(self):
        p = self.page
        if self.mode == 'current':
            snapshot = p._current_loadouts
            if snapshot is None or snapshot.profile_id != p.profile.currentData():
                return []
            return [self._current_row(row) for row in snapshot.current]
        if getattr(p, '_saved_summaries_profile', None) != p.profile.currentData():
            return []
        rows = []
        summaries = getattr(p, '_saved_plan_summaries', {})
        for i in range(p.saved.count()):
            plan = p.saved.itemData(i)
            summary = summaries.get(plan, {})
            origin = (summary.get('publication') or {}).get('kind', 'legacy')
            if origin != 'backup' and not self.show_versions.isChecked():
                continue
            removed = {tuple(key) for key in summary.get('removed_roles', [])}
            for role in summary.get('roles', ()):
                if (role['character_id'], role['modal_id']) in removed:
                    continue
                rows.append(
                    dict(
                        role,
                        origin=origin,
                        plan_id=plan,
                        created_at=summary.get('created_at', p.saved.itemText(i).split(' · ')[0]),
                    )
                )
        return rows

    def _current_row(self, row):
        """Merge only the exact saved role's original score and saved timestamp."""
        row = dict(row)
        profile = self.page.profile.currentData()
        summaries = (
            getattr(self.page, '_saved_plan_summaries', {})
            if getattr(self.page, '_saved_summaries_profile', None) == profile
            else {}
        )
        summary = summaries.get(row['plan_id'], {})
        key = row['character_id'], row['modal_id']
        frozen = next(
            (
                item
                for item in summary.get('roles', ())
                if (item['character_id'], item['modal_id']) == key
            ),
            None,
        )
        cached = self._details.get((profile, row['plan_id']))
        payload = cached[0] if cached else {}
        if frozen is None and payload:
            role = next(
                (
                    item
                    for item in payload['result']['roles']
                    if (item['character_id'], item['modal_id']) == key
                ),
                None,
            )
            if role is not None:
                frozen = dict(total_cents=role['total_cents'])
                ids = role['item_ids']
                valid = len(ids) == len(set(ids)) == 5 and len(role['scores']) == 5
                rating = rating_result(role.get('rating')) if valid else None
                if rating is not None:
                    frozen['rating_label'] = rating.label
        if frozen is not None:
            for field in ('total_cents', 'rating_label'):
                if field in frozen:
                    row[field] = frozen[field]
        row['created_at'] = (
            summary.get('created_at') or payload.get('created_at') or row.get('updated_at', '')
        )
        return row
