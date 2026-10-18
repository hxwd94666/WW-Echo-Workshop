# 轮询明确启用的前台游戏快捷键。
"""Opt-in chord polling, not a keyboard hook, reservation or input sender.

Only configured keys/modifiers are read, only with target foreground.
The Win32 high bit describes current state; the unreliable low bit is ignored.
"""

import os
import threading

from echo_workshop.application.hotkeys import ACTIONS, MODIFIERS, parse_bindings

WINDOWS_KEYS = {0x5B, 0x5C}


class ShortcutDetector:
    def __init__(self):
        self.reset()

    def reset(self):
        self._foreground = False
        self._held = set()
        self._last = {}

    def sample(self, bindings, pressed, foreground, now):
        if not foreground:
            self._foreground = False
            self._held = set()
            return ()
        mods = {name for name, vk in MODIFIERS.items() if vk in pressed}
        if pressed & WINDOWS_KEYS:
            mods.add('Win')  # Do not mistake an OS chord for a plain-key binding.
        held = {chord.vk for chord in bindings.values() if chord.vk in pressed}
        if not self._foreground:
            # Focus acquisition/reconfiguration primes existing held keys.
            self._foreground, self._held = True, held
            return ()
        rising = held - self._held
        self._held = held
        result = []
        for action in ACTIONS:
            chord = bindings.get(action)
            if (
                chord is not None
                and chord.vk in rising
                and set(chord.modifiers) == mods
                and now - self._last.get(action, float('-inf')) >= 0.4
            ):
                result.append(action)
                self._last[action] = now
        return tuple(result)


class NativeKeyReader:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('键盘快捷入口仅在 Windows 可用')
        import ctypes

        self._user32 = ctypes.WinDLL('user32', use_last_error=True)
        self._read = self._user32.GetAsyncKeyState
        self._read.argtypes = [ctypes.c_int]
        self._read.restype = ctypes.c_short

    def pressed(self, virtual_keys):
        return {vk for vk in virtual_keys if self._read(vk) & 0x8000}


class ShortcutService:
    def __init__(self, foreground, dispatch, *, reader=None, stop_event=None, autostart=True):
        self.foreground, self.dispatch = foreground, dispatch
        self.reader = reader or NativeKeyReader()
        self.runtime_stop = stop_event or threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._detector = ShortcutDetector()
        self._bindings = {}
        self._thread = None
        self._autostart = autostart
        self.generation = 0
        self.message = '快捷键未配置，不读取按键'

    def configure(self, raw):
        bindings = parse_bindings(raw)
        with self._lock:
            if self._bindings == bindings:
                return  # Reopening/arming must not discard a queued press or reset held keys.
            self._bindings = bindings
            self._detector.reset()
            self.generation += 1
            self.message = (
                '已配置：仅目标游戏在前台时响应，不拦截游戏按键'
                if bindings
                else '快捷键已禁用，不读取按键'
            )
            if (
                self._autostart
                and bindings
                and self._thread is None
                and not self._stop.is_set()
                and not self.runtime_stop.is_set()
            ):
                self._thread = threading.Thread(
                    target=self._run, name='echo-capture-shortcuts', daemon=True
                )
                self._thread.start()

    def poll(self, now):
        with self._lock:
            if self._stop.is_set() or self.runtime_stop.is_set() or not self._bindings:
                return ()
            focused = bool(self.foreground())
            keys = (
                set(MODIFIERS.values())
                | WINDOWS_KEYS
                | {chord.vk for chord in self._bindings.values()}
            )
            pressed = self.reader.pressed(keys) if focused else set()
            actions = self._detector.sample(self._bindings, pressed, focused, now)
            generation = self.generation
        for action in actions:
            self.dispatch(generation, action)
        return actions

    def _run(self):
        import time

        while not self._stop.wait(0.05) and not self.runtime_stop.is_set():
            try:
                self.poll(time.monotonic())
            except Exception as error:
                with self._lock:
                    self.message = '快捷键检查失败，未执行操作：' + str(error)
                    self._detector.reset()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
