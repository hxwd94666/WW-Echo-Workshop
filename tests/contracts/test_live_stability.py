# 验证实时评分稳定键会忽略一帧 OCR 抖动但保留真实词条变化。
import unittest
from types import SimpleNamespace

from ww_echo_workshop.adapters.okww.overlay_task import _analysis_key


def analysis(score, *subs):
    return SimpleNamespace(
        layout='display',
        current_score=score,
        main_rows=(SimpleNamespace(stat_name='暴击', value=22.0),),
        sub_rows=tuple(SimpleNamespace(stat_name=name, value=value) for name, value in subs),
    )


class LiveAnalysisStabilityContract(unittest.TestCase):
    def test_same_rows_produce_same_key(self):
        left = analysis(26.83, ('暴击伤害', 13.8), ('暴击', 9.3))
        right = analysis(26.83, ('暴击伤害', 13.8), ('暴击', 9.3))
        self.assertEqual(_analysis_key(left), _analysis_key(right))

    def test_score_flicker_does_not_change_key(self):
        left = analysis(26.83, ('暴击伤害', 13.8))
        right = analysis(21.70, ('暴击伤害', 13.8))
        self.assertEqual(_analysis_key(left), _analysis_key(right))

    def test_real_row_change_changes_key(self):
        left = analysis(26.83, ('暴击伤害', 13.8))
        right = analysis(26.83, ('暴击伤害', 14.0))
        self.assertNotEqual(_analysis_key(left), _analysis_key(right))
