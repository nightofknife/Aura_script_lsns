"""Smoke checks for optional PC trade end-city selection."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets.trade_page import TradePage
from packages.resonance_gui.widgets.workflow_page import WorkflowPage


def _application() -> QApplication:
    return QApplication.instance() or QApplication(["aura-trade-page-smoke"])


def test_trade_page_end_city_defaults_to_no_constraint(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "trade.ini"), QSettings.Format.IniFormat)
    page = TradePage(ResonanceConfigRepository(settings))

    assert page.end_city.itemText(0) == "否"
    assert page.end_city.itemData(0) == ""
    assert page.end_city_notice.isHidden()

    inputs = page.collect_inputs()
    assert inputs["required_end_city_ids"] is None


def test_trade_page_end_city_selection_tracks_available_cities(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "trade.ini"), QSettings.Format.IniFormat)
    page = TradePage(ResonanceConfigRepository(settings))

    page.set_inputs(
        {
            "available_city_ids": ["1", "2", "11"],
            "required_end_city_ids": ["11"],
        }
    )

    assert page.end_city.currentData() == "11"
    assert page.collect_inputs()["required_end_city_ids"] == ["11"]

    page.set_inputs({"available_city_ids": ["1", "2"], "required_end_city_ids": None})

    assert page.end_city.currentData() == ""
    assert page.collect_inputs()["required_end_city_ids"] is None

    page.set_inputs(
        {
            "available_city_ids": ["1", "2", "11"],
            "required_end_city_ids": ["11"],
        }
    )
    page.city_checks["11"].setChecked(False)

    assert page.end_city.findData("11") == -1
    assert page.end_city.currentData() == ""
    assert page.collect_inputs()["required_end_city_ids"] is None


def test_trade_inputs_drop_end_city_outside_available_cities(tmp_path) -> None:
    settings = QSettings(str(tmp_path / "trade.ini"), QSettings.Format.IniFormat)
    repository = ResonanceConfigRepository(settings)

    repository.save_trade_inputs(
        {
            "available_city_ids": ["1", "2"],
            "required_end_city_ids": ["11"],
        }
    )

    loaded = repository.load_trade_inputs()
    assert loaded["available_city_ids"] == ["1", "2"]
    assert loaded["required_end_city_ids"] is None


def test_trade_end_city_availability_tracks_combined_workflow_order(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "workflow.ini"), QSettings.Format.IniFormat)
    repository = ResonanceConfigRepository(settings)
    page = TradePage(repository)
    workflow = WorkflowPage(repository)
    workflow.tradeEndCityAvailabilityChanged.connect(page.set_end_city_constraint_available)
    page.set_inputs(
        {
            "available_city_ids": ["1", "2", "11"],
            "required_end_city_ids": ["11"],
        }
    )

    workflow._refresh_combined_summary()
    assert workflow.commerce_steps() == ["trade", "passenger"]
    assert not page.end_city.isEnabled()
    assert not page.end_city_notice.isHidden()
    assert page.end_city.currentData() == "11"
    page.set_busy(True)
    page.set_busy(False)
    assert not page.end_city.isEnabled()

    workflow._swap_commerce_order()
    assert workflow.commerce_steps() == ["passenger", "trade"]
    assert page.end_city.isEnabled()
    assert page.end_city_notice.isHidden()
    assert page.end_city.currentData() == "11"

    workflow._commerce_checks["passenger"].setChecked(False)
    assert workflow.commerce_steps() == ["trade"]
    assert page.end_city.isEnabled()
