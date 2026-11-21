# 用合成按键和延迟的 GUI 消息验证收录意图，不读取真实键盘。
from itertools import product
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ww_echo_workshop.adapters.okww.capture_commands import CaptureCommands
from ww_echo_workshop.adapters.okww.capture_device import capture_source_identity
from ww_echo_workshop.adapters.okww.shortcuts import ShortcutService
from ww_echo_workshop.application.hotkeys import KEY_CODES, encode_bindings
from ww_echo_workshop.capture.intake import CaptureSession


class CaptureShortcutContract(unittest.TestCase):
    def test_queued_shortcut_stays_with_its_original_intake_and_source(self):
        cases = tuple(
            (action, change)
            for action, change in product(
                ('capture', 'stop'), ('same_session', 'new_session', 'new_profile', 'new_source')
            )
        ) + (
            ('capture', 'new_attempt'),
            ('capture', 'probe_changes_source'),
            ('stop', 'manual_progress'),
            ('stop', 'scan_pause'),
            ('stop', 'new_scan'),
        )
        for action, change in cases:
            with self.subTest(action=action, change=change):
                pressed, messages = set(), []
                capture = CaptureSession(
                    SimpleNamespace(profiles=lambda: {'first', 'second'}),
                    None,
                    job_factory=Mock(),
                    clock=lambda: 1.0,
                )
                host = SimpleNamespace(capture_method=object(), hwnd_window=None)
                runtime = SimpleNamespace(
                    capture=capture,
                    shortcuts=None,
                    backups=SimpleNamespace(restoring=False),
                    stop_event=Event(),
                    _closed=False,
                    weights=SimpleNamespace(active=None),
                    sonata=SimpleNamespace(active=None),
                )
                runtime.stop_intake = capture.stop
                command = CaptureCommands()
                command.runtime = runtime
                command.config = {'InventoryProfileID': 'first'}
                command.executor = SimpleNamespace(_last_frame_time=1)
                command._destroyed = False
                command._game_foreground = lambda: True
                command._capture_ready = lambda: True
                command._ensure_signals = Mock()
                command.get_tasks = tuple
                command.log_info = Mock()
                command._window_signals = SimpleNamespace(
                    shortcut=SimpleNamespace(emit=lambda *args: messages.append(args))
                )
                raw = encode_bindings({'capture': 'F8', 'stop': 'F9'})
                reader = SimpleNamespace(pressed=lambda keys: pressed & keys)
                with (
                    patch(
                        'ww_echo_workshop.adapters.okww.shortcuts.ShortcutService',
                        side_effect=lambda *args, **kw: ShortcutService(
                            *args, **kw, reader=reader, autostart=False
                        ),
                    ),
                    patch(
                        'ww_echo_workshop.adapters.okww.capture_commands.device_manager',
                        return_value=host,
                    ),
                ):
                    if change in ('scan_pause', 'new_scan'):
                        capture.start_scan('first', 1)
                    else:
                        capture.arm_manual('first')
                    command._configure_shortcuts(raw)
                    runtime.shortcuts.poll(1.0)  # Prime focus without a key.
                    pressed.add(KEY_CODES['F8' if action == 'capture' else 'F9'])
                    self.assertEqual((action,), runtime.shortcuts.poll(1.1))
                    self.assertEqual(1, len(messages))
                    if change in ('new_session', 'new_profile'):
                        profile = 'second' if change == 'new_profile' else 'first'
                        capture.stop()
                        command.config['InventoryProfileID'] = profile
                        capture.arm_manual(profile)
                    elif change == 'new_source':
                        host.capture_method = object()
                    elif change == 'new_attempt':
                        self.assertTrue(capture.request_once('first'))
                        capture.cancel_once()
                    elif change == 'probe_changes_source':

                        def ready_then_switch():
                            host.capture_method = object()
                            return True

                        command._capture_ready = ready_then_switch
                    elif change == 'manual_progress':
                        self.assertTrue(capture.request_once('first'))
                        request = capture.single_request
                        capture.single_progress(request, 'recognizing', '识别中')
                    elif change == 'scan_pause':
                        capture.pause(generation=1)
                    elif change == 'new_scan':
                        capture.stop(generation=1)
                        capture.start_scan('first', 2)
                    command.handle_shortcut(*messages[0])
                if (action, change) == ('capture', 'same_session'):
                    self.assertEqual('single', capture.status.state)
                    self.assertEqual(
                        capture_source_identity(host), capture.single_request.source_identity
                    )
                elif action == 'stop' and change in (
                    'same_session',
                    'manual_progress',
                    'scan_pause',
                ):
                    self.assertEqual('stopped', capture.status.state)
                elif change == 'new_scan':
                    self.assertEqual('active', capture.scan_status(2).state)
                else:
                    self.assertEqual('armed', capture.status.state, 'stale shortcut reached intake')
                    self.assertIsNone(capture.single_request)
