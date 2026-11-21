# 在宿主缺少内嵌页能力时复用同一工坊页面。
from PySide6.QtWidgets import QDialog, QVBoxLayout
from PySide6.QtCore import Qt
from ww_echo_workshop.ui.shell import EchoAssistantPanel
from ww_echo_workshop.ui.widgets.layout import fit_dialog


class ModeWindow(QDialog):
    mode = 'inventory'

    def __init__(self, services, parent=None, *, on_close):
        super().__init__(parent)
        self._on_close = on_close
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle('声骸工坊')
        self.panel = EchoAssistantPanel(services, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.panel)
        self.page = self.panel._activate(self.mode)
        fit_dialog(self, 1180, 820)

    def __getattr__(self, name):
        page = self.__dict__.get('page')
        if page is not None:
            return getattr(page, name)
        raise AttributeError(name)

    def stop_worker(self):
        self.panel.stop_worker()

    def closeEvent(self, event):
        self.panel.close()
        self._on_close(self)
        super().closeEvent(event)


class AssistantWindow(ModeWindow):
    mode = 'workspace'
