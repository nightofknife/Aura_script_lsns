"""Cross-page edits must agree with the snapshot sent to a workflow."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    repository = ResonanceConfigRepository(QSettings(str(tmp_path / "sync.ini"), QSettings.IniFormat))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    yield widget
    widget._close_ready = True
    widget.close()
    app.processEvents()


def test_duplicate_parameters_sync_both_ways_without_collecting(window, monkeypatch):
    quick, trade, passenger = window.workflow_page, window.trade_page, window.passenger_page
    for page in (trade, passenger):
        monkeypatch.setattr(page, "collect_inputs", lambda: pytest.fail("page switch collected inputs"))
    for summary, editor in (
        (quick.trade_fatigue, trade.fatigue_budget),
        (quick.trade_cargo, trade.cargo_capacity),
        (quick.trade_books, trade.book_budget),
        (quick.passenger_trips, passenger.trip_count),
    ):
        summary.setValue(12)
        assert editor.value() == 12
        editor.setValue(19)
        quick.show_commerce_summary()
        assert summary.value() == 19
    for summary, editor in (
        (quick.trade_investment, trade.auto_cape_island_investment),
        (quick.trade_rubbish_recycling, trade.auto_rubbish_recycling),
        (quick.trade_sparkling_water, trade.auto_sparkling_water),
        (quick.trade_auto_bento, trade.auto_bento),
        (quick.trade_auto_pickup, trade.auto_pickup),
        (quick.passenger_trade, passenger.trade_during_trip),
        (quick.passenger_reposition, passenger.auto_reposition),
    ):
        summary.setChecked(False)
        assert not editor.isChecked()
        editor.setChecked(True)
        assert summary.isChecked()


def test_passenger_endpoint_collision_syncs_final_pair_and_estimates(window):
    quick, passenger = window.workflow_page, window.passenger_page
    pairs = [(quick.passenger_city_a, quick.passenger_city_b), (passenger.city_a, passenger.city_b)]
    for source, target in (pairs, pairs[::-1]):
        # Selecting the other endpoint triggers the page's automatic correction.
        for index in (0, 1, 0):
            desired = source[1 - index].currentData()
            source[index].setCurrentIndex(source[index].findData(desired))
            assert source[index].currentData() == desired
            assert source[0].currentData() != source[1].currentData()
            assert [box.currentData() for box in source] == [box.currentData() for box in target]
    passenger.trip_count.setValue(3)
    assert quick.passenger_route_fatigue() == passenger._current_route_estimate().trip_fatigue * 3


@pytest.mark.parametrize("kind", ["trade", "passenger"])
def test_workflow_snapshot_uses_latest_edits_from_both_pages(window, monkeypatch, kind):
    quick = window.workflow_page
    for key, check in quick._task_checks.items():
        check.setChecked(key == "commerce")
    for key, check in quick._commerce_checks.items():
        check.setChecked(key == kind)
    if kind == "trade":
        quick.trade_fatigue.setValue(333)
        window.trade_page.cargo_capacity.setValue(1000)
        window.trade_page.auto_cape_island_investment.setChecked(False)
        expected = {"fatigue_budget": 333, "cargo_capacity": 1000, "auto_cape_island_investment": False}
    else:
        quick.passenger_trips.setValue(3)
        window.passenger_page.trade_during_trip.setChecked(False)
        expected = {"trip_count": 3, "trade_during_trip": False}
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._start_workflow()
    task = next(row for row in window._workflow_pending if row.get("step") == kind)
    assert {key: task["inputs"][key] for key in expected} == expected
    saved = window._settings.load_trade_inputs() if kind == "trade" else window._settings.load_passenger_inputs()
    assert {key: saved[key] for key in expected} == expected
    window._workflow_active = False
