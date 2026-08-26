"""Smoke checks for optional PC trade end-city selection."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets.trade_page import TradePage


def _application() -> QApplication:
    return QApplication.instance() or QApplication(["aura-trade-page-smoke"])


def test_trade_page_end_city_defaults_to_no_constraint(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "trade.ini"), QSettings.Format.IniFormat)
    page = TradePage(ResonanceConfigRepository(settings))

    assert page.end_city.itemText(0) == "否"
    assert page.end_city.itemData(0) == ""

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
