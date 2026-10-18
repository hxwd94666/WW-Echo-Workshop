# 有界缓存完整卡片栅格，状态、主题、字号、尺寸或头像变化时自动失效。
from collections import OrderedDict
from math import ceil

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QPainter, QPixmap
from qfluentwidgets import isDarkTheme, themeColor
from echo_workshop.ui.inventory.card_data import CardData

_CARDS = OrderedDict()
_SEEN = OrderedDict()
_BYTES = 0
_LIMIT = 24 * 1024 * 1024
_STATS = {'hits': 0, 'renders': 0}


def draw_cached_card(painter, rect, data, font, selected, footer, avatar, show_owner, render):
    """Only pixels are cached: evidence, selection and scores stay immutable."""
    global _BYTES
    ratio = painter.device().devicePixelRatioF()
    width, height = ceil(rect.width() * ratio), ceil(rect.height() * ratio)
    cost = width * height * 4
    if width <= 0 or height <= 0:
        return
    if not isinstance(data, CardData):
        render(painter, rect)
        return
    key = (
        id(data),
        rect.width(),
        rect.height(),
        QFont(font),  # A frozen copy keeps kerning/hinting and cannot mutate with the caller.
        selected,
        footer,
        avatar.cacheKey() if show_owner and avatar is not None else None,
        show_owner,
        isDarkTheme(),
        themeColor().rgba(),
        ratio,
    )
    cached = _CARDS.get(key)
    if cached is not None:
        _CARDS.move_to_end(key)
        _STATS['hits'] += 1
        painter.drawPixmap(rect.topLeft(), cached[1])
        return
    # A first-time scroll past an item must not pay for an offscreen raster.
    # Admit recurring cells only; keep the admission metadata bounded as well.
    if key not in _SEEN:
        _SEEN[key] = data, 1  # Retain identity; no recycled-id alias.
        if len(_SEEN) > 1024:
            _SEEN.popitem(last=False)
        render(painter, rect)
        return
    _SEEN.move_to_end(key)
    if _SEEN[key][1] < 3:
        _SEEN[key] = data, _SEEN[key][1] + 1
        render(painter, rect)
        return
    if cost > _LIMIT:
        render(painter, rect)
        return
    pixmap = QPixmap(width, height)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    target = QPainter(pixmap)
    try:
        render(target, QRect(0, 0, rect.width(), rect.height()))
    finally:
        target.end()
    _STATS['renders'] += 1
    _CARDS[key] = data, pixmap
    _BYTES += cost
    while len(_CARDS) > 128 or _BYTES > _LIMIT:
        _, (_, previous) = _CARDS.popitem(last=False)
        _BYTES -= previous.width() * previous.height() * 4
    painter.drawPixmap(rect.topLeft(), pixmap)
