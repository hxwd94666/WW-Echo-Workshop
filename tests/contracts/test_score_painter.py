# 验证实时评分绘制器仍输出每个词条的单独分数。
from types import SimpleNamespace
import unittest

from echo_workshop.adapters.okww.score_painter import EchoStatBoxPainter


class Canvas:
    def __init__(self):
        self.texts = []
        self.ratio = 1.0

    def rectangle(self, *args, **kwargs):
        pass

    def text(self, x, y, text, *args, **kwargs):
        self.texts.append(text)


class ScorePainterRows(unittest.TestCase):
    def test_nonzero_row_scores_are_painted_after_each_rectangle(self):
        rectangles = tuple(
            SimpleNamespace(x=10, y=10 + index * 20, width=100, height=18, color=(255, 0, 0))
            for index in range(3)
        )
        painter = EchoStatBoxPainter()
        painter.update(rectangles, 'summary', (), (), (4.88, 0.0, 2.5))
        canvas = Canvas()
        painter.paint(canvas, SimpleNamespace(_frame_width=0, _frame_height=0))
        self.assertEqual(['+4.88', '+2.50'], canvas.texts)
