"""Settings and battle switches keep their existing contracts."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QCheckBox, QScrollArea, QVBoxLayout, QWidget

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.style import APP_STYLE
from packages.resonance_gui.widgets.battle_page import BattlePage
from packages.resonance_gui.widgets.player_data_panel import PlayerDataPanel
from packages.resonance_gui.widgets.settings_hub_page import SettingsHubPage
from packages.resonance_gui.widgets.toggle_button import ToggleButton


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = ResonanceConfigRepository(QSettings(str(tmp_path / "toggles.ini"), QSettings.IniFormat))
    widget = QWidget()
    widget._settings = settings
    yield widget
    widget.close()
    app.processEvents()


def test_settings_and_player_data_have_buttons_not_native_checkboxes(page):
    settings = page._settings
    for widget in (SettingsHubPage(settings), PlayerDataPanel(settings), BattlePage(settings)):
        assert widget.findChildren(QCheckBox) == []
        switches = widget.findChildren(ToggleButton)
        assert switches
        assert all(button.text().strip() and button.toolTip().strip() for button in switches)
        widget.close()


def test_battle_stop_rule_is_fixed_and_weekly_snapshot_round_trips(page):
    battle = BattlePage(page._settings)
    battle.set_inputs({"jobs": [battle._job_from_controls()], "stop_on_failure": False})
    assert "战败" in battle.stop_rule_label.text()
    assert "体力不足" in battle.stop_rule_label.text()
    assert not hasattr(battle, "stop_on_failure")
    inputs = battle.collect_inputs()
    assert set(inputs) == {"weekly_plan"}
    page._settings.save_battle_inputs(inputs)
    restored = BattlePage(page._settings)
    assert restored.collect_inputs() == inputs
    restored.close()
    battle.close()
