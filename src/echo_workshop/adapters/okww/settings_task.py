# 保留宿主配置身份并管理插件运行时生命周期。
import sqlite3
import threading
from ok import BaseTask
from echo_workshop.application.templates import (
    DEFAULT_TEMPLATE,
    template_names,
)
from echo_workshop.application.runtime import PluginRuntime
from .lifecycle import install_unload_cleanup
from .assistant_ui import SettingsUIBridge
from .capture_commands import CaptureCommands


class EchoWorkshopSettingsTask(SettingsUIBridge, CaptureCommands, BaseTask):
    def on_main_window(self, window):
        """Host hook: attach the embedded page before the first window show."""
        from .assistant_tab import attach_echo_assistant

        return attach_echo_assistant(window)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = '声骸工坊'
        from echo_workshop.ui.widgets.workshop_icon import WorkshopIcon

        self.group_icon = WorkshopIcon()
        self.description = '看评分、收录声骸、比较和配装'
        self._echo_owned_cleanup = True
        self.runtime = None
        self.startup_error = ''
        self.control_repair_window = None
        self._loading_echo_config = False
        self._destroyed = False
        self._destroy_lock = threading.RLock()
        self._entry_cards = []
        self.inventory_window = None
        self.assistant_window = None
        self.embedded_panel = None
        self.comparison_window = None
        self.weight_window = None
        self.allocation_window = None
        self.shortcut_window = None
        self.shortcut_message = ''
        self.shortcut_message_at = float('-inf')
        self._window_signals = None
        self.scan_progress_overlay = None
        self.scan_start_notice = None
        self.default_config.update(
            {
                '启用声骸评分': True,
                '自动匹配评分模板': False,
                '角色评分模板': DEFAULT_TEMPLATE,
                'Show Debug Boxes': False,
                'InventoryProfileID': '',
                'TemplateCharacterID': '',
                'TemplateModalID': '',
                'ComparisonSettingsJSON': '',
                'AutoWeightUpdates': True,
                'AllocationSettingsJSON': '',
                'CaptureHotkeysJSON': '',
                'ShowCurrentComparisons': False,
                'IntakeMode': 'manual',
                'ScanMinimumLevel': '25',
            }
        )
        self.config_type.update(
            {
                '启用声骸评分': {'hidden': True},
                '角色评分模板': {'type': 'drop_down', 'options': template_names()},
                # Imported packages are a user-facing distribution. OCR boxes are
                # a development diagnostic and must not appear in this UI.
                'Show Debug Boxes': {'hidden': True},
                'InventoryProfileID': {'hidden': True},
                'TemplateCharacterID': {'hidden': True},
                'TemplateModalID': {'hidden': True},
                'ComparisonSettingsJSON': {'hidden': True},
                'AutoWeightUpdates': {'hidden': True},
                'AllocationSettingsJSON': {'hidden': True},
                'CaptureHotkeysJSON': {'hidden': True},
                '自动匹配评分模板': {'hidden': True},
                'ShowCurrentComparisons': {'hidden': True},
                'IntakeMode': {'hidden': True},
                'ScanMinimumLevel': {'hidden': True},
            }
        )
        self.config_description.update(
            {
                '启用声骸评分': '在游戏声骸详情中显示评分',
                '自动匹配评分模板': '按当前声骸的装备角色选择评分模版',
                '角色评分模板': '选择用于评分的角色与模版',
                'Show Debug Boxes': '显示 OK Script 的 OCR 调试框',
                'ShowCurrentComparisons': '同时查看匹配页所选角色的评分，最多三个',
            }
        )

    def load_config(self):
        # Hydration must retain a bounded persisted display label until the
        # validated per-workspace weight cache exists. Manual editing still
        # uses the ordinary active-options validator below.
        self._loading_echo_config = True
        try:
            return super().load_config()
        finally:
            self._loading_echo_config = False

    def on_create(self):
        # Imported tasks do receive on_create, but this host's imported-task
        # loader does not call post_init. Do not put required setup there.
        if self.runtime is not None and not self.runtime._closed:
            return
        self._destroyed = False
        self.startup_error = ''
        try:
            self.runtime = PluginRuntime(self.config.config_file)
        except (ValueError, sqlite3.Error) as error:
            from echo_workshop.storage.control_repair import ControlRepair

            issue = ControlRepair(self.config.config_file).selection_issue()
            if issue is None:
                raise  # Unrelated failures remain visible, not a silent fallback.
            self.runtime = None
            self.startup_error = '工作区控制记录待修复；未创建替代库存：' + str(error)
            install_unload_cleanup()
            return
        from echo_workshop.storage.workspace_bundle import PREFERENCE_KEYS

        self.runtime.backups.settings_reader = lambda: {
            key: self.config.get(key, self.default_config[key]) for key in PREFERENCE_KEYS
        }
        profiles = self.runtime.repository.profiles()
        if self.runtime.workspace_profile in profiles:
            self.config['InventoryProfileID'] = self.runtime.workspace_profile
        if not profiles:
            profile_id = self.runtime.repository.create_profile('默认档案')
            self.config['InventoryProfileID'] = profile_id
        elif self.config.get('InventoryProfileID') not in profiles:
            self.config['InventoryProfileID'] = next(iter(profiles))
        if not self.config.get('TemplateCharacterID') and not self.config.get('TemplateModalID'):
            try:
                char_id, modal_id = self.runtime.weights.active.resolve_selection(
                    self.config.get('角色评分模板')
                )
            except ValueError:
                pass  # Keep unresolved text visible and require an explicit choice.
            else:
                self.config['TemplateCharacterID'] = char_id
                self.config['TemplateModalID'] = modal_id
        install_unload_cleanup()
        self.runtime.updates.start(auto_enabled=self.config.get('AutoWeightUpdates', True))
        self.runtime.sonata_updates.start(auto_enabled=self.config.get('AutoWeightUpdates', True))
        self.runtime.sonata_effect_updates.start(
            auto_enabled=self.config.get('AutoWeightUpdates', True)
        )

    def on_destroy(self):
        with self._destroy_lock:
            self._destroy_once()

    def _destroy_once(self):
        if self._destroyed:
            return
        self._destroyed = True
        self._embedded_activate = None
        if self.runtime is not None:
            # Broadcast to every shared token before waiting for any one window.
            self.runtime.stop_event.set()
            self.runtime.capture.stop()
        if self.embedded_panel is not None:
            self.embedded_panel.stop_worker()
        if self.inventory_window is not None:
            self.inventory_window.stop_worker()
        if self.assistant_window is not None:
            self.assistant_window.stop_worker()
        if self.comparison_window is not None:
            self.comparison_window.stop_worker()
        if self.allocation_window is not None:
            self.allocation_window.stop_worker()
        if self.control_repair_window is not None:
            # Qt close remains queued on its owning thread, but repair I/O must
            # finish before unload returns, including runtime=None and deleted
            # card/signal owners. This method performs no Qt calls.
            self.control_repair_window.stop_worker()
        if self._window_signals is not None:
            try:
                self._window_signals.shutdown.emit()
                self._window_signals.deleteLater()
            except RuntimeError:
                pass  # The parent card was already deleted by the host.
            self._window_signals = None
        if self.runtime is not None:
            self.runtime.close()

    def validate_config(self, key, value):
        if key == '角色评分模板' and self._loading_echo_config:
            return (
                None
                if isinstance(value, str) and len(value) <= 1024 * 1024
                else '已保存模版名称类型/大小错误'
            )
        if key == '角色评分模板' and self.runtime is not None and not self.runtime._closed:
            return (
                None
                if value in self.runtime.weights.active.options
                else '请选择活动权重列表中的评分模版'
            )
        if key == '角色评分模板' and value not in template_names():
            return '请选择列表中的评分模版'
        return None

    def run(self):
        # Kept for compatibility with the host's imported-task registry. The
        # card exposes a persistent switch, so users never need to run it.
        return
