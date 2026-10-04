"""Passenger city buttons and boolean options keep their existing contracts."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QCheckBox, QScrollArea

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.style import APP_STYLE
from packages.resonance_gui.widgets.battle_page import BattlePage
from packages.resonance_gui.widgets.passenger_page import PassengerPage
from packages.resonance_gui.widgets.player_data_panel import PlayerDataPanel
from packages.resonance_gui.widgets.settings_hub_page import SettingsHubPage
from packages.resonance_gui.widgets.toggle_button import ToggleButton


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = ResonanceConfigRepository(QSettings(str(tmp_path / "toggles.ini"), QSettings.IniFormat))
    widget = PassengerPage(settings)
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    if font_id >= 0:
        widget.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    widget.setStyleSheet(APP_STYLE)
    yield widget
    widget.close()
    app.processEvents()


def assert_endpoints_match(page):
    for endpoint, combo in (("A", page.city_a), ("B", page.city_b)):
        selected = [city_id for city_id, button in page.endpoint_buttons[endpoint].items() if button.isChecked()]
        assert selected == [combo.currentData()]
    assert page.city_a.currentData() != page.city_b.currentData()


def test_endpoint_buttons_keep_collision_correction_and_combo_bindings(page):
    assert isinstance(page.parameter_panel, QScrollArea)
    assert_endpoints_match(page)
    for endpoint, combo, other in (("A", page.city_a, page.city_b), ("B", page.city_b, page.city_a)):
        desired = other.currentData()
        page.endpoint_buttons[endpoint][desired].click()
        assert combo.currentData() == desired
        assert_endpoints_match(page)
        # Shared workflow bindings can still edit the original state adapter.
        combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
        assert_endpoints_match(page)
    page.set_busy(True)
    assert all(not button.isEnabled() for choices in page.endpoint_buttons.values() for button in choices.values())
    page.set_busy(False)
    assert all(button.isEnabled() for choices in page.endpoint_buttons.values() for button in choices.values())


def test_option_text_tooltips_and_snapshot_round_trip(page):
    assert isinstance(page.trade_during_trip, ToggleButton)
    assert page.trade_during_trip.text() == "中途买卖货"
    assert page.auto_reposition.text() == "自动前往线路"
    assert "先卖后买" in page.trade_during_trip.toolTip()
    assert "疲劳消耗较低" in page.auto_reposition.toolTip()
    page.set_inputs({"passenger_city_a_id": "2", "passenger_city_b_id": "3", "trip_count": 4,
                     "trade_during_trip": False, "reposition_to_route": False})
    assert_endpoints_match(page)
    inputs = page.collect_inputs()
    assert inputs["passenger_city_a_id"] == "2"
    assert inputs["passenger_city_b_id"] == "3"
    assert inputs["trip_count"] == 4
    assert not inputs["trade_during_trip"]
    assert not inputs["reposition_to_route"]
    page.trade_during_trip.click()
    assert page.collect_inputs()["trade_during_trip"]


def test_settings_and_player_data_have_buttons_not_native_checkboxes(page):
    settings = page._settings
    for widget in (SettingsHubPage(settings), PlayerDataPanel(settings), BattlePage(settings)):
        assert widget.findChildren(QCheckBox) == []
        switches = widget.findChildren(ToggleButton)
        assert switches
        assert all(button.text().strip() and button.toolTip().strip() for button in switches)
        widget.close()


def test_battle_failure_button_snapshot_round_trip(page):
    battle = BattlePage(page._settings)
    battle.set_inputs({"jobs": [battle._job_from_controls()], "stop_on_failure": False})
    assert not battle.collect_inputs()["stop_on_failure"]
    assert battle.stop_on_failure.text() == "失败即停止"
    battle.stop_on_failure.click()
    assert battle.collect_inputs()["stop_on_failure"]
    assert page._settings.load_battle_inputs()["stop_on_failure"]
    battle.close()


def test_passenger_button_grid_render_snapshot(page, tmp_path):
    page.resize(1220, 820)
    page.show()
    page.endpoint_selector_buttons["A"].click()
    QApplication.processEvents()
    buttons = list(page.endpoint_buttons["A"].values())
    assert all(button.width() >= button.minimumSizeHint().width() for button in buttons)
    for row in range((len(buttons) - 1) // 3):
        assert buttons[row * 3].geometry().bottom() < buttons[(row + 1) * 3].geometry().top()
    assert page.grab().save(str(tmp_path / "passenger-button-grid.png"))


def test_endpoint_choices_are_collapsed_until_selected(page):
    page.resize(1180, 720)
    page.show()
    QApplication.processEvents()
    assert all(panel.isHidden() for panel in page.endpoint_panels.values())
    assert "往返" in page.route_title.text()
    assert page.trip_count.isVisible() and page.trade_during_trip.isVisible() and page.auto_reposition.isVisible()
    page.endpoint_selector_buttons["B"].click()
    assert not page.endpoint_panels["B"].isHidden()
    desired = next(city for city in page.endpoint_buttons["B"] if city != page.city_a.currentData())
    page.endpoint_buttons["B"][desired].click()
    assert page.city_b.currentData() == desired
    assert desired in page.endpoint_buttons["B"]
    assert page.endpoint_panels["B"].isHidden()
    assert page.city_b.currentText() in page.endpoint_selector_buttons["B"].text()
