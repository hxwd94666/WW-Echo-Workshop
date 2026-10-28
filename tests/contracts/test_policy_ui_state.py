# 校验测试声骸状态保存、恢复和坏文件回退。
import tempfile
import unittest

from ww_echo_workshop.application.policy_ui_state import PolicyUiStateStore


class PolicyUiStateStoreContract(unittest.TestCase):
    def _folder(self):
        try:
            return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except OSError:
            self.skipTest('当前解释器没有可写临时目录')

    def test_roundtrip_and_bad_file_keeps_previous(self):
        context = self._folder()
        with context as folder:
            store = PolicyUiStateStore(folder)
            state = {
                'scoring': 'linear',
                'mode': 'reroll',
                'cost': 3,
                'rows': [{'name': '暴击', 'value': 6.3}] + [{'name': None, 'value': 0.0}] * 4,
                'candidate': [{'name': '暴伤', 'value': 12.6}] + [{'name': None, 'value': 0.0}] * 4,
                'target': 70.0,
                'main_score': 1.0,
                'max_score': 100.0,
                'weights': '1 2 3',
            }
            self.assertTrue(store.save(state))
            loaded = store.load()
            self.assertEqual(loaded['mode'], 'reroll')
            self.assertEqual(loaded['rows'][0]['name'], '暴击')
            store.path.write_text('not json', encoding='utf-8')
            self.assertIsNone(store.load())

    def test_invalid_state_is_rejected(self):
        context = self._folder()
        with context as folder:
            store = PolicyUiStateStore(folder)
            self.assertFalse(store.save({'mode': 'bogus'}))
            self.assertIsNone(store.load())


if __name__ == '__main__':
    unittest.main()
