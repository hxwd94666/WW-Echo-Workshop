# 以独立窗口承载相同的allocation页面。
from echo_workshop.ui.window import ModeWindow


class AllocationWindow(ModeWindow):
    mode = 'allocation'
