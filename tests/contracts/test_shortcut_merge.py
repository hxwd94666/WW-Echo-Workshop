# 校验已移除的旧快捷键配置不会阻止新配置解析。
import unittest

from ww_echo_workshop.application.hotkeys import encode_bindings, parse_bindings


class ShortcutCompatibilityContract(unittest.TestCase):
    def test_removed_actions_are_ignored(self):
        raw = (
            '{"schema_version":1,"bindings":{'
            '"capture":"F8","stop":"F9",'
            '"policy_detail":"Ctrl+Alt+P","policy_hud_edit":"F5"}}'
        )
        bindings = parse_bindings(raw)
        self.assertEqual(bindings['capture'].text, 'F8')
        self.assertEqual(bindings['stop'].text, 'F9')
        self.assertNotIn('policy_detail', bindings)
        self.assertNotIn('policy_hud_edit', bindings)

    def test_new_bindings_roundtrip(self):
        raw = encode_bindings({'capture': 'F8', 'stop': 'F9'})
        bindings = parse_bindings(raw)
        self.assertEqual(set(bindings), {'capture', 'stop'})
