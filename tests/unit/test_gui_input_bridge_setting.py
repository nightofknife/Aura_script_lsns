"""GUI input bridge opt-in without starting a game or worker process."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.aura_game import SubprocessGameRunner
from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.settings_hub_page import SettingsHubPage
from plans.resonance_pc.src.services.input_bridge_service import ResonancePcInputBridgeService


@pytest.fixture
def repository(tmp_path):
    app = QApplication.instance() or QApplication(["input-bridge-setting-test"])
    yield ResonanceConfigRepository(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    app.processEvents()


def test_checkbox_defaults_warning_and_persistence(repository, monkeypatch):
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    page = SettingsHubPage(repository)
    assert not page.use_input_bridge.isChecked()
    assert page.use_input_bridge.text() == "使用桥接输入器"
    page.use_input_bridge.click()
    warning.assert_called_once_with(page, "风险提示", "有违规风险，自行使用")
    assert not repository.value("game/use_input_bridge", False)
    page.save_values()
    restored = SettingsHubPage(repository)
    assert restored.use_input_bridge.isChecked()
    assert warning.call_count == 1
    restored.use_input_bridge.click()
    restored.save_values()
    assert not repository.value("game/use_input_bridge", False)
    assert warning.call_count == 1


def test_main_window_sync_locks_during_work(repository):
    window = ResonanceMainWindow(settings=repository, initialize_on_startup=False, update_checker=lambda: "")
    try:
        received = []
        window.requestInputBridgeEnabled.connect(received.append)
        window._sync_input_bridge_setting()
        assert received == [False]
        repository.set_value("game/use_input_bridge", "true")
        window._sync_input_bridge_setting()
        assert received[-1] is True
        for flag in ("_busy", "_workflow_active", "_commerce_active"):
            setattr(window, flag, True)
            count = len(received)
            window._sync_input_bridge_setting()
            assert not window.settings_page.use_input_bridge.isEnabled()
            assert len(received) == count
            setattr(window, flag, False)
            window._sync_input_bridge_setting()
            assert window.settings_page.use_input_bridge.isEnabled()
    finally:
        window.close()


def test_runner_explicit_opt_in_rebuilds_only_between_tasks(repository):
    bridge = RunnerBridge(runner_factory=lambda: SubprocessGameRunner(env_overrides={"AURA_GUI_INPUT_BRIDGE": "1"}))
    first = bridge._runner_instance()
    assert first.env_overrides["AURA_GUI_INPUT_BRIDGE"] == "0"
    first.close = Mock()
    bridge.set_input_bridge_enabled(True)
    assert bridge._runner_instance() is first
    first.close.assert_not_called()
    second = bridge._task_runner_instance()
    first.close.assert_called_once()
    assert second is not first
    assert second.env_overrides["AURA_GUI_INPUT_BRIDGE"] == "1"
    assert bridge._task_runner_instance() is second
    second.close = Mock()
    bridge.set_input_bridge_enabled(False)
    third = bridge._task_runner_instance()
    second.close.assert_called_once()
    assert third.env_overrides["AURA_GUI_INPUT_BRIDGE"] == "0"
    third.close()


@pytest.mark.parametrize("config,override,expected", [
    ("system", None, False), ("bridge", None, True),
    ("bridge", "0", False), ("system", "1", True),
    ("bridge", "invalid", False),
])
def test_service_gui_override_wins_over_plan_config(monkeypatch, config, override, expected):
    if override is None:
        monkeypatch.delenv("AURA_GUI_INPUT_BRIDGE", raising=False)
    else:
        monkeypatch.setenv("AURA_GUI_INPUT_BRIDGE", override)
    screen = SimpleNamespace(target_runtime=SimpleNamespace(config={"resonance_pc.input.mode": config}))
    service = ResonancePcInputBridgeService(screen)
    assert service.enabled is expected
