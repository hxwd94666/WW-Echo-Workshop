# 让已保存配装按最多三列展示完整声骸卡片。
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QAbstractItemView

from echo_workshop.ui.inventory.cards import EchoCardGrid


class SavedLoadoutGrid(EchoCardGrid):
    """Three fluid columns for saved roles; narrow views keep readable cards."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setSelectionMode(QAbstractItemView.MultiSelection)

    def _resize_grid(self):
        available = max(1, self.viewport().width() - 2)
        self.columns = min(3, max(1, available // 220))
        width = max(1, available // self.columns)
        line = self.fontMetrics().height()
        height = 30 + 9 * (line + 8) + 10
        if self.show_score_footer:
            height += line + 14
        self.setGridSize(QSize(width, height))
