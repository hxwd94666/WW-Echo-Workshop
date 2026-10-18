# 冻结匹配所用库存和候选选择。
"""Read-only cross-page inventory selection, frozen by opaque instance IDs.

The assistant panel owns one session while the inventory grid remains the
ordinary presentation. This module has no Qt, repository, scoring, or mutation
dependency; applying returns explicit IDs, never a live query or an ``all`` flag.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


def _ids(values):
    if isinstance(values, (str, bytes)):
        raise ValueError('声骸选择必须是实例 ID 集合。')
    try:
        result = frozenset(values)
    except TypeError as error:
        raise ValueError('声骸选择必须是实例 ID 集合。') from error
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError('声骸选择包含无效实例 ID。')
    return result


@dataclass(frozen=True)
class InventoryViewState:
    """Opaque library presentation state to restore on either exit path."""

    profile_id: str
    search: str = ''
    filters: Any = None
    selected_ids: frozenset = field(default_factory=frozenset)
    current_id: str | None = None
    scroll: int = 0

    def __post_init__(self):
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise ValueError('请选择本地档案。')
        if not isinstance(self.search, str):
            raise ValueError('声骸库搜索状态无效。')
        object.__setattr__(self, 'selected_ids', _ids(self.selected_ids))
        object.__setattr__(self, 'filters', deepcopy(self.filters))
        if self.current_id is not None and (
            not isinstance(self.current_id, str) or not self.current_id
        ):
            raise ValueError('声骸库当前选择无效。')
        if type(self.scroll) is not int or self.scroll < 0:
            raise ValueError('声骸库滚动位置无效。')


@dataclass
class MatchInventorySession:
    """One profile-bound draft; hidden checked IDs survive search and reload.

    ``previous_ids=None`` means the caller previously used a dynamic matching
    scope; ``frozenset()`` means an explicitly empty scope. Both cancel intact.
    Selection methods must be called only for real user changes, not transient Qt
    model-reset/filter restoration notifications.
    """

    profile_id: str
    previous_ids: frozenset | None
    view_state: InventoryViewState
    return_route: str = 'comparison'
    active: bool = field(default=True, init=False)
    _draft_ids: frozenset = field(default_factory=frozenset, init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise ValueError('请选择本地档案。')
        if (
            not isinstance(self.view_state, InventoryViewState)
            or self.view_state.profile_id != self.profile_id
        ):
            raise ValueError('声骸选择会话与声骸库档案不一致。')
        if self.return_route not in ('comparison', 'allocation'):
            raise ValueError('声骸选择的返回页面无效。')
        if self.previous_ids is not None:
            self.previous_ids = _ids(self.previous_ids)
            self._draft_ids = self.previous_ids

    @property
    def selected_ids(self):
        return self._draft_ids

    def ensure_profile(self, profile_id):
        if not self.active:
            raise ValueError('声骸选择已结束，请重新打开选择。')
        if profile_id != self.profile_id:
            raise ValueError('选择声骸期间档案已变化，请取消后重新选择。')

    def sync_visible(self, profile_id, visible_ids, selected_ids):
        """Replace only the visible part of the draft after a user selection."""
        self.ensure_profile(profile_id)
        visible, selected = _ids(visible_ids), _ids(selected_ids)
        if not selected <= visible:
            raise ValueError('选中声骸与当前可见列表不一致，请刷新后重试。')
        self._draft_ids = (self._draft_ids - visible) | selected
        return self._draft_ids

    def visible_selected(self, profile_id, visible_ids):
        """Restore projection without editing hidden or missing selections."""
        self.ensure_profile(profile_id)
        return self._draft_ids & _ids(visible_ids)

    def clear_selection(self, profile_id):
        """Explicit clear includes selected IDs hidden by the current filter."""
        self.ensure_profile(profile_id)
        self._draft_ids = frozenset()

    def apply_selected(self, profile_id):
        self.ensure_profile(profile_id)
        result = frozenset(self._draft_ids)
        self.active = False
        return result

    def apply_filtered(self, profile_id, visible_ids):
        self.ensure_profile(profile_id)
        result = _ids(visible_ids)
        self.active = False
        return result

    def cancel(self, profile_id):
        self.ensure_profile(profile_id)
        self.active = False
        return self.view_state


def selectable_inventory_ids(rows, profile_id):
    """Collect ordinary inventory IDs, excluding pending-capture observations.

    Accepts the InventoryModel source triples ``(record, kind, detail)``. The caller
    supplies only rows currently accepted by the card grid's proxy. No UI row
    position, display name, fingerprint, score, or global ``all`` flag is retained.
    """
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError('请选择本地档案。')
    result = []
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            raise ValueError('声骸库列表格式无效。')
        record, kind, _ = row
        if getattr(record, 'profile_id', None) != profile_id:
            raise ValueError('声骸库列表包含其他档案的数据，请刷新后重试。')
        if kind == 'item':
            result.append(getattr(record, 'item_id', None))
    return _ids(result)
