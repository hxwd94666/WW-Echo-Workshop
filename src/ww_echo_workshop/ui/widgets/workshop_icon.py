# 缓存声骸工坊兽面标志，与项目主页共用同一张透明图。
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QPixmap
from ww_echo_workshop.ui.widgets.controls import is_dark_theme


_ICON_PATH = str(Path(__file__).with_name('assets') / 'workshop.png')


@lru_cache(maxsize=2)
def _workshop_icon(dark):
    image = QImage(_ICON_PATH)
    if image.isNull():
        raise ValueError('声骸工坊图标资源缺失或损坏')
    if not dark:
        # 浅色侧栏反转明暗而不改透明度，保留兽面的眼部与轮廓对比。
        image.invertPixels(QImage.InvertRgb)
    return QIcon(QPixmap.fromImage(image))


def workshop_icon():
    if not isinstance(QCoreApplication.instance(), QGuiApplication):
        return QIcon()
    return QIcon(_workshop_icon(is_dark_theme()))
