# 装配工坊设置分组与明确应用的评分和诊断选项。
from PySide6.QtWidgets import QCheckBox, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CardWidget, PushButton


class _SettingsSection(CardWidget):
    """Always-visible section; the whole settings page owns the only scrollbar."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        heading = BodyLabel(title, self)
        font = heading.font()
        font.setBold(True)
        font.setPointSize(11)
        heading.setFont(font)
        layout.addWidget(heading)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        layout.addWidget(self.content)


class AssistantSettingsController:
    def _build_settings(self):
        from echo_workshop.ui.settings.groups import GroupedSettingsSurface

        area = GroupedSettingsSurface(self)
        body = area.body
        self.settings_sections = {}
        for key, title, group in (
            ('score', '实时评分', 'common'),
            ('shortcuts', '收录与快捷键', 'intake'),
            ('weights', '自定义评分权重', 'scoring'),
            ('search', '匹配与配装计算', 'scoring'),
            ('profiles', '档案管理', 'data'),
            ('screenshots', '声骸截图', 'data'),
            ('data', '资料更新', 'data'),
            ('plugin', '诊断与插件', 'advanced'),
        ):
            section = _SettingsSection(title, body)
            self.settings_sections[key] = section
            area.add_section(key, section, group)
        inventory = self.proxy.inventory_window
        # This is the same authoritative header control, not a second setting.
        self.settings_scan_level = inventory.scan_level
        intake = self.settings_sections['shortcuts']
        intake.setToolTip('自动扫描等级在顶部设置；扫描前将背包拉到顶部并切回游戏。')
        score = self.settings_sections['score']
        self.live_score_checks = {}
        for key, label, default in (
            ('启用声骸评分', '显示实时评分', True),
            ('自动匹配评分模板', '跟随当前装备角色评分', False),
        ):
            check = QCheckBox(label)
            check.setChecked(bool(self.task.config.get(key, default)))
            check.toggled.connect(lambda value, k=key: self.task.config.__setitem__(k, bool(value)))
            score.content_layout.addWidget(check)
            self.live_score_checks[key] = check
        from echo_workshop.ui.widgets.roles import RolePicker

        self.score_picker = RolePicker(self.runtime.weights.active)
        self.score_picker.reload(
            self.runtime.weights.active,
            (self.task.config.get('TemplateCharacterID'), self.task.config.get('TemplateModalID')),
        )
        score.content_layout.addWidget(self.score_picker)
        apply = PushButton('使用此角色评分')
        apply.setToolTip('选择角色和模版后点击使用。评分衡量词条质量，不代表伤害增幅。')
        apply.clicked.connect(self.apply_score_selection)
        self.score_apply_button = apply
        score.content_layout.addWidget(apply)
        self.score_source_hint = BodyLabel('')
        self.score_source_hint.setWordWrap(True)
        score.content_layout.addWidget(self.score_source_hint)
        self.advanced = self.settings_sections[
            'search'
        ]  # Existing public owner of global controls.
        # Legacy transfer controllers stay compatible but are not duplicate
        # daily actions. All profile CRUD/portable transfer is in one card.
        self.build_profile_management(self.settings_sections['profiles'])
        from echo_workshop.ui.settings.screenshots import ScreenshotSettings

        screenshots = self.settings_sections['screenshots']
        screenshots.content_layout.addWidget(ScreenshotSettings(self.runtime, screenshots.content))
        self.plugin_management = self.settings_sections['plugin']
        self.export_diagnostics_button = PushButton('导出诊断报告')
        self.export_diagnostics_button.setToolTip('仅运行摘要，不含声骸库、截图、OCR或账号。')
        self.export_diagnostics_button.clicked.connect(self.export_diagnostics)
        self.plugin_management.content_layout.addWidget(self.export_diagnostics_button)
        self.uninstall_button = PushButton('移除插件')
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

    def _ensure_weights(self):
        if not hasattr(self, 'weights'):
            from echo_workshop.ui.settings.page import WeightSettings

            self.weights = WeightSettings(self.proxy, self.settings_sections['weights'].content)
            self.settings_sections['weights'].content_layout.addWidget(self.weights)
            self.settings_sections['data'].content_layout.insertWidget(
                0, self.weights.resource_controls
            )
            self.weights.advanced_controls.hide()
        return self.weights

    def _populate_settings(self, group=None):
        group = group or self.settings_page.current_group
        if group == 'intake' and not hasattr(self, 'shortcuts'):
            from echo_workshop.ui.settings.page import ShortcutSettings

            section = self.settings_sections['shortcuts']
            self.shortcuts = ShortcutSettings(self.proxy, section.content)
            section.content_layout.addWidget(self.shortcuts)
        if group in ('scoring', 'data'):
            weights = self._ensure_weights()
            if group == 'scoring':
                weights.ensure_custom_editor()
                self.show_budgets()
        self._settings_ready = True

    def _ensure_repair(self):
        # A repair service is still needed for existing damaged selectors or
        # interrupted startup. It is not a duplicate everyday backup action.
        if hasattr(self, 'repair'):
            return
        from echo_workshop.ui.settings.page import RepairSettings

        section = _SettingsSection('启动配置修复', self.settings_page.body)
        self.settings_sections['repair'] = section
        self.settings_page.add_section('repair', section, 'advanced')
        self.repair = RepairSettings(self.proxy, section.content)
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
        from echo_workshop.application.diagnostics import export_diagnostics

        path, _ = QFileDialog.getSaveFileName(
            self, '导出运行诊断（不含声骸库和截图）', 'echo-diagnostics.json', 'JSON (*.json)'
        )
        if not path:
            return
        try:
            from echo_workshop.resources.paths import package_manifest

            manifest = package_manifest()
            export_diagnostics(
                path,
                context={
                    'plugin_version': manifest.get('version', ''),
                    'auto_weight_updates': bool(self.task.config.get('AutoWeightUpdates', True)),
                },
            )
            self.feedback.setText('诊断报告已导出。')
        except (ValueError, OSError) as error:
            self.feedback.setText('诊断导出未完成：' + str(error))
        self.feedback.show()

    def apply_score_selection(self):
        key = self.score_picker.key()
        if key is None:
            self.feedback.setText('请选择角色和模版。')
            self.feedback.show()
            return
        try:
            template = self.runtime.weights.active.template(*key)
        except (ValueError, KeyError):
            self.feedback.setText('权重已更新，请重新选择角色和模版。')
            self.feedback.show()
            return
        self.task.config['TemplateCharacterID'], self.task.config['TemplateModalID'] = key
        self.task.config['角色评分模板'] = template['name']
        self.feedback.setText('评分角色已更新。')
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
