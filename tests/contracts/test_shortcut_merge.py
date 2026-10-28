# 校验旧配置下 HUD/详情快捷键仍能注册，显式清空则不再回退默认。
import unittest

try:
    from ww_echo_workshop.adapters.okww.capture_commands import CaptureCommands
    from ww_echo_workshop.application.hotkeys import encode_bindings, parse_bindings
except ImportError:
    CaptureCommands = None
    encode_bindings = None
    parse_bindings = None


@unittest.skipUnless(CaptureCommands is not None, '当前解释器没有宿主 ok 依赖')
class ShortcutMergeContract(unittest.TestCase):
    class Fake(CaptureCommands or object):
        def __init__(self, config):
            self.config = config

        def _ensure_signals(self, card):
            pass

    def test_old_config_gets_hud_and_detail_defaults(self):
        fake = self.Fake({'CaptureHotkeysJSON': ''})
        raw = fake._shortcut_payload('')
        bindings = parse_bindings(raw)
        self.assertEqual(bindings['policy_detail'].text, 'Ctrl+Alt+P')
        self.assertEqual(bindings['policy_hud_edit'].text, 'Ctrl+Alt+L')

    def test_explicit_empty_disables_fallback(self):
        raw = encode_bindings(
            {'capture': '', 'stop': '', 'policy_detail': '', 'policy_hud_edit': ''}
        )
        fake = self.Fake({'CaptureHotkeysJSON': raw})
        bindings = parse_bindings(fake._shortcut_payload(raw))
        self.assertNotIn('policy_hud_edit', bindings)
        self.assertNotIn('policy_detail', bindings)


if __name__ == '__main__':
    unittest.main()
