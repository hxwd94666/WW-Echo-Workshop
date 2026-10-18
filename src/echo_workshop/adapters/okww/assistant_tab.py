# 将声骸工坊接入宿主原生脚本页并同步导航名称与专用图标。
"""Bind the workshop from the plugin task; never register another tab."""

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QWidget
from qfluentwidgets import MessageBox
from ok import Logger, og
from ok.core.events import communicate


def attach_echo_assistant(window):
    """Called by the plugin task on_main_window, before the host's first show."""
    bridge = getattr(window, '_echo_assistant_bridge', None)
    if bridge is None:
        bridge = EchoAssistantTab(window)
        window._echo_assistant_bridge = bridge
    bridge.bind_plugin()
    return bridge


class EchoAssistantTab(QObject):
    """Non-visual controller. The only page/route belongs to imported_tabs."""

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName('EchoAssistantController')
        self.executor = window.executor
        self.logger = Logger.get_logger(self.__class__.__name__)
        self._window = window
        self._task = self._page = self._host_tab = None
        self._closed = self._binding = False
        self._legacy_visibility = []
        self.status = self.error = ''
        window.installEventFilter(self)
        # The native import subscriber was registered by MainWindow already.
        communicate.task_list_updated.connect(self.bind_plugin)
        callback = self.bind_plugin
        self.destroyed.connect(lambda *_: communicate.task_list_updated.disconnect(callback))

    @property
    def name(self):
        return '声骸工坊'

    def window(self):
        return self._window

    def eventFilter(self, watched, event):
        if watched is self._window:
            if event.type() == QEvent.Show:
                self.bind_plugin()
            elif event.type() == QEvent.Close and not getattr(watched, 'do_not_quit', False):
                self.close()
        return super().eventFilter(watched, event)

    def bind_plugin(self):
        if self._closed or self._binding or self.executor is None:
            return
        self._binding = True
        try:
            self._bind_plugin_page()
            self._sync_navigation()
        finally:
            self._binding = False

    def _bind_plugin_page(self):
        from shiboken6 import isValid

        legacy = self._window.imported_tabs.get('echo-workshop')
        manager = getattr(og, 'task_manager', None)
        entry = getattr(manager, 'imported_scripts', {}).get('echo-workshop', {})
        tasks = [
            t
            for t in entry.get('tasks', ())
            if type(t).__name__ == 'EchoWorkshopSettingsTask'
            and not getattr(t, '_destroyed', False)
        ]
        task = tasks[0] if len(tasks) == 1 else None
        if task is self._task and self._page is not None and self._host_tab is legacy:
            return
        self._detach()
        self.error = ''
        if task is None or legacy is None or not isValid(legacy):
            self.status = '尚未加载声骸评分包；请从脚本导入安装。'
            return
        factory = getattr(task, 'create_import_page', None)
        if not callable(factory):
            self.status = '当前评分包使用弹窗界面；更新评分包后启用内嵌页。'
            return
        try:
            page = factory(legacy.view)
            if not isinstance(page, QWidget) or page.isWindow():
                raise TypeError('页面工厂未返回内嵌 QWidget')
        except Exception as error:
            self.status = '声骸工坊加载失败；原脚本入口仍可使用。'
            self.error = str(error)
            self.logger.warning('Echo assistant page factory failed: ' + str(error))
            return
        self._task, self._page = task, page
        self.status = ''
        task._embedded_activate = self.activate_page
        uninstall = getattr(page, 'uninstall_button', None)
        if uninstall is not None:
            uninstall.clicked.connect(self.uninstall_plugin)
            uninstall.setEnabled(True)

    def _sync_navigation(self):
        """Replace only native page contents/label; no add/removeInterface calls."""
        from shiboken6 import isValid

        if self._closed:
            return
        window = self._window
        legacy = window.imported_tabs.get('echo-workshop')
        if legacy is None or not isValid(legacy):
            self._restore_native_widgets()
            return
        timer = getattr(legacy, 'timer', None)
        if isinstance(timer, QTimer) and isValid(timer) and timer.parent() is None:
            timer.setParent(legacy)
        if not getattr(legacy, '_echo_lifecycle_bound', False):
            legacy._echo_lifecycle_bound = True
            callback = legacy.refresh_ui
            legacy.destroyed.connect(lambda *_: communicate.task_list_updated.disconnect(callback))
        live = (
            self._page is not None
            and isValid(self._page)
            and not getattr(self._page, '_stopped', False)
        )
        entry = getattr(og.task_manager, 'imported_scripts', {}).get('echo-workshop', {})
        label = self.name if live else entry.get('script_name', '声骸评分')
        navigation = window.navigationInterface.widget(legacy.objectName())
        navigation.setText(label)
        if live and getattr(self._task, 'group_icon', None) is not None:
            navigation.setIcon(self._task.group_icon)
        if live:
            if self._host_tab is not legacy:
                self._restore_native_widgets()
                self._host_tab = legacy
                self._legacy_visibility = [
                    (widget, widget.isHidden())
                    for widget in (
                        legacy.task_cards_view,
                        legacy.task_info_container,
                        legacy.button_container,
                    )
                ]
            for widget, _ in self._legacy_visibility:
                widget.hide()
            if legacy.vBoxLayout.indexOf(self._page) < 0:
                if self._page.parentWidget() is not legacy.view:
                    self._page.setParent(legacy.view)
                legacy.vBoxLayout.addWidget(self._page, 1)
            self._page.show()
        else:
            self._restore_native_widgets()
        window.update_navigation_width()

    def _restore_native_widgets(self):
        from shiboken6 import isValid

        for widget, hidden in self._legacy_visibility:
            if isValid(widget):
                widget.setVisible(not hidden)
        self._legacy_visibility = []
        self._host_tab = None

    def uninstall_plugin(self):
        manager = getattr(og, 'task_manager', None)
        if manager is None or 'echo-workshop' not in manager.imported_scripts:
            return
        confirm = MessageBox(
            '移除声骸工坊', '移除插件？已收录的声骸、截图和档案会保留。', self._window
        )
        if confirm.exec():
            manager.delete_imported_script('echo-workshop')

    def activate_page(self):
        if self._host_tab is not None:
            self._window.switchTo(self._host_tab)

    def _detach(self):
        from shiboken6 import isValid

        if self._task is not None:
            self._task._embedded_activate = None
        if self._page is not None and isValid(self._page):
            parent = self._page.parentWidget()
            if parent is not None and parent.layout() is not None:
                parent.layout().removeWidget(self._page)
            self._page.close()
            self._page.deleteLater()
        self._restore_native_widgets()
        self._task = self._page = None

    def close(self):
        if not self._closed:
            self._closed = True
            communicate.task_list_updated.disconnect(self.bind_plugin)
            self._detach()
