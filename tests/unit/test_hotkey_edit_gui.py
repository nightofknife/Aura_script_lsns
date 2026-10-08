"""Recording is an explicit click transaction, not a timed key-sequence edit."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from packages.resonance_gui.widgets.hotkey_edit import HotkeyEdit


@pytest.fixture
def editor():
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    layout = QVBoxLayout(host)
    value = HotkeyEdit(host)
    other = QLineEdit(host)
    layout.addWidget(value)
    layout.addWidget(other)
    host.show()
    app.processEvents()
    yield value, other, host
    host.close()
    app.processEvents()


def click(value):
    QTest.mouseClick(value.line_edit, Qt.MouseButton.LeftButton)


def test_click_record_overwrite_release_and_click_commit(editor):
    value, _other, _host = editor
    value.set_shortcut("F8")
    states, changes = [], []
    value.recordingChanged.connect(states.append)
    value.shortcutChanged.connect(changes.append)
    click(value)
    assert value.recording
    assert value.line_edit.text() == "请按下快捷键"
    QTest.keyClick(value.line_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert value.recording and value.shortcut() == "F8"
    assert value.line_edit.text() == "Ctrl+S"
    QTest.keyClick(value.line_edit, Qt.Key.Key_F9)
    assert value.line_edit.text() == "F9"
    assert states == [True] and changes == []
    click(value)
    assert not value.recording
    assert value.shortcut() == "F9"
    assert states == [True, False] and changes == ["F9"]


def test_escape_no_key_and_explicit_cancel_keep_original(editor):
    value, _other, _host = editor
    value.set_shortcut("Ctrl+Alt+S")
    click(value)
    QTest.keyClick(value.line_edit, Qt.Key.Key_F9)
    QTest.keyClick(value.line_edit, Qt.Key.Key_Escape)
    assert not value.recording and value.shortcut() == "Ctrl+Alt+S"
    click(value)
    click(value)
    assert value.shortcut() == "Ctrl+Alt+S"
    click(value)
    value.cancel_recording()
    assert not value.recording and value.shortcut() == "Ctrl+Alt+S"


def test_modifier_only_and_autorepeat_do_not_replace_candidate(editor):
    value, _other, _host = editor
    value.set_shortcut("F8")
    click(value)
    QTest.keyClick(value.line_edit, Qt.Key.Key_Control)
    assert value.line_edit.text() == "请按下快捷键"
    QTest.keyClick(value.line_edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    before = value.line_edit.text()
    repeat = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier, "b", True)
    QApplication.sendEvent(value.line_edit, repeat)
    assert value.line_edit.text() == before == "Ctrl+Alt+A"
    click(value)
    assert value.shortcut() == before


def test_focus_loss_and_hide_cancel_without_committing(editor):
    value, other, host = editor
    value.set_shortcut("F8")
    click(value)
    QTest.keyClick(value.line_edit, Qt.Key.Key_F9)
    other.setFocus()
    QApplication.processEvents()
    assert not value.recording and value.shortcut() == "F8"
    click(value)
    QTest.keyClick(value.line_edit, Qt.Key.Key_F10)
    host.hide()
    assert not value.recording and value.shortcut() == "F8"


def test_clear_control_and_disabled_recording(editor):
    value, _other, _host = editor
    value.set_shortcut("f8")
    changes = []
    value.shortcutChanged.connect(changes.append)
    QTest.mouseClick(value.clear_button, Qt.MouseButton.LeftButton)
    assert value.shortcut() == "" and changes == [""]
    value.setEnabled(False)
    click(value)
    assert not value.recording


def test_tab_is_recorded_not_focus_navigation(editor):
    value, other, _host = editor
    click(value)
    QTest.keyClick(value.line_edit, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    assert value.recording and value.line_edit.text() == "Ctrl+Tab"
    assert not other.hasFocus()
    click(value)
    assert value.shortcut() == "Ctrl+Tab"


def test_recording_has_no_automatic_idle_timeout(editor):
    value, _other, _host = editor
    click(value)
    QTest.keyClick(value.line_edit, Qt.Key.Key_F8)
    QTest.qWait(1300)
    assert value.recording and value.shortcut() == ""
    click(value)
    assert value.shortcut() == "F8"


def test_container_click_supports_same_recording_contract(editor):
    value, _other, _host = editor
    QTest.mouseClick(value, Qt.MouseButton.LeftButton)
    assert value.recording and value.line_edit.hasFocus()
    QTest.keyClick(value.line_edit, Qt.Key.Key_F8)
    QTest.mouseClick(value, Qt.MouseButton.LeftButton)
    assert not value.recording and value.shortcut() == "F8"
