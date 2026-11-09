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
    workflow, trade = window.workflow_page, window.trade_page
    assert workflow.trade_editor_page.isAncestorOf(trade.parameter_panel)
    assert workflow._task_config_pages["battle"].isAncestorOf(window.battle_page)
    monkeypatch.setattr(trade, "collect_inputs", lambda: pytest.fail("navigation collected inputs"))
    trade.fatigue_budget.setValue(333)
    for task in ("trade", "battle", "trade"):
        workflow._select_task(task)
        window._switch_page("settings")
        window._switch_page("workflow")
    assert trade.fatigue_budget.value() == 333
    assert not hasattr(workflow, "trade_fatigue")
    assert not hasattr(window, "passenger_page")




def test_workflow_snapshot_uses_latest_single_editor_values(window, monkeypatch):
    for key, check in window.workflow_page._task_checks.items():
        check.setChecked(key == "trade")
    window.trade_page.fatigue_budget.setValue(333)
    window.trade_page.cargo_capacity.setValue(1000)
    window.trade_page.auto_cape_island_investment.setChecked(False)
    expected = {"fatigue_budget": 333, "cargo_capacity": 1000, "auto_cape_island_investment": False}
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._start_workflow()
    assert [row["step"] for row in window._workflow_pending] == ["trade"]
    assert "refresh_recovery" not in window.workflow_page._tree_items
    task = window._workflow_pending[-1]
    assert task["step"] == task["dispatch"] == "trade"
    assert {key: task["inputs"][key] for key in expected} == expected
    saved = window._settings.load_trade_inputs()
    assert {key: saved[key] for key in expected} == expected


def test_restoring_freight_draft_does_not_autosave_partial_state(window, monkeypatch):
    values = window.trade_page.collect_ui_state()
    values.update(auto_bento=True, bento_priority=["love_bentos"], auto_sparkling_water=True,
                  auto_pickup=True, auto_pickup_interval_sec=3.25)
    monkeypatch.setattr(window._settings, "save_trade_inputs", lambda *_: pytest.fail("restore must not autosave"))
    window.trade_page.set_inputs(values)
    assert window.trade_page.auto_bento.isChecked()
    assert window.trade_page.auto_pickup_interval_sec.value() == 3.25
    assert window.trade_page._selected_bento_priority() == ["love_bentos"]


def test_workflow_run_respects_freight_error_only_when_freight_selected(window):
    trade, workflow = window.trade_page, window.workflow_page
    trade.set_inputs({"available_city_ids": ["1", "2", "11"], "required_end_city_ids": ["11"]})
    assert workflow.run_button.isEnabled()
    trade.city_checks["11"].setChecked(False)
    assert not workflow.run_button.isEnabled()
    assert "终点" in workflow.run_button.toolTip()
    workflow._task_checks["trade"].setChecked(False)
    assert workflow.run_button.isEnabled()
    workflow._task_checks["trade"].setChecked(True)
    assert not workflow.run_button.isEnabled()
    # A validation change must never disable the active Stop button.
    workflow.begin_workflow(["trade"], {})
    assert workflow.run_button.isEnabled()
    workflow.finish_workflow(success=False, message="offline test")
    assert not workflow.run_button.isEnabled()
    trade.city_checks["11"].setChecked(True)
    assert workflow.run_button.isEnabled()
