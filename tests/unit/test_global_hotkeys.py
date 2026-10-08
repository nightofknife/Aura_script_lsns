"""Hotkey registration tests inject a backend and never reserve system keys."""
import ctypes
from ctypes import wintypes
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from packages.resonance_gui import hotkeys
from packages.resonance_gui.hotkeys import GlobalHotkeyManager, MOD_NOREPEAT, validate_hotkeys


class FakeBackend:
    def __init__(self):
        self.registered = {}
        self.fail = set()
        self.calls = []
        self.last_error = "conflict"

    def register(self, hotkey_id, modifiers, vk):
        self.calls.append(("register", hotkey_id, modifiers, vk))
        assert modifiers & MOD_NOREPEAT
        if (modifiers, vk) in self.fail or hotkey_id in self.registered:
            return False
        if (modifiers, vk) in self.registered.values():
            return False
        self.registered[hotkey_id] = modifiers, vk
        return True

    def unregister(self, hotkey_id):
        self.calls.append(("unregister", hotkey_id))
        return self.registered.pop(hotkey_id, None) is not None


@pytest.fixture
def manager():
    app = QApplication.instance() or QApplication([])
    backend = FakeBackend()
    value = GlobalHotkeyManager(backend=backend)
    yield value, backend
    value.close()
    app.processEvents()


def test_portable_text_normalization_and_empty_defaults():
    assert validate_hotkeys(" ctrl+alt+s ", "f8") == ("Ctrl+Alt+S", "F8")
    assert validate_hotkeys("", "") == ("", "")
    assert validate_hotkeys("", "F9") == ("", "F9")


@pytest.mark.parametrize("value", ["Ctrl", "Shift", "Alt", "Ctrl+Shift", "Ctrl+K, Ctrl+C",
                                    "F12", "Ctrl+F12", "Alt+Tab", "Ctrl+Alt+Del", "Alt+F4",
                                    "Ctrl+Esc", "Ctrl+Shift+Esc", "Alt+Space", "Meta+S", "Print",
                                    "Num+1", "nonsense", "Ctrl+Unknown"])
def test_invalid_or_reserved_hotkeys_are_rejected(value):
    with pytest.raises(ValueError):
        validate_hotkeys(value, "")


@pytest.mark.parametrize("first,second", [("ctrl+s", "Ctrl+S"), ("Return", "Enter"), ("Ctrl++", "Ctrl+Shift+=" )])
def test_same_physical_hotkeys_are_rejected(first, second):
    with pytest.raises(ValueError, match="不能相同"):
        validate_hotkeys(first, second)


def test_unset_default_never_constructs_native_backend(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(hotkeys, "WindowsHotkeyBackend", lambda: pytest.fail("native backend was created"))
    value = GlobalHotkeyManager()
    assert value.configure("", "")
    assert not value._filter_installed
    value.close()
    app.processEvents()


def test_registration_messages_and_repeat_flag(manager):
    value, backend = manager
    activations = []
    value.activated.connect(activations.append)
    assert value.configure("Ctrl+Alt+S", "F8")
    assert len(backend.registered) == 2
    assert not value._filter_installed
    for action in ("start", "stop"):
        msg = wintypes.MSG()
        msg.message = hotkeys.WM_HOTKEY
        msg.wParam = value._ids[action]
        modifiers, vk = backend.registered[value._ids[action]]
        msg.lParam = (vk << 16) | (modifiers & ~MOD_NOREPEAT)
        assert value._native_filter.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(msg)) == (True, 0)
    assert activations == ["start", "stop"]
    assert not value._handle_hotkey(1)
    before = list(backend.calls)
    assert value.configure("Ctrl+Alt+S", "F8")
    assert backend.calls == before


def test_swap_can_rebind_without_self_conflicting(manager):
    value, backend = manager
    assert value.configure("F8", "F9")
    assert value.configure("F9", "F8")
    assert backend.registered[value._ids["start"]][1] == 0x78
    assert backend.registered[value._ids["stop"]][1] == 0x77


def test_failed_second_registration_restores_old_pair(manager):
    value, backend = manager
    assert value.configure("F8", "F9")
    old = dict(backend.registered)
    errors = []
    value.errorOccurred.connect(errors.append)
    backend.fail.add((MOD_NOREPEAT, 0x7A))  # F11
    assert not value.configure("F10", "F11")
    assert backend.registered == old
    assert value._configured == ("F8", "F9")
    assert "旧配置已恢复" in errors[-1]


def test_invalid_configuration_leaves_live_bindings_untouched(manager):
    value, backend = manager
    assert value.configure("F8", "F9")
    before = list(backend.calls)
    assert not value.configure("F12", "F9")
    assert backend.calls == before


def test_suspend_releases_keys_and_configuration_updates_do_not_register_until_resume(manager):
    value, backend = manager
    calls = []
    value.activated.connect(calls.append)
    assert value.configure("F8", "F9")
    value.set_suspended(True)
    assert backend.registered == {}
    assert value._handle_hotkey(value._ids["start"])
    assert calls == []
    before = list(backend.calls)
    assert value.configure("Ctrl+S", "Ctrl+T")
    assert backend.calls == before
    value.set_suspended(False)
    assert len(backend.registered) == 2
    value._handle_hotkey(value._ids["stop"])
    assert calls == ["stop"]


def test_rebound_hotkey_ignores_stale_native_messages(manager):
    value, backend = manager
    calls = []
    value.activated.connect(calls.append)
    assert value.configure("F8", "F9")
    assert value.configure("F10", "F11")
    value._handle_hotkey(value._ids["start"], 0, 0x77)
    assert calls == []
    value._handle_hotkey(value._ids["start"], 0, 0x79)
    assert calls == ["start"]


def test_close_releases_both_ids_and_is_idempotent(manager):
    value, backend = manager
    assert value.configure("F8", "F9")
    value.close()
    assert not backend.registered
    assert not value._quit_connected
    before = list(backend.calls)
    value.close()
    assert backend.calls == before
    assert not value.configure("F8", "")


def test_unregister_failure_does_not_start_rebinding_and_restores_removed_old_key(manager, monkeypatch):
    value, backend = manager
    assert value.configure("F8", "F9")
    old = dict(backend.registered)
    original = backend.unregister
    fail_once = [True]
    def unregister(hotkey_id):
        if hotkey_id == value._ids["start"] and fail_once[0]:
            fail_once[0] = False
            return False
        return original(hotkey_id)
    monkeypatch.setattr(backend, "unregister", unregister)
    assert not value.configure("F10", "F11")
    assert backend.registered == old
    assert value._configured == ("F8", "F9")


def test_resume_conflict_reports_no_registered_keys_and_can_retry(manager):
    value, backend = manager
    errors = []
    value.errorOccurred.connect(errors.append)
    assert value.configure("F8", "F9")
    value.set_suspended(True)
    backend.fail.add((MOD_NOREPEAT, 0x77))
    value.set_suspended(False)
    assert not backend.registered
    assert "当前未注册快捷键" in errors[-1]
    backend.fail.clear()
    assert value.configure("F8", "F9")
    assert len(backend.registered) == 2


def test_failed_rollback_is_reported_without_claiming_old_pair_is_restored(manager):
    value, backend = manager
    errors, calls = [], []
    value.errorOccurred.connect(errors.append)
    value.activated.connect(calls.append)
    assert value.configure("F8", "F9")
    backend.fail.update({(MOD_NOREPEAT, 0x77), (MOD_NOREPEAT, 0x7A)})
    assert not value.configure("F10", "F11")
    assert "恢复失败" in errors[-1]
    assert value._ids["start"] not in backend.registered
    value._handle_hotkey(value._ids["start"])
    assert calls == []
    # A successfully restored old stop key remains useful even if start is lost.
    value._handle_hotkey(value._ids["stop"])
    assert calls == ["stop"]


def test_winapi_backend_uses_thread_messages_without_real_registration(monkeypatch):
    calls = []
    class Api:
        def __call__(self, *args):
            calls.append(args)
            return True
    user32 = SimpleNamespace(RegisterHotKey=Api(), UnregisterHotKey=Api())
    monkeypatch.setattr(hotkeys.sys, "platform", "win32")
    monkeypatch.setattr(hotkeys.ctypes, "WinDLL", lambda *args, **kwargs: user32)
    backend = hotkeys.WindowsHotkeyBackend()
    assert backend.register(23, MOD_NOREPEAT, 0x77)
    assert backend.unregister(23)
    assert calls == [(None, 23, MOD_NOREPEAT, 0x77), (None, 23)]


def test_default_native_filter_is_lazy_and_close_removes_it_and_disconnects_quit(monkeypatch):
    app = QApplication.instance() or QApplication([])
    backend = FakeBackend()
    filters = []
    monkeypatch.setattr(hotkeys, "WindowsHotkeyBackend", lambda: backend)
    monkeypatch.setattr(app, "installNativeEventFilter", lambda value: filters.append(("install", value)))
    monkeypatch.setattr(app, "removeNativeEventFilter", lambda value: filters.append(("remove", value)))
    value = GlobalHotkeyManager()
    assert value.configure("", "")
    assert filters == []
    assert value.configure("F8", "F9")
    assert filters == [("install", value._native_filter)]
    assert value._filter_installed
    assert value.configure("F10", "F11")
    assert len(filters) == 1
    value.close()
    assert filters == [("install", value._native_filter), ("remove", value._native_filter)]
    assert not value._filter_installed
    assert not value._quit_connected
    before = list(backend.calls)
    app.aboutToQuit.emit()
    assert backend.calls == before


def test_active_shortcuts_defaults_normalized_pair_and_read_only_property(manager):
    value, _backend = manager
    assert value.active_shortcuts == ("", "")
    assert value.configure("ctrl+alt+s", "f9")
    assert value.active_shortcuts == ("Ctrl+Alt+S", "F9")
    with pytest.raises(AttributeError):
        value.active_shortcuts = ("F10", "F11")
    assert value.configure("", "F8")
    assert value.active_shortcuts == ("", "F8")


def test_active_shortcuts_are_empty_during_suspend_and_resume_conflict(manager):
    value, backend = manager
    assert value.configure("F8", "F9")
    value.set_suspended(True)
    assert value.active_shortcuts == ("", "")
    assert value.configure("F10", "F11")
    assert value.active_shortcuts == ("", "")
    backend.fail.add((MOD_NOREPEAT, 0x79))
    value.set_suspended(False)
    assert value._configured == ("F10", "F11")
    assert not value._registered
    assert value.active_shortcuts == ("", "")
    backend.fail.clear()
    assert value.configure("F10", "F11")
    assert value.active_shortcuts == ("F10", "F11")


def test_active_shortcuts_include_only_successfully_restored_actions(manager):
    value, backend = manager
    assert value.configure("F8", "F9")
    backend.fail.update({(MOD_NOREPEAT, 0x77), (MOD_NOREPEAT, 0x7A)})
    assert not value.configure("F10", "F11")
    assert value._configured == ("F8", "F9")
    assert value.active_shortcuts == ("", "F9")


def test_active_shortcuts_exclude_missing_mismatched_or_wrong_action_entries(manager):
    value, _backend = manager
    assert value.configure("F8", "F9")
    original = dict(value._registered)
    start_id = value._ids["start"]
    value._registered[start_id] = ("start", hotkeys.MOD_CONTROL, 0x77)
    assert value.active_shortcuts == ("", "F9")
    value._registered[start_id] = ("start", 0, 0x79)
    assert value.active_shortcuts == ("", "F9")
    value._registered[start_id] = ("stop", 0, 0x77)
    assert value.active_shortcuts == ("", "F9")
    value._registered.pop(value._ids["stop"])
    assert value.active_shortcuts == ("", "")
    value._registered.update(original)


def test_active_shortcuts_compare_physical_specs_and_return_portable_text(manager):
    value, _backend = manager
    assert value.configure("Ctrl++", "F8")
    assert value.active_shortcuts == ("Ctrl++", "F8")
    value._configured = ("Ctrl+Shift+=", "F8")
    assert value.active_shortcuts == ("Ctrl+Shift+=", "F8")


def test_active_shortcuts_closed_or_suspended_hide_even_retained_registrations(manager, monkeypatch):
    value, backend = manager
    assert value.configure("F8", "F9")
    monkeypatch.setattr(backend, "unregister", lambda _hotkey_id: False)
    value.set_suspended(True)
    assert value._registered
    assert value.active_shortcuts == ("", "")
    value.close()
    assert value._registered
    assert value.active_shortcuts == ("", "")
