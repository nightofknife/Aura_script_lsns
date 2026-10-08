"""Button switches preserve checkbox state semantics and delayed help."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QToolTip, QWidget

from packages.resonance_gui.style import APP_STYLE
from packages.resonance_gui.widgets.toggle_button import ToggleButton


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_toggle_button_checkbox_api_and_blocked_signals(app):
    button = ToggleButton("自动前往线路")
    assert isinstance(button, QPushButton)
    assert button.isCheckable()
    assert button.text() == "自动前往线路"
    assert button.property("toggleButton") is True
    states, toggles = [], []
    button.stateChanged.connect(states.append)
    button.toggled.connect(toggles.append)
    button.setChecked(True)
    button.setChecked(True)
    assert button.checkState() == Qt.CheckState.Checked
    button.click()
    assert states == [2, 0]
    assert toggles == [True, False]
    button.blockSignals(True)
    button.setCheckState(Qt.CheckState.Checked)
    button.blockSignals(False)
    assert button.isChecked()
    assert not button.icon().isNull()
    assert states == [2, 0]
    button.setCheckState(0)
    assert button.icon().isNull()
    assert states == [2, 0, 0]
    parent = QWidget()
    assert ToggleButton(parent).parent() is parent


def test_keyboard_and_disabled_clicks_preserve_boolean_state(app):
    button = ToggleButton("失败即停止")
    button.show()
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert button.isChecked()
    button.setEnabled(False)
    button.click()
    assert button.isChecked()
    button.close()


def test_tooltip_waits_700ms_and_cancels_on_leave(app, monkeypatch):
    button = ToggleButton("自动启动游戏")
    button.setToolTip("游戏未运行时，使用已保存的程序路径启动游戏")
    button.show()
    app.processEvents()
    monkeypatch.setattr(button, "underMouse", lambda: True)
    shown = []
    monkeypatch.setattr(QToolTip, "showText", lambda *args: shown.append(args[1]))
    enter = lambda: QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1))
    QApplication.sendEvent(button, enter())
    assert button._tooltip_timer.interval() == 700
    QTest.qWait(200)
    assert shown == []
    QTest.qWait(580)
    assert shown == [button.toolTip()]
    QApplication.sendEvent(button, enter())
    QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    assert not button._tooltip_timer.isActive()
    button.close()


def test_toggle_style_has_readable_selected_and_disabled_surfaces():
    assert 'QPushButton[toggleButton="true"]:checked' in APP_STYLE
    assert 'QPushButton[toggleButton="true"]:checked:disabled' in APP_STYLE
    assert 'QPushButton[toggleButton="true"]:checked:hover' in APP_STYLE
