# 校验策略数据更新地址与导入编排；不访问真实网络。
import unittest
from unittest.mock import patch

from ww_echo_workshop.core.policy import PolicyError
from ww_echo_workshop.resources.policy_updates import (
    PolicyDataUpdater,
    validate_url,
)


class PolicyUpdateContract(unittest.TestCase):
    def test_url_is_https_only(self):
        self.assertEqual(
            validate_url('https://example.com/echo-data.json'), 'https://example.com/echo-data.json'
        )
        for value in (
            'http://example.com/data.json',
            'https://user:pass@example.com/data.json',
            'https://example.com/data.json#frag',
            '',
        ):
            with self.assertRaises(PolicyError):
                validate_url(value)

    def test_updater_imports_fetched_content(self):
        class Store:
            def __init__(self):
                self.content = None

            def import_bytes(self, content):
                self.content = content
                return {'version': 'test-1'}

        store = Store()
        with patch(
            'ww_echo_workshop.resources.policy_updates.fetch',
            return_value=('https://example.com/data.json', b'{"ok":true}'),
        ):
            final, pack = PolicyDataUpdater(store).update('https://example.com/data.json')
        self.assertEqual(final, 'https://example.com/data.json')
        self.assertEqual(pack['version'], 'test-1')
        self.assertEqual(store.content, b'{"ok":true}')
