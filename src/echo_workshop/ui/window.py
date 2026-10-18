# 在宿主缺少内嵌页能力时复用同一工坊页面。
from PySide6.QtWidgets import QDialog, QVBoxLayout
from PySide6.QtCore import Qt
from echo_workshop.ui.shell import EchoAssistantPanel
from echo_workshop.ui.widgets.layout import fit_dialog


class ModeWindow(QDialog):
    mode = 'inventory'
    owner_attribute = None

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self._task = task
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle('声骸工坊')
        self.panel = EchoAssistantPanel(task, self)
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
        attribute = self.owner_attribute or self.mode + '_window'
        if getattr(self._task, attribute, None) is self:
            setattr(self._task, attribute, None)
        super().closeEvent(event)


class AssistantWindow(ModeWindow):
    owner_attribute = 'assistant_window'
