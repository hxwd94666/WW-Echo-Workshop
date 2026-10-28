# 缓存声骸工坊兽面标志，与项目主页共用同一张透明图。
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from qfluentwidgets import FluentIconBase, Theme, isDarkTheme


@lru_cache(maxsize=2)
def _workshop_icon(dark):
    image = QImage(str(Path(__file__).with_name('assets') / 'workshop.png'))
    if image.isNull():
        raise ValueError('声骸工坊图标资源缺失或损坏')
    if not dark:
        # 浅色侧栏反转明暗而不改透明度，保留兽面的眼部与轮廓对比。
        image.invertPixels(QImage.InvertRgb)
    return QIcon(QPixmap.fromImage(image))


class WorkshopIcon(FluentIconBase):
    """One shared mask, two cached theme palettes; no per-frame file reads."""

    def icon(self, theme=Theme.AUTO, color=None):
        dark = isDarkTheme() if theme == Theme.AUTO else theme == Theme.DARK
        # 品牌图保留眼部与面部的明暗差，不将两者染成同一主题色。
        return QIcon(_workshop_icon(dark))

    def render(self, painter, rect, theme=Theme.AUTO, indexes=None, **attributes):
        # 侧栏默认槽位为16像素，居中扩至20像素以接近相邻栏目图标。
        bounds = QRectF(rect)
        if 0 < min(bounds.width(), bounds.height()) <= 20:
            extra = min(bounds.width(), bounds.height()) / 8
            bounds.adjust(-extra, -extra, extra, extra)
        self.icon(theme, attributes.get('fill')).paint(painter, bounds.toRect(), Qt.AlignCenter)
