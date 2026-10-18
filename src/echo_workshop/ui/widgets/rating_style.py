# 生成统一评级颜色和数值展示。
"""Plugin presentation colours and frozen rating text; no grading mathematics."""

import re

LIGHT = {
    'C': '#62676f',
    'B': '#08784d',
    'A': '#1769ae',
    'S': '#7950b5',
    'SS': '#aa6700',
    'SSS': '#b53943',
}
DARK = {
    'C': '#b6bac2',
    'B': '#5dc996',
    'A': '#71bbff',
    'S': '#c4a2ff',
    'SS': '#e6bb64',
    'SSS': '#ff8e92',
}
COLOUR_NOTE = '颜色为本插件展示配色；评级下限来自当次模版。'


def rating_colour(rating, dark=False):
    label = getattr(rating, 'label', rating)
    return (DARK if dark else LIGHT).get(label, '#b7b7b7' if dark else '#656565')


def score_text(points, rating=None, prefix=''):
    return prefix + f'{points:.2f}' + (f' · {rating.label}' if rating is not None else '')


def rating_tooltip(rating, version=''):
    if rating is None:
        return ''
    return (
        rating.tooltip
        + '\n来源：'
        + getattr(rating, 'source', '当次模版')
        + '\n'
        + COLOUR_NOTE
        + ('\n权重版本：' + version if version else '')
    )


def summary_colour(text):
    """Native overlay is always rendered against the game, using dark palette."""
    match = re.search(r' · (SSS|SS|S|A|B|C)(?:\s|$)', text)
    return rating_colour(match[1], True) if match else None
