"""Recovery defaults preserve bento type order and ignore obsolete resource order."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository, TRADE_INPUTS_KEY
from packages.resonance_gui.widgets.trade_page import TradePage
from packages.resonance_shared.freight_parameters import (
    DEFAULT_RECOVERY_THRESHOLDS, effective_freight_inputs, validate_freight_recovery_thresholds,
)


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    widget = TradePage(ResonanceConfigRepository(QSettings(str(tmp_path / "defaults.ini"), QSettings.Format.IniFormat)))
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_defaults_allow_enabling_either_resource_without_filling_thresholds(page):
    assert {key: getattr(page, key).value() for key in DEFAULT_RECOVERY_THRESHOLDS} == DEFAULT_RECOVERY_THRESHOLDS
    assert not hasattr(page, "recovery_priority")
    assert not hasattr(page, "recovery_priority_panel")
    page.auto_sparkling_water.setChecked(True)
    page.auto_bento.setChecked(True)
    runtime = page.collect_task_inputs()
    assert "recovery_priority" not in runtime
    assert all(runtime[key] == value for key, value in DEFAULT_RECOVERY_THRESHOLDS.items())


@pytest.mark.parametrize("priority", [["bento", "sparkling_water"], None, [], ["bento"],
                                      ["bento", "bento"], "invalid", {"bento": 1}])
def test_obsolete_order_is_ignored_by_repository_gui_and_runtime_without_changing_food_order(page, priority):
    old = {"auto_sparkling_water": True, "auto_bento": True, "recovery_priority": priority,
           "bento_priority": ["love_bentos", "work_meals"]}
    page._settings.settings.setValue(TRADE_INPUTS_KEY, json.dumps(old))
    loaded = page._settings.load_trade_inputs()
    assert "recovery_priority" not in loaded
    assert loaded["bento_priority"] == ["love_bentos", "work_meals"]
    page.set_inputs(old)
    assert page.collect_task_inputs()["bento_priority"] == ["love_bentos", "work_meals"]
    assert "recovery_priority" not in page.collect_ui_state()
    assert "recovery_priority" not in page.collect_task_inputs()
    assert "recovery_priority" not in effective_freight_inputs(old)
    assert old["recovery_priority"] == priority
    page.save_ui_state()
    saved = page._settings.load_trade_inputs()
    assert saved["bento_priority"] == ["love_bentos", "work_meals"]
    assert "recovery_priority" not in saved


def test_obsolete_order_never_becomes_food_type_order(page):
    page.set_inputs({"auto_bento": True, "recovery_priority": ["bento", "sparkling_water"]})
    assert page.collect_task_inputs()["bento_priority"] == ["work_meals", "love_bentos"]


def test_existing_custom_and_unset_values_are_never_overwritten(page):
    custom = {"auto_sparkling_water": True, "sparkling_water_start_threshold": None,
              "sparkling_water_stop_threshold": 40, "bento_start_threshold": 850,
              "bento_stop_threshold": 180}
    page._settings.save_trade_inputs(custom)
    page.set_inputs(page._settings.load_trade_inputs())
    assert page.sparkling_water_start_threshold.value() == -1
    assert page.bento_start_threshold.value() == 850
    assert page.bento_stop_threshold.value() == 180
    with pytest.raises(ValueError, match="气泡水"):
        page.collect_task_inputs()


def test_threshold_validation_uses_new_missing_defaults_but_keeps_explicit_unset_invalid():
    validate_freight_recovery_thresholds({"auto_sparkling_water": True, "auto_bento": True})
    for key in DEFAULT_RECOVERY_THRESHOLDS:
        with pytest.raises(ValueError):
            validate_freight_recovery_thresholds({"auto_sparkling_water": True, "auto_bento": True, key: None})


def test_inactive_obsolete_order_is_always_removed_without_mutating_input():
    source = {"auto_bento": False, "auto_sparkling_water": False, "recovery_priority": "invalid"}
    assert "recovery_priority" not in effective_freight_inputs(source)
    assert source["recovery_priority"] == "invalid"
