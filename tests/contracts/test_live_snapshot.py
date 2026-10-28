# 校验实时快照只读、可清空，且不保存截图。
import unittest
from types import SimpleNamespace

from ww_echo_workshop.application.live_snapshot import LiveSnapshotStore


class LiveSnapshotContract(unittest.TestCase):
    def test_capture_copies_rows_and_metadata(self):
        store = LiveSnapshotStore()
        analysis = SimpleNamespace(
            cost=4,
            main_rows=(SimpleNamespace(stat_name='暴击', value=22.0),),
            sub_rows=(SimpleNamespace(stat_name='暴击', value=6.3),),
            selected_ids=('1601', 'default'),
            current_score=9.19,
            observed_cost=4,
            layout='enhancement',
        )
        snapshot = store.capture(analysis, now=12.0)
        self.assertEqual(snapshot.sub_rows, (('暴击', 6.3),))
        self.assertEqual(snapshot.current_score, 9.19)
        self.assertEqual(snapshot.captured_at, 12.0)
        self.assertIs(store.latest(), snapshot)
        store.clear()
        self.assertIsNone(store.latest())

    def test_empty_analysis_does_not_replace_previous_snapshot(self):
        store = LiveSnapshotStore()
        store.capture(
            SimpleNamespace(
                cost=4,
                main_rows=(),
                sub_rows=(SimpleNamespace(stat_name='暴击', value=6.3),),
                selected_ids=(),
                current_score=1.0,
                observed_cost=4,
                layout='enhancement',
            )
        )
        previous = store.latest()
        store.capture(
            SimpleNamespace(
                cost=None,
                main_rows=(),
                sub_rows=(),
                selected_ids=(),
                current_score=None,
                observed_cost=None,
                layout='',
            )
        )
        self.assertIs(store.latest(), previous)


if __name__ == '__main__':
    unittest.main()
