"""Settings persistence and task controls without sending game input."""

import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.app import _light_application_palette
from packages.resonance_gui.hotkeys import validate_hotkeys
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.settings_hub_page import SettingsHubPage


class FakeHotkeys(QObject):
    activated = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, parent=None, *, backend=None):
        super().__init__(parent)
        self.bindings = ("", "")
        self.suspended = False
        self.closed = False
        self.fail = False
        self.fail_pair = None
        self.calls = []

    def configure(self, start, stop):
        self.calls.append((start, stop))
        if self.fail or (start, stop) == self.fail_pair:
            self.errorOccurred.emit("快捷键被其他程序占用")
            return False
        self.bindings = validate_hotkeys(start, stop)
        return True

    def set_suspended(self, suspended):
        self.suspended = suspended

    def close(self):
        self.closed = True

    @property
    def active_shortcuts(self):
        return self.bindings if not self.closed and not self.suspended else ("", "")


@pytest.fixture(autouse=True)
def cleanup_windows():
    app = QApplication.instance() or QApplication([])
    existing = set(app.topLevelWidgets())
    yield
    for widget in set(app.topLevelWidgets()) - existing:
        if isinstance(widget, ResonanceMainWindow):
            widget._close_ready = True
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def repository(tmp_path):
    app = QApplication.instance() or QApplication([])
    yield ResonanceConfigRepository(QSettings(str(tmp_path / "hotkeys.ini"), QSettings.Format.IniFormat))
    app.processEvents()


@pytest.fixture
def window(repository, monkeypatch):
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr("packages.resonance_gui.main_window.GlobalHotkeyManager", FakeHotkeys)
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    style = QStyleFactory.create("Fusion")
    style.setParent(widget)
    widget.setStyle(style)
    for child in widget.findChildren(QWidget):
        child.setStyle(style)
    widget.setPalette(_light_application_palette())
    widget.setFont(QFont("Microsoft YaHei", 10))
    yield widget
    widget._busy = False
    widget._workflow_active = False
    widget._close_ready = True
    widget.close()
    assert widget._hotkeys.closed


def test_defaults_and_separate_saved_bindings(repository):
    page = SettingsHubPage(repository)
    assert repository.load_hotkeys() == {"start": "", "stop": ""}
    assert page.categories.item(1).text() == "执行设置"
    page.start_hotkey.set_shortcut("Ctrl+Alt+F8")
    page.stop_hotkey.set_shortcut("Ctrl+Alt+F9")
    page.save_values()
    assert page.save_result.text() == "设置已保存"
    restored = SettingsHubPage(repository)
    assert restored.start_hotkey.shortcut() == "Ctrl+Alt+F8"
    assert restored.stop_hotkey.shortcut() == "Ctrl+Alt+F9"
    page.close()
    restored.close()


def test_duplicate_hotkey_does_not_save(repository):
    page = SettingsHubPage(repository)
    page.start_hotkey.set_shortcut("Ctrl+F8")
    page.stop_hotkey.set_shortcut("Ctrl+F8")
    page.save_values()
    assert "无效" in page.save_result.text()
    assert repository.load_hotkeys() == {"start": "", "stop": ""}
    page.close()


def test_apply_failure_preserves_saved_and_active_bindings(window, repository):
    page = window.settings_page
    page.start_hotkey.set_shortcut("Ctrl+Alt+F8")
    page.stop_hotkey.set_shortcut("Ctrl+Alt+F9")
    page.save_values()
    old = repository.load_hotkeys()
    window._hotkeys.fail = True
    page.stop_hotkey.set_shortcut("Ctrl+Alt+F10")
    page.save_values()
    assert "占用" in page.save_result.text()
    assert repository.load_hotkeys() == old
    assert window._hotkeys.bindings == (old["start"], old["stop"])


def test_persistence_failure_restores_runtime_bindings(window, repository, monkeypatch):
    page = window.settings_page
    page.start_hotkey.set_shortcut("Ctrl+F8")
    page.stop_hotkey.set_shortcut("Ctrl+F9")
    page.save_values()
    old = window._hotkeys.bindings
    page.stop_hotkey.set_shortcut("Ctrl+F10")
    monkeypatch.setattr(repository, "save_hotkeys", Mock(side_effect=OSError("不可写")))
    page.save_values()
    assert "不可写" in page.save_result.text()
    assert window._hotkeys.bindings == old
    assert repository.load_hotkeys() == {"start": old[0], "stop": old[1]}


def test_pair_storage_failure_restores_previous_values(repository, monkeypatch):
    repository.save_hotkeys("Ctrl+F8", "Ctrl+F9")
    monkeypatch.setattr(repository, "sync_checked", Mock(side_effect=OSError("不可写")))
    with pytest.raises(OSError):
        repository.save_hotkeys("Ctrl+F10", "Ctrl+F11")
    assert repository.load_hotkeys() == {"start": "Ctrl+F8", "stop": "Ctrl+F9"}


@pytest.mark.parametrize("previous", [
    ("Ctrl+K, Ctrl+C", "Ctrl+F9"), ("Ctrl+F10", "Ctrl+F11"),
])
def test_failed_save_restores_actual_active_pair_not_bad_or_unavailable_saved_values(window, repository, monkeypatch, previous):
    repository.save_hotkeys(*previous)
    assert window._hotkeys.active_shortcuts == ("", "")
    page = window.settings_page
    page.start_hotkey.set_shortcut("Ctrl+F8")
    page.stop_hotkey.set_shortcut("Ctrl+F9")
    monkeypatch.setattr(repository, "save_hotkeys", Mock(side_effect=OSError("不可写")))
    page.save_values()
    assert "不可写" in page.save_result.text()
    assert window._hotkeys.active_shortcuts == ("", "")
    assert repository.load_hotkeys() == {"start": previous[0], "stop": previous[1]}


def test_failed_save_with_new_conflict_on_old_pair_disables_unpersisted_hotkeys(window, repository, monkeypatch):
    page = window.settings_page
    page.start_hotkey.set_shortcut("Ctrl+F8")
    page.stop_hotkey.set_shortcut("Ctrl+F9")
    page.save_values()
    page.stop_hotkey.set_shortcut("Ctrl+F10")

    def fail_persistence(*_args):
        window._hotkeys.fail_pair = ("Ctrl+F8", "Ctrl+F9")
        raise OSError("不可写")

    monkeypatch.setattr(repository, "save_hotkeys", fail_persistence)
    page.save_values()
    assert window._hotkeys.active_shortcuts == ("", "")
    assert "停用" in page.save_result.text()


def test_hotkey_start_uses_workflow_and_is_ignored_while_busy_or_closing(window, monkeypatch):
    start = Mock()
    monkeypatch.setattr(window, "_start_workflow", start)
    window._hotkeys.activated.emit("start")
    start.assert_called_once()
    for flag in ("_busy", "_workflow_active", "_commerce_active", "_closing"):
        setattr(window, flag, True)
        window._hotkeys.activated.emit("start")
        assert start.call_count == 1
        setattr(window, flag, False)


def test_stop_cancels_current_and_remaining_workflow_without_running_tasks(window):
    cancelled, cleared = [], []
    window.requestCancelCurrent.connect(lambda: cancelled.append(True))
    window.requestClearQueue.connect(lambda: cleared.append(True))
    window._workflow_active = True
    window._busy = True
    window._workflow_pending = [{"step": "trade"}, {"step": "passenger"}]
    window._hotkeys.activated.emit("stop")
    assert window._workflow_pending == []
    assert window._workflow_stopping
    assert cancelled == [True]
    assert cleared == [True]
    window._hotkeys.activated.emit("stop")
    assert cancelled == [True]


def test_stop_handles_independent_task_and_does_nothing_when_idle(window):
    cancelled = []
    window.requestCancelCurrent.connect(lambda: cancelled.append(True))
    window._hotkeys.activated.emit("stop")
    assert cancelled == []
    window._busy = True
    window._hotkeys.activated.emit("stop")
    assert cancelled == [True]


def test_recording_pauses_hotkeys_does_not_save_and_cancel_restores_bindings(window, repository, monkeypatch):
    page = window.settings_page
    page.categories.setCurrentRow(1)
    window._switch_page(window.SETTINGS_PAGE_INDEX)
    window.show()
    QApplication.processEvents()
    start = Mock()
    monkeypatch.setattr(window, "_start_workflow", start)
    QTest.mouseClick(page.start_hotkey.line_edit, Qt.MouseButton.LeftButton)
    assert page.start_hotkey.recording
    assert window._hotkeys.suspended
    window._hotkeys.activated.emit("start")
    start.assert_not_called()
    page.save_values()
    assert "结束录制" in page.save_result.text()
    assert repository.load_hotkeys() == {"start": "", "stop": ""}
    page._cancel()
    assert not page.start_hotkey.recording
    assert not window._hotkeys.suspended


def test_execution_settings_visual_at_small_window(window):
    window.settings_page.categories.setCurrentRow(1)
    window._switch_page(window.SETTINGS_PAGE_INDEX)
    window.resize(1180, 720)
    window.show()
    QApplication.processEvents()
    page = window.settings_page
    for editor in (page.start_hotkey, page.stop_hotkey):
        assert editor.isVisible()
        assert editor.width() > 100
    folder = Path(".pytest_tmp/gui-hotkey-integration/visual")
    folder.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(folder / "execution-settings.png"))


def test_invalid_stored_text_does_not_prevent_settings_from_opening(repository):
    repository.save_hotkeys("Ctrl+K, Ctrl+C", "Ctrl+F9")
    page = SettingsHubPage(repository)
    assert page.start_hotkey.shortcut() == ""
    assert page.stop_hotkey.shortcut() == "Ctrl+F9"
    assert "无效" in page.hotkey_status.text()
    assert repository.load_hotkeys()["start"] == "Ctrl+K, Ctrl+C"
    page.close()
