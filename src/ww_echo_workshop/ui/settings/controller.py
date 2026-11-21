# 装配工坊设置分组与明确应用的评分和诊断选项。
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QVBoxLayout, QWidget
from PySide6.QtWidgets import QPushButton
from ww_echo_workshop.ui.widgets.controls import CardWidget, SubtitleLabel

if TYPE_CHECKING:
    from ww_echo_workshop.application.backup_jobs import BackupService
    from ww_echo_workshop.application.profiles import ProfileSettingsService
    from ww_echo_workshop.application.resource_settings import ResourceSettings
    from ww_echo_workshop.application.screenshot_cleanup import CleanupStatus
    from ww_echo_workshop.ui.settings.repair import RepairControls
    from ww_echo_workshop.ui.settings.shortcuts import ShortcutControls


@dataclass(frozen=True)
class SettingsServices:
    profiles: 'ProfileSettingsService'
    backups: 'BackupService'
    resources: 'ResourceSettings'
    shortcuts: 'ShortcutControls'
    new_repair: Callable[[], 'RepairControls']
    request_clear_screenshots: Callable[[], bool]
    screenshot_cleanup_status: Callable[[], 'CleanupStatus']
    deleted_profile_archives: Path
    stop_event: Event


class _SettingsSection(CardWidget):
    """Always-visible section; the whole settings page owns the only scrollbar."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        heading = SubtitleLabel(title, self)
        layout.addWidget(heading)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        layout.addWidget(self.content)


class AssistantSettingsController:
    def _build_settings(self):
        from ww_echo_workshop.ui.settings.groups import GroupedSettingsSurface

        area = GroupedSettingsSurface(self)
        body = area.body
        self.settings_sections = {}
        for key, title, group in (
            ('shortcuts', '收录与快捷键', 'intake'),
            ('weights', '自定义评分权重', 'scoring'),
            ('search', '匹配与配装计算', 'scoring'),
            ('profiles', '档案管理', 'data'),
            ('screenshots', '声骸截图', 'data'),
            ('plugin', '诊断与插件', 'advanced'),
        ):
            section = _SettingsSection(title, body)
            self.settings_sections[key] = section
            area.add_section(key, section, group)
        inventory = self.inventory_page
        # This is the same authoritative header control, not a second setting.
        self.settings_scan_level = inventory.scan_level
        intake = self.settings_sections['shortcuts']
        intake.setToolTip('自动扫描等级在顶部设置；扫描前将背包拉到顶部并切回游戏。')
        self.advanced = self.settings_sections[
            'search'
        ]  # Existing public owner of global controls.
        # Legacy transfer controllers stay compatible but are not duplicate
        # daily actions. All profile CRUD/portable transfer is in one card.
        self.build_profile_management(self.settings_sections['profiles'])
        from ww_echo_workshop.ui.settings.screenshots import ScreenshotSettings

        screenshots = self.settings_sections['screenshots']
        self.screenshot_settings = ScreenshotSettings(
            screenshots.content,
            request_clear=self.settings_services.request_clear_screenshots,
            cleanup_status=self.settings_services.screenshot_cleanup_status,
        )
        screenshots.content_layout.addWidget(self.screenshot_settings)
        self.plugin_management = self.settings_sections['plugin']
        self.export_diagnostics_button = QPushButton('导出诊断报告')
        self.export_diagnostics_button.setToolTip('仅运行摘要，不含声骸库、截图、OCR或账号。')
        self.export_diagnostics_button.clicked.connect(self.export_diagnostics)
        self.plugin_management.content_layout.addWidget(self.export_diagnostics_button)
        self.uninstall_button = QPushButton('移除插件')
        self.uninstall_button.setToolTip('移除插件后，已收录的声骸库仍保留。')
        self.uninstall_button.setEnabled(False)  # Actual host integration owns this callback.
        self.plugin_management.content_layout.addWidget(self.uninstall_button)
        area.content_layout.addStretch()
        body.setAutoFillBackground(True)
        self._settings_surfaces = (area, area.viewport(), body)
        for surface in self._settings_surfaces:
            surface.setPalette(self.palette())
        self._settings_ready = False
        area.groupChanged.connect(self._populate_settings)
        return area

    def ensure_weight_settings(self):
        if not hasattr(self, 'weights'):
            from ww_echo_workshop.ui.settings.page import WeightSettings

            self.weights = WeightSettings(self.settings_services.resources, self)
            self.weights.advanced_controls.hide()
        return self.weights

    def _ensure_weights(self):
        weights = self.ensure_weight_settings()
        section = self.settings_sections['weights']
        if weights.parent() is not section.content:
            section.content_layout.addWidget(weights)
        weights.ensure_custom_editor()
        return weights

    def _populate_settings(self, group=None):
        group = group or self.settings_page.current_group
        if group == 'intake' and not hasattr(self, 'shortcuts'):
            from ww_echo_workshop.ui.settings.page import ShortcutSettings

            section = self.settings_sections['shortcuts']
            self.shortcuts = ShortcutSettings(self.settings_services.shortcuts, section.content)
            section.content_layout.addWidget(self.shortcuts)
        if group == 'scoring':
            weights = self._ensure_weights()
            weights.ensure_custom_editor()
            self.show_budgets()
        self._settings_ready = True

    def _ensure_repair(self):
        # A repair service is still needed for existing damaged selectors or
        # interrupted startup. It is not a duplicate everyday backup action.
        if hasattr(self, 'repair'):
            return
        from ww_echo_workshop.ui.settings.page import RepairSettings

        section = _SettingsSection('启动配置修复', self.settings_page.body)
        self.settings_sections['repair'] = section
        self.settings_page.add_section('repair', section, 'advanced')
        self.repair = RepairSettings(self.settings_services.new_repair(), section.content)
        section.content_layout.addWidget(self.repair)

    def open_settings_section(self, key):
        if self._stopped:
            return self
        if key == 'repair':
            self._ensure_repair()
        if self._match_inventory_session is not None:
            self.cancel_inventory_selection()
        self.stack.setCurrentWidget(self.settings_page)
        self.settings_page.show_group(self.settings_page.group_for_section(key))
        self.navigation.hide()
        self.settings_button.setText('返回')
        self.settings_page.ensureWidgetVisible(self.settings_sections[key])
        return self

    def export_diagnostics(self):
        from PySide6.QtWidgets import QFileDialog
        from ww_echo_workshop.application.diagnostics import export_diagnostics

        path, _ = QFileDialog.getSaveFileName(
            self, '导出运行诊断（不含声骸库和截图）', 'echo-diagnostics.json', 'JSON (*.json)'
        )
        if not path:
            return
        try:
            from ww_echo_workshop.resources.paths import package_manifest

            manifest = package_manifest()
            export_diagnostics(
                path,
                context={
                    'plugin_version': manifest.get('version', ''),
                    'auto_weight_updates': self.settings_services.resources.auto(),
                },
            )
            self.feedback.setText('诊断报告已导出。')
        except (ValueError, OSError) as error:
            self.feedback.setText('诊断导出未完成：' + str(error))
        self.feedback.show()

    def show_budgets(self):
        page = self._page('comparison')
        body = page.global_calculation_settings
        if body.parentWidget() is not self.advanced.content:
            self.advanced.content_layout.addWidget(body)
            body.show()

    def show_settings(self):
        if self._stopped:
            return
        if self.stack.currentWidget() is self.settings_page:
            self.navigate(getattr(self, '_business_route', 'inventory'))
        else:
            if self._match_inventory_session is not None:
                self.cancel_inventory_selection()
            self._populate_settings()  # Reuse controls; opening settings never checks the network.
            self.stack.setCurrentWidget(self.settings_page)
            self.navigation.hide()
            self.settings_button.setText('返回')
