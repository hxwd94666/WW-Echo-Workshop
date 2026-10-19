# 挂载助手页面并兼容宿主任务卡入口。
import weakref
from PySide6.QtCore import Qt, QTimer, QStringListModel
from qfluentwidgets import SwitchButton, PushButton
from echo_workshop.ui.widgets.inputs import EditableComboBox, PinyinCompleter as QCompleter
from echo_workshop.resources.search import search_matches
from echo_workshop.application.templates import matching_template_names
from .lifecycle import _WindowSignals, _window_lifetime_guard


class _TemplateSearchComboBox(EditableComboBox):
    def _onReturnPressed(self):
        exact_index = self.findText(self.text())
        if exact_index >= 0:
            self.setCurrentIndex(exact_index)
            return
        labels = getattr(self, '_active_labels', None)
        matches = (
            [name for name in labels if search_matches(name, self.text())]
            if labels is not None
            else matching_template_names(self.text())
        )
        if len(matches) == 1:
            self.setCurrentIndex(self.findText(matches[0]))


class SettingsUIBridge:
    @_window_lifetime_guard
    def create_import_page(self, parent):
        """Local B bridge factory, not a claim that ok-script auto-discovers it."""
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
        from shiboken6 import isValid

        if QThread.currentThread() != QApplication.instance().thread():
            raise RuntimeError('插件页面应在 GUI 线程创建')
        if self.runtime is None or self.runtime._closed:
            raise ValueError(self.startup_error or '插件尚未初始化')
        self._ensure_signals(None)
        self._configure_shortcuts(self.config.get('CaptureHotkeysJSON', ''))
        if (
            self.embedded_panel is None
            or not isValid(self.embedded_panel)
            or self.embedded_panel._stopped
        ):
            from echo_workshop.ui.shell import EchoAssistantPanel

            self.embedded_panel = EchoAssistantPanel(self, parent)
        for reference in tuple(self._entry_cards):
            card = reference()
            if card is not None and isValid(card):
                self._sync_card_entry(card)
        return self.embedded_panel

    def _route_embedded(self, name):
        from shiboken6 import isValid

        panel = self.embedded_panel
        if panel is not None and isValid(panel) and not panel._stopped:
            panel.navigate(name)
            activate = getattr(self, '_embedded_activate', None)
            if activate is not None:
                activate()
            return True
        return False

    def _route_settings(self, section):
        from shiboken6 import isValid

        panel = self.embedded_panel
        if panel is not None and isValid(panel) and not panel._stopped:
            panel.open_settings_section(section)
            activate = getattr(self, '_embedded_activate', None)
            if activate is not None:
                activate()
            return True
        return False

    @_window_lifetime_guard
    def open_assistant(self, card=None):
        if self._route_embedded('inventory'):
            return
        from echo_workshop.ui.window import AssistantWindow

        if self._destroyed or self.runtime is None or self.runtime._closed:
            return
        self._ensure_signals(card)
        if self.assistant_window is None:
            self.assistant_window = AssistantWindow(self, card)
        self.assistant_window.show()
        self.assistant_window.raise_()
        self.assistant_window.activateWindow()

    @_window_lifetime_guard
    def open_inventory(self, card):
        if self._route_embedded('inventory'):
            return
        from echo_workshop.ui.inventory.window import InventoryWindow

        if self._destroyed or self.runtime is None or self.runtime._closed:
            return
        self._ensure_signals(card)
        if self.inventory_window is None:
            self.inventory_window = InventoryWindow(self, card)
            from echo_workshop.ui.widgets.layout import add_home_button

            add_home_button(self.inventory_window, self)
        self.inventory_window.show()
        self.inventory_window.raise_()
        self.inventory_window.activateWindow()

    def _close_inventory(self):
        if self.scan_start_notice is not None:
            from shiboken6 import isValid

            if isValid(self.scan_start_notice):
                self.scan_start_notice.close()
            self.scan_start_notice = None
        if self.scan_progress_overlay is not None:
            self.scan_progress_overlay.close()
        if self.manual_progress_overlay is not None:
            self.manual_progress_overlay.close()
        if self.embedded_panel is not None:
            from shiboken6 import isValid

            if isValid(self.embedded_panel):
                self.embedded_panel.close()
        if self.assistant_window is not None:
            self.assistant_window.close()
        if self.inventory_window is not None:
            self.inventory_window.close()
        if self.comparison_window is not None:
            self.comparison_window.close()
        if self.weight_window is not None:
            self.weight_window.close()
        if self.allocation_window is not None:
            self.allocation_window.close()
        if self.shortcut_window is not None:
            self.shortcut_window.close()
        if self.control_repair_window is not None:
            self.control_repair_window.close()

    @_window_lifetime_guard
    def open_control_repair(self, card):
        if self._route_settings('repair'):
            return
        if self._destroyed:
            return
        from echo_workshop.ui.settings.repair import ControlRepairWindow

        self._ensure_signals(card)
        if self.control_repair_window is None:
            self.control_repair_window = ControlRepairWindow(self, card)
        self.control_repair_window.show()
        self.control_repair_window.raise_()

    def _ensure_signals(self, card):
        from PySide6.QtCore import QCoreApplication
        from shiboken6 import isValid

        if self._window_signals is None or not isValid(self._window_signals):
            # TaskCard refresh deletes its widgets. The event bridge must outlive
            # those cards, otherwise detected keys target a deleted QObject.
            self._window_signals = _WindowSignals(QCoreApplication.instance())
            self._window_signals.shutdown.connect(self._close_inventory, Qt.QueuedConnection)
        if not getattr(self._window_signals, '_shortcut_connected', False):
            self._window_signals.shortcut.connect(self.handle_shortcut, Qt.QueuedConnection)
            self._window_signals._shortcut_connected = True

    @_window_lifetime_guard
    def open_shortcuts(self, card):
        if self._route_settings('shortcuts'):
            return
        from echo_workshop.ui.settings.shortcuts import ShortcutWindow

        if self._destroyed or self.runtime is None or self.runtime._closed:
            return
        self._ensure_signals(card)
        if self.shortcut_window is None:
            self.shortcut_window = ShortcutWindow(self, card)
        self.shortcut_window.show()
        self.shortcut_window.raise_()

    @_window_lifetime_guard
    def open_allocation(self, card):
        if self._route_embedded('allocation'):
            return
        from echo_workshop.ui.allocation.window import AllocationWindow

        if self._destroyed or self.runtime is None or self.runtime._closed:
            return
        self._ensure_signals(card)
        if self.allocation_window is None:
            self.allocation_window = AllocationWindow(self, card)
            from echo_workshop.ui.widgets.layout import add_home_button

            add_home_button(self.allocation_window, self)
        self.allocation_window.show()
        self.allocation_window.raise_()
        self.allocation_window.activateWindow()

    @_window_lifetime_guard
    def open_weights(self, card):
        if self._route_settings('weights'):
            return
        from echo_workshop.ui.settings.weight_editor import WeightWindow

        if self._destroyed or self.runtime is None or self.runtime._closed:
            return
        self._ensure_signals(card)
        if self.weight_window is None:
            self.weight_window = WeightWindow(self, card)
        self.weight_window.show()
        self.weight_window.raise_()
        self.weight_window.activateWindow()

    @_window_lifetime_guard
    def open_comparison(self, card):
        if self._route_embedded('comparison'):
            return
        from echo_workshop.ui.matching.window import ComparisonWindow

        if self._destroyed or self.runtime is None or self.runtime._closed:
            return
        self._ensure_signals(card)
        if self.comparison_window is None:
            self.comparison_window = ComparisonWindow(self, card)
            from echo_workshop.ui.widgets.layout import add_home_button

            add_home_button(self.comparison_window, self)
        self.comparison_window.show()
        self.comparison_window.raise_()
        self.comparison_window.activateWindow()

    def _install_inventory_button(self, card):
        if getattr(card, '_echo_inventory_button', None) is not None:
            return
        self._ensure_signals(card)
        repair = PushButton('故障恢复 / 控制记录修复', card)
        repair.clicked.connect(lambda: self.open_control_repair(card))
        card.viewLayout.addWidget(repair)
        card._echo_control_repair_button = repair
        if self.runtime is None or self.runtime._closed:
            repair.setText('工作区启动受阻：打开故障恢复')
            card._echo_inventory_button = repair
            return
        repair.hide()
        assistant = PushButton('打开声骸工坊', card)
        assistant.clicked.connect(lambda: self.open_assistant(card))
        card.viewLayout.addWidget(assistant)
        card._echo_assistant_button = assistant
        self._entry_cards.append(weakref.ref(card))
        try:
            self._configure_shortcuts(self.config.get('CaptureHotkeysJSON', ''))
        except (ValueError, OSError) as error:
            self.shortcut_message = '快捷键配置待修正：' + str(error)
        button = PushButton('声骸库 / 开始收录', card)
        button.clicked.connect(lambda: self.open_inventory(card))
        # Keep the compact header narrow: use the expanded content area.
        button.hide()  # Retain callable legacy entry, not a second main action.
        card._echo_inventory_button = button
        comparison = PushButton('多角色评分矩阵 / 找回', card)
        comparison.clicked.connect(lambda: self.open_comparison(card))
        comparison.hide()
        card._echo_comparison_button = comparison
        weights = PushButton('XWUID 权重更新', card)
        weights.clicked.connect(lambda: self.open_weights(card))
        weights.hide()
        card._echo_weight_button = weights
        allocation = PushButton('公平配装 / 换装清单', card)
        allocation.clicked.connect(lambda: self.open_allocation(card))
        allocation.hide()
        card._echo_allocation_button = allocation
        shortcuts = PushButton('收录快捷键 / 游戏内操作', card)
        shortcuts.clicked.connect(lambda: self.open_shortcuts(card))
        shortcuts.hide()
        card._echo_shortcut_button = shortcuts

    def _sync_card_entry(self, card):
        """B ready: just a header jump. Legacy hosts keep the existing controls."""
        from shiboken6 import isValid

        panel = self.embedded_panel
        compact = panel is not None and isValid(panel) and not panel._stopped
        if getattr(card, '_echo_compact_entry', False) == compact:
            return
        assistant = getattr(card, '_echo_assistant_button', None)
        if assistant is None:
            return
        card._echo_compact_entry = compact
        if compact:
            card._echo_legacy_buttons = tuple((b, b.isHidden()) for b in card.all_buttons)
            card._echo_legacy_description = card.card.contentLabel.text()
            for button, _ in card._echo_legacy_buttons:
                button.hide()
            card.viewLayout.removeWidget(assistant)
            assistant.setText('进入声骸工坊')
            assistant.show()
            card.all_buttons = [assistant]
            card._rebuild_button_layout()
            card.setExpand(False)
            card.card.expandButton.setEnabled(False)
            card.card.expandButton.hide()
            card.card.contentLabel.setText('在侧栏内使用声骸库、比较和配装')
        else:
            card.button_layout.removeWidget(assistant)
            card.viewLayout.addWidget(assistant)
            assistant.setText('打开声骸工坊')
            assistant.show()
            card.all_buttons = [b for b, _ in card._echo_legacy_buttons]
            for button, hidden in card._echo_legacy_buttons:
                button.setVisible(not hidden)
            card._rebuild_button_layout()
            card.card.expandButton.setEnabled(True)
            card.card.expandButton.show()
            card.card.contentLabel.setText(card._echo_legacy_description)
            card.setExpand(True)

    def _install_total_switch(self, card):
        if getattr(card, '_echo_total_switch', None) is not None:
            return
        for button in (card.start_button, card.pause_button, card.stop_button):
            if button is not None:
                button.hide()
        switch = SwitchButton(parent=card)
        switch.setOnText('已启用')
        switch.setOffText('已停用')
        switch.setChecked(bool(self.config.get('启用声骸评分', True)))
        switch.checkedChanged.connect(
            lambda checked: self.config.__setitem__('启用声骸评分', bool(checked))
        )
        # Stop TaskCard from restoring its one-shot Start button whenever any
        # other task changes state, and let its normal layout rebuild retain
        # our persistent switch.
        card.onetime = False
        card.all_buttons.append(switch)
        card._rebuild_button_layout()
        card._echo_total_switch = switch

    def _install_search_combo(self, card):
        widget = card.config_widget_by_key.get('角色评分模板')
        if widget is None or isinstance(widget.combo_box, _TemplateSearchComboBox):
            return
        old_combo = widget.combo_box
        combo = _TemplateSearchComboBox(widget)
        combo.addItems(widget.tr_options)
        combo.setFixedWidth(330)
        combo.setPlaceholderText('输入角色名搜索')
        selected = widget.config.get(widget.key)
        translated = next(
            (text for text, value in widget.tr_dict.items() if value == selected), selected
        )
        combo.setCurrentIndex(combo.findText(translated))
        completer = QCompleter(widget.tr_options, combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        combo.setCompleter(completer)
        combo.currentTextChanged.connect(
            lambda text: (
                self._select_template(widget, text)
                if self.runtime is not None
                and not self.runtime._closed
                and text in self.runtime.weights.active.options
                else None
            )
        )
        widget.layout.replaceWidget(old_combo, combo)
        old_combo.deleteLater()
        widget.combo_box = combo
        widget._template_completer = completer
        timer = QTimer(card)
        timer.setInterval(1000)
        timer.timeout.connect(lambda: self._refresh_template_options(card, widget))
        card._echo_template_timer = timer
        timer.start()
        self._refresh_template_options(card, widget)

    def _refresh_template_options(self, card, widget):
        self._sync_card_entry(card)
        if self.runtime is None or self.runtime._closed:
            card._echo_template_timer.stop()
            return
        snapshot = self.runtime.weights.active
        if getattr(card, '_echo_template_version', None) == snapshot.version:
            return
        combo = widget.combo_box
        labels = list(snapshot.options)
        selected_ids = (self.config.get('TemplateCharacterID'), self.config.get('TemplateModalID'))
        selected = next(
            (label for label, row in snapshot.options.items() if row[:2] == selected_ids), None
        )
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(labels)
        combo._active_labels = labels
        if selected is not None:
            self.config['角色评分模板'] = selected
            combo.setCurrentIndex(combo.findText(selected))
        else:
            combo.setCurrentIndex(-1)
            combo.setText('失效选择：' + self.config.get('角色评分模板', ''))
        combo.blockSignals(False)
        widget.tr_options = labels
        widget.tr_dict = dict(zip(labels, labels))
        widget._template_completer.setModel(QStringListModel(labels, widget._template_completer))
        self.config_type['角色评分模板']['options'] = labels
        card._echo_template_version = snapshot.version

    def _select_template(self, widget, label):
        if self.runtime is not None and not self.runtime._closed:
            widget.update_config(label)
            character, modal = self.runtime.weights.active.resolve_selection(label)
            self.config['TemplateCharacterID'] = character
            self.config['TemplateModalID'] = modal


def _patch_imported_task_card():
    """Customize the card when the host exposes it; never block plugin loading."""
    try:
        from ok.ui.qt.tasks.TaskCard import TaskCard
    except ImportError as error:
        return False, str(error)

    if getattr(TaskCard, '_echo_score_card_patch', False):
        return True, ''
    original_init = TaskCard.__init__

    def patched_init(card, task, onetime):
        original_init(card, task, onetime)
        if task.__class__.__name__ != 'EchoWorkshopSettingsTask':
            return
        try:
            task._install_total_switch(card)
            task._install_search_combo(card)
            task._install_inventory_button(card)
            task._sync_card_entry(card)
            if not getattr(card, '_echo_compact_entry', False):
                card.setExpand(True)
        except Exception as error:
            try:
                task.log_warning(f'宿主卡片适配未完成：{error}')
            except Exception:
                pass

    TaskCard.__init__ = patched_init
    TaskCard._echo_score_card_patch = True
    return True, ''


def ensure_task_card_patch():
    return _patch_imported_task_card()
