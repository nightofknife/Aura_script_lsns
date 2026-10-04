"""The single actual parameter editor feeds navigation and task snapshots."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    families = []
    for name in ("msyh.ttc", "msyhbd.ttc"):
        font_id = QFontDatabase.addApplicationFont(f"C:/Windows/Fonts/{name}")
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    if families:
        app.setFont(QFont(families[0], 10))
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    repository = ResonanceConfigRepository(QSettings(str(tmp_path / "sync.ini"), QSettings.IniFormat))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    yield widget
    widget._workflow_active = False
    widget._close_ready = True
    widget.close()
    app.processEvents()


def test_single_parameter_components_survive_navigation_without_collecting(window, monkeypatch):
    workflow, trade, passenger = window.workflow_page, window.trade_page, window.passenger_page
    assert workflow.trade_editor_page.isAncestorOf(trade.parameter_panel)
    assert workflow.passenger_editor_page.isAncestorOf(passenger.parameter_panel)
    assert workflow._task_config_pages["battle"] is window.battle_page
    for page in (trade, passenger):
        monkeypatch.setattr(page, "collect_inputs", lambda: pytest.fail("navigation collected inputs"))
    trade.fatigue_budget.setValue(333)
    passenger.trip_count.setValue(3)
    for task in ("trade", "passenger", "battle", "trade"):
        workflow._select_task(task)
        window._switch_page(window.SETTINGS_PAGE_INDEX)
        window._switch_page(window.WORKFLOW_PAGE_INDEX)
    assert trade.fatigue_budget.value() == 333
    assert passenger.trip_count.value() == 3
    assert not hasattr(workflow, "trade_fatigue")
    assert not hasattr(workflow, "passenger_trips")


def test_passenger_endpoint_buttons_keep_corrected_pair_and_estimates(window):
    passenger = window.passenger_page
    for endpoint, changed, other in (("A", passenger.city_a, passenger.city_b),
                                     ("B", passenger.city_b, passenger.city_a)):
        desired = other.currentData()
        passenger.endpoint_buttons[endpoint][desired].click()
        assert changed.currentData() == desired
        assert passenger.city_a.currentData() != passenger.city_b.currentData()
        for key, combo in (("A", passenger.city_a), ("B", passenger.city_b)):
            assert [city for city, button in passenger.endpoint_buttons[key].items() if button.isChecked()] == [combo.currentData()]
    passenger.trip_count.setValue(3)
    assert str(passenger._current_route_estimate().trip_fatigue * 3) in passenger.expected_fatigue.text()


@pytest.mark.parametrize("kind", ["trade", "passenger"])
def test_workflow_snapshot_uses_latest_single_editor_values(window, monkeypatch, kind):
    for key, check in window.workflow_page._task_checks.items():
        check.setChecked(key == kind)
    if kind == "trade":
        window.trade_page.fatigue_budget.setValue(333)
        window.trade_page.cargo_capacity.setValue(1000)
        window.trade_page.auto_cape_island_investment.setChecked(False)
        expected = {"fatigue_budget": 333, "cargo_capacity": 1000, "auto_cape_island_investment": False}
    else:
        window.passenger_page.trip_count.setValue(3)
        window.passenger_page.trade_during_trip.setChecked(False)
        expected = {"trip_count": 3, "trade_during_trip": False}
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._start_workflow()
    assert len(window._workflow_pending) == 1
    task = window._workflow_pending[0]
    assert task["step"] == task["dispatch"] == kind
    assert {key: task["inputs"][key] for key in expected} == expected
    saved = window._settings.load_trade_inputs() if kind == "trade" else window._settings.load_passenger_inputs()
    assert {key: saved[key] for key in expected} == expected
