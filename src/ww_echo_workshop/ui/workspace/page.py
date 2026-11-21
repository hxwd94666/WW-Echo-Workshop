# 工作台页面：聚合实时评分策略、声骸强化测试和数据更新入口。
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWidgets import QPushButton

from ww_echo_workshop.ui.widgets.inputs import QComboBox
from ww_echo_workshop.ui.workspace.data_card import PolicyDataCard
from ww_echo_workshop.ui.workspace.hud import HudController
from ww_echo_workshop.ui.workspace.live_card import LivePolicyCard
from ww_echo_workshop.ui.workspace.test_card import PolicyTestCard

if TYPE_CHECKING:
    from ww_echo_workshop.application.hud_state import HudState
    from ww_echo_workshop.application.live_preferences import LivePreferences
    from ww_echo_workshop.application.live_snapshot import LiveSnapshot
    from ww_echo_workshop.application.policy_resource_jobs import PolicyResourceJobs
    from ww_echo_workshop.application.policy_ui_state import PolicyUiStateStore
    from ww_echo_workshop.application.preset_catalog import PresetCatalog
    from ww_echo_workshop.core.policy import PolicyResult
    from ww_echo_workshop.resources.weights import WeightSnapshot

# Prefer the full-width stacked workbench until the page is genuinely wide.
WIDE_BREAKPOINT = 1600


@dataclass(frozen=True)
class WorkspacePageServices:
    preferences: 'LivePreferences'
    policy_resources: 'PolicyResourceJobs'
    live_presets: 'PresetCatalog'
    test_presets: 'PresetCatalog'
    test_state: 'PolicyUiStateStore'
    hud_state: 'HudState'
    weight_snapshot: Callable[[], 'WeightSnapshot']
    live_snapshot: Callable[[], 'LiveSnapshot | None']
    solve_policy: Callable[[dict], 'PolicyResult']
    closed: Callable[[], bool]


class WorkspacePage(QWidget):
    def __init__(
        self,
        services: WorkspacePageServices,
        parent=None,
        *,
        profiles: Mapping[str, str],
        profile_id: str | None,
        weight_settings,
        open_weights,
    ):
        super().__init__(parent)
        self.setObjectName('workspacePage')
        self.setMinimumSize(0, 0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)
        self.profile_box = QComboBox(self)
        for profile_id, label in profiles.items():
            self.profile_box.addItem(label, userData=profile_id)
        index = self.profile_box.findData(profile_id)
        self.profile_box.setCurrentIndex(index if index >= 0 else 0)
        self.profile_box.currentIndexChanged.connect(self.profile_changed)
        self.profile_box.hide()

        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.setFrameShape(QScrollArea.NoFrame)
        body = QWidget(self.area)
        body.setObjectName('workspaceBody')
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 2, 0, 2)
        content.setSpacing(12)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        self.live_card = LivePolicyCard(
            body,
            preferences=services.preferences,
            weight_snapshot=services.weight_snapshot,
            live_snapshot=services.live_snapshot,
            presets=services.live_presets,
            reset_hud=lambda: self.hud.reset_auto_position(),
        )
        self.hud = HudController(
            services.preferences,
            self,
            weight_snapshot=services.weight_snapshot,
            state=services.hud_state,
            closed=services.closed,
        )
        self.test_card = PolicyTestCard(
            body,
            preferences=services.preferences,
            weight_snapshot=services.weight_snapshot,
            live_snapshot=services.live_snapshot,
            presets=services.test_presets,
            state_store=services.test_state,
            solve=services.solve_policy,
        )
        self.live_card.preset_changed.connect(self.test_card.sync_preset)
        self.test_card.preset_changed.connect(self.live_card.sync_preset)
        self.data_card = PolicyDataCard(
            body,
            jobs=services.policy_resources,
            weight_snapshot=services.weight_snapshot,
            weight_settings=weight_settings,
            open_weights=open_weights,
            policy_changed=self._policy_data_changed,
        )
        self.data_card.hide()
        self.data_toggle = QPushButton('▸  数据与更新', body)
        self.data_toggle.setObjectName('workshopDataToggle')
        self.data_toggle.setAccessibleName('展开数据与更新')
        self.data_toggle.clicked.connect(self._toggle_data)
        self.left_column = QWidget(body)
        self.left_layout = QVBoxLayout(self.left_column)
        self.left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_layout.setSpacing(12)
        self.left_layout.addWidget(self.live_card)
        self.left_layout.addWidget(self.data_toggle)
        self.left_layout.addWidget(self.data_card)
        self.left_layout.addStretch()
        self.live_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.data_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.left_column.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.test_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._wide = None
        self._arrange(False)
        content.addLayout(self.grid)
        content.addStretch()
        self.area.setWidget(body)
        outer.addWidget(self.area, 1)

    def _arrange(self, wide):
        if self._wide == wide:
            return
        self._wide = wide
        cards = (self.left_column, self.test_card)
        for card in cards:
            self.grid.removeWidget(card)
        for column in range(2):
            self.grid.setColumnStretch(column, 0)
        if wide:
            self.grid.addWidget(self.left_column, 0, 0)
            self.grid.addWidget(self.test_card, 0, 1)
            self.grid.setColumnStretch(0, 4)
            self.grid.setColumnStretch(1, 6)
        else:
            self.grid.addWidget(self.left_column, 0, 0)
            self.grid.addWidget(self.test_card, 1, 0)
            self.grid.setColumnStretch(0, 1)
        for card in cards:
            card.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'grid'):
            self._arrange(self.width() >= WIDE_BREAKPOINT)

    def _toggle_data(self):
        expanded = not self.data_card.isVisible()
        self.data_card.setVisible(expanded)
        self.data_toggle.setText(('▾  ' if expanded else '▸  ') + '数据与更新')
        if expanded:
            self.data_card.refresh()
        self.left_layout.activate()

    def profile_changed(self, *_):
        if not hasattr(self, 'data_card'):
            return
        self.data_card.refresh()

    def apply_surface_palette(self, palette):
        self.setPalette(palette)

    def refresh_summary(self):
        return

    def _policy_data_changed(self):
        self.live_card.refresh_presets()
        self.test_card.refresh_presets()
        self.test_card.reload_policy_data()

    def closeEvent(self, event):
        self.hud.close()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.live_card.refresh_status()
        self.data_card.refresh()
        self.refresh_summary()
        self._arrange(self.width() >= WIDE_BREAKPOINT)
        self.left_column.layout().activate()

    def lifecycle_busy(self):
        return False

    def lifecycle_draft(self):
        return False

    def lifecycle_rule_dirty(self):
        return False

    def request_stop(self):
        self.test_card.shutdown()
        self.data_card.stop_worker()
