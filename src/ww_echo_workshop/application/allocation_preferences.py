# 读写独立配装工作草稿，保留其他档案与未参与角色。
import json

from ww_echo_workshop.core.allocation_preferences import (
    allocation_settings,
    pending_role_keys,
)
from ww_echo_workshop.core.matching_calculation import (
    normalize_candidate_ids,
    restrict_priority_groups,
)

__all__ = ['AllocationPreferences', 'allocation_settings', 'pending_role_keys']


class AllocationPreferences:
    def __init__(self, read, write):
        self._read, self._write = read, write

    def read(self):
        return allocation_settings({'AllocationSettingsJSON': self._read()})

    def save_draft(
        self,
        profile_id,
        *,
        roles,
        fully_tuned,
        pending,
        candidate_ids,
        priority_groups,
        subset_adjustment,
        source_plan,
        removed_role_ids,
    ):
        data = self.read()
        rows = [role.to_dict() for role in roles]
        saved = data['profiles'].get(profile_id, {})
        if subset_adjustment:
            active = {role.character_id for role in roles}
            rows += [
                row
                for row in saved.get('roles', [])
                if row.get('character_id') not in active
                and row.get('character_id') not in removed_role_ids
            ]
        draft = dict(
            roles=rows,
            fully_tuned=fully_tuned,
            pending_roles=pending_role_keys(pending, rows),
            candidate_item_ids=normalize_candidate_ids(candidate_ids),
        )
        if 'search_budget' in saved:
            draft['search_budget'] = dict(saved['search_budget'])
        groups = priority_groups
        if subset_adjustment and source_plan and saved.get('priority_groups'):
            keys = {row['character_id']: (row['character_id'], row['modal_id']) for row in rows}
            groups = tuple(
                tuple(keys[key[0]] for key in group if key[0] in keys)
                for group in saved['priority_groups']
            )
        if groups:
            draft['priority_groups'] = restrict_priority_groups(
                groups, tuple((row['character_id'], row['modal_id']) for row in rows)
            )
        data['profiles'][profile_id] = draft
        self._write(json.dumps(data, ensure_ascii=False, allow_nan=False))
