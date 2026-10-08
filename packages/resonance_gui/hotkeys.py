"""Validated, thread-scoped Windows global hotkeys with rollback on conflicts."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from itertools import count
import sys
import weakref
from typing import Protocol

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, QThread, Qt, Signal
from PySide6.QtGui import QKeySequence

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312
_IDS = count(0x6000, 2)
_MODIFIER_KEYS = {Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift, Qt.Key.Key_Meta,
                  Qt.Key.Key_AltGr, Qt.Key.Key_Super_L, Qt.Key.Key_Super_R}
_SPECIAL_KEYS = {
    Qt.Key.Key_Backspace: 0x08, Qt.Key.Key_Tab: 0x09, Qt.Key.Key_Backtab: 0x09,
    Qt.Key.Key_Return: 0x0D, Qt.Key.Key_Enter: 0x0D, Qt.Key.Key_Pause: 0x13,
    Qt.Key.Key_CapsLock: 0x14, Qt.Key.Key_Escape: 0x1B, Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22, Qt.Key.Key_End: 0x23,
    Qt.Key.Key_Home: 0x24, Qt.Key.Key_Left: 0x25, Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28, Qt.Key.Key_Insert: 0x2D,
    Qt.Key.Key_Delete: 0x2E, Qt.Key.Key_NumLock: 0x90, Qt.Key.Key_ScrollLock: 0x91,
}
# RegisterHotKey needs a virtual key, not the printable character produced by it.
_PUNCTUATION = {
    ";": (0xBA, 0), ":": (0xBA, MOD_SHIFT), "=": (0xBB, 0), "+": (0xBB, MOD_SHIFT),
    ",": (0xBC, 0), "<": (0xBC, MOD_SHIFT), "-": (0xBD, 0), "_": (0xBD, MOD_SHIFT),
    ".": (0xBE, 0), ">": (0xBE, MOD_SHIFT), "/": (0xBF, 0), "?": (0xBF, MOD_SHIFT),
    "`": (0xC0, 0), "~": (0xC0, MOD_SHIFT), "[": (0xDB, 0), "{": (0xDB, MOD_SHIFT),
    "\\": (0xDC, 0), "|": (0xDC, MOD_SHIFT), "]": (0xDD, 0), "}": (0xDD, MOD_SHIFT),
    "'": (0xDE, 0), '"': (0xDE, MOD_SHIFT),
    **{symbol: (ord(digit), MOD_SHIFT) for symbol, digit in zip("!@#$%^&*()", "1234567890")},
}


def _shortcut_spec(value: str) -> tuple[str, int, int]:
    if not isinstance(value, str):
        raise ValueError("快捷键必须是文本")
    value = value.strip()
    if not value:
        return "", 0, 0
    sequence = QKeySequence.fromString(value, QKeySequence.SequenceFormat.PortableText)
    if sequence.count() != 1:
        raise ValueError("快捷键只能包含一次同时按键，不能使用连续组合")
    combination = sequence[0]
    key, qt_mods = combination.key(), combination.keyboardModifiers()
    if key in _MODIFIER_KEYS or key == Qt.Key.Key_unknown or int(key) == 0:
        raise ValueError("快捷键需要一个主键，不能仅使用修饰键")
    if qt_mods & (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.KeypadModifier
                  | Qt.KeyboardModifier.GroupSwitchModifier):
        raise ValueError("不支持 Windows 键、AltGr 或区分数字小键盘的组合")
    if key == Qt.Key.Key_F12 or key == Qt.Key.Key_Print:
        raise ValueError("F12 与 PrintScreen 是系统保留按键")
    mods = (MOD_CONTROL if qt_mods & Qt.KeyboardModifier.ControlModifier else 0)
    mods |= MOD_ALT if qt_mods & Qt.KeyboardModifier.AltModifier else 0
    mods |= MOD_SHIFT if qt_mods & Qt.KeyboardModifier.ShiftModifier else 0
    if key == Qt.Key.Key_Backtab:
        mods |= MOD_SHIFT
    number = int(key)
    if ord("A") <= number <= ord("Z") or ord("0") <= number <= ord("9"):
        vk = number
    elif int(Qt.Key.Key_F1) <= number <= int(Qt.Key.Key_F24):
        vk = 0x70 + number - int(Qt.Key.Key_F1)
    elif key in _SPECIAL_KEYS:
        vk = _SPECIAL_KEYS[key]
    elif 0 <= number <= 0x10FFFF and chr(number) in _PUNCTUATION:
        vk, extra_mods = _PUNCTUATION[chr(number)]
        mods |= extra_mods
    else:
        raise ValueError("此按键不支持 Windows 全局快捷键")
    if (mods & MOD_ALT and vk in {0x09, 0x1B, 0x20, 0x73}) or (
        mods & MOD_CONTROL and vk == 0x1B
    ) or (mods & MOD_CONTROL and mods & MOD_ALT and vk == 0x2E):
        raise ValueError("此组合为 Windows 系统保留快捷键")
    return sequence.toString(QKeySequence.SequenceFormat.PortableText), mods, vk


def validate_hotkeys(start: str, stop: str) -> tuple[str, str]:
    first, second = _shortcut_spec(start), _shortcut_spec(stop)
    if first[0] and second[0] and first[1:] == second[1:]:
        raise ValueError("开始与停止快捷键不能相同")
    return first[0], second[0]


class HotkeyBackend(Protocol):
    """Injected backends never need to call the real Windows registry API."""

    def register(self, hotkey_id: int, modifiers: int, virtual_key: int) -> bool: ...
    def unregister(self, hotkey_id: int) -> bool: ...


class WindowsHotkeyBackend:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("全局快捷键仅支持 Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self.last_error = ""

    def register(self, hotkey_id: int, modifiers: int, virtual_key: int) -> bool:
        ok = bool(self._user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key))
        self.last_error = "" if ok else str(ctypes.WinError(ctypes.get_last_error()))
        return ok

    def unregister(self, hotkey_id: int) -> bool:
        ok = bool(self._user32.UnregisterHotKey(None, hotkey_id))
        self.last_error = "" if ok else str(ctypes.WinError(ctypes.get_last_error()))
        return ok


class _NativeHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, manager: GlobalHotkeyManager) -> None:
        super().__init__()
        self._manager = weakref.ref(manager)

    def nativeEventFilter(self, event_type, message):  # noqa: N802, ANN001
        if bytes(event_type) not in {b"windows_generic_MSG", b"windows_dispatcher_MSG"}:
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message != WM_HOTKEY:
            return False, 0
        manager = self._manager()
        if manager is None:
            return False, 0
        handled = manager._handle_hotkey(int(msg.wParam), int(msg.lParam) & 0xFFFF,
                                               (int(msg.lParam) >> 16) & 0xFFFF)
        return handled, 0


class GlobalHotkeyManager(QObject):
    """Create/configure/close on the Qt GUI thread; defaults do not register keys.

    Backend methods are ``register(id, modifiers, vk)`` and ``unregister(id)``.
    Suspension releases registrations so the recorder can receive those keys.
    """

    activated = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None, *, backend: HotkeyBackend | None = None,
                 native_events: bool | None = None) -> None:
        super().__init__(parent)
        base = next(_IDS)
        if base + 1 > 0xBFFF:
            raise RuntimeError("全局快捷键标识已耗尽")
        self._ids = {"start": base, "stop": base + 1}
        self._backend = backend
        self._use_native_events = backend is None if native_events is None else bool(native_events)
        self._filter_installed = False
        self._configured = ("", "")
        self._registered: dict[int, tuple[str, int, int]] = {}
        self._suspended = False
        self._closed = False
        self._backend_error = ""
        self._native_filter = _NativeHotkeyFilter(self)
        self._app = QCoreApplication.instance()
        self._quit_connected = False
        if self._app is not None and self.thread() == self._app.thread():
            self._app.aboutToQuit.connect(self.close)
            self._quit_connected = True

    @property
    def active_shortcuts(self) -> tuple[str, str]:
        """Snapshot only effective registrations, not saved or desired values.

        A failed resume/rollback may leave no bindings or only one old binding.
        Stale registrations that no longer match the configured physical key
        are deliberately excluded, just as they are from activation dispatch.
        """
        if self._closed or self._suspended:
            return "", ""
        active = []
        for action, value in zip(("start", "stop"), self._configured):
            text, modifiers, vk = _shortcut_spec(value)
            binding = self._registered.get(self._ids[action])
            active.append(text if text and binding == (action, modifiers, vk) else "")
        return active[0], active[1]

    def _call(self, operation: str, *args: int) -> bool:
        try:
            if self._backend is None:
                self._backend = WindowsHotkeyBackend()
            ok = bool(getattr(self._backend, operation)(*args))
            self._backend_error = str(getattr(self._backend, "last_error", ""))
            return ok
        except Exception as exc:
            self._backend_error = str(exc)
            return False

    def _release(self) -> bool:
        ok = True
        for hotkey_id in list(self._registered):
            if self._call("unregister", hotkey_id):
                self._registered.pop(hotkey_id)
            else:
                ok = False
        return ok

    def _register(self, hotkey_id: int, binding: tuple[str, int, int]) -> bool:
        action, modifiers, vk = binding
        if not self._call("register", hotkey_id, modifiers | MOD_NOREPEAT, vk):
            return False
        self._registered[hotkey_id] = action, modifiers, vk
        return True

    def _restore(self, old: dict[int, tuple[str, int, int]]) -> bool:
        ok = True
        for hotkey_id, binding in old.items():
            if hotkey_id not in self._registered and not self._register(hotkey_id, binding):
                ok = False
        return ok

    def configure(self, start: str, stop: str) -> bool:
        if self._closed or QThread.currentThread() != self.thread():
            self.errorOccurred.emit("快捷键管理器已关闭或调用线程不正确")
            return False
        if self._app is not None and self.thread() != self._app.thread():
            self.errorOccurred.emit("全局快捷键必须在 Qt GUI 线程设置")
            return False
        if self._app is None and (start or stop):
            self.errorOccurred.emit("请在 Qt 应用初始化后设置全局快捷键")
            return False
        try:
            values = validate_hotkeys(start, stop)
        except ValueError as exc:
            self.errorOccurred.emit(str(exc))
            return False
        if self._suspended:
            self._configured = values
            return True
        desired = {}
        for action, value in zip(("start", "stop"), values):
            if value:
                _text, mods, vk = _shortcut_spec(value)
                desired[self._ids[action]] = action, mods, vk
        if desired == self._registered:
            self._configured = values
            return True
        if desired and self._use_native_events and not self._filter_installed:
            try:
                self._app.installNativeEventFilter(self._native_filter)
                self._filter_installed = True
            except RuntimeError as exc:
                self.errorOccurred.emit(f"全局快捷键消息监听失败：{exc}")
                return False
        old = dict(self._registered)
        if not self._release():
            recovered = self._restore(old)
            self.errorOccurred.emit("旧快捷键释放失败" + ("；恢复失败" if not recovered else ""))
            return False
        for hotkey_id, binding in desired.items():
            if not self._register(hotkey_id, binding):
                error = self._backend_error
                cleaned = self._release()
                recovered = self._restore(old) if cleaned else False
                recovery = ("；旧配置已恢复" if old else "；当前未注册快捷键") if recovered else "；旧配置恢复失败，请重新设置"
                self.errorOccurred.emit("快捷键注册失败，可能已被占用：" + error + recovery)
                return False
        self._configured = values
        return True

    def set_suspended(self, suspended: bool) -> None:
        if self._closed or self._suspended == bool(suspended):
            return
        if QThread.currentThread() != self.thread():
            self.errorOccurred.emit("快捷键暂停必须在管理器所属线程执行")
            return
        self._suspended = bool(suspended)
        if suspended:
            if not self._release():
                self.errorOccurred.emit("录制前释放全局快捷键失败")
        else:
            self.configure(*self._configured)

    def _handle_hotkey(self, hotkey_id: int, modifiers: int | None = None, vk: int | None = None) -> bool:
        if hotkey_id not in self._ids.values():
            return False
        binding = self._registered.get(hotkey_id)
        if not self._closed and not self._suspended and binding is not None:
            action_index = 0 if binding[0] == "start" else 1
            expected = _shortcut_spec(self._configured[action_index])[1:]
            if binding[1:] == expected and (modifiers is None or (modifiers & ~MOD_NOREPEAT, vk) == binding[1:]):
                self.activated.emit(binding[0])
        return True

    def close(self) -> None:
        if self._closed and not self._registered:
            return
        if QThread.currentThread() != self.thread():
            self.errorOccurred.emit("快捷键释放必须在管理器所属线程执行")
            return
        self._closed = True
        if not self._release():
            self.errorOccurred.emit("关闭时释放全局快捷键失败")
        if self._app is not None and self._filter_installed:
            self._app.removeNativeEventFilter(self._native_filter)
            self._filter_installed = False
        if self._app is not None and self._quit_connected:
            try:
                self._app.aboutToQuit.disconnect(self.close)
            except (RuntimeError, TypeError):
                pass
            self._quit_connected = False
