# 以独立窗口承载相同的inventory页面。
from echo_workshop.ui.window import ModeWindow


class InventoryWindow(ModeWindow):
    mode = 'inventory'
