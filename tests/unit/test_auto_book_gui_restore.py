"""Auto Book GUI contracts, using offscreen widgets and recording runners only."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository, TRADE_PREVIEW_INPUT_KEYS
from packages.resonance_gui.logic import (
    PC_TRADE_PREVIEW_TASK_REF, PC_TRADE_TASK_REF, PC_COMBINED_COMMERCE_TASK_REF,
    average_book_profit_text, normalize_trade_task_inputs, trade_result_summary,
    TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA,
)
from packages.resonance_gui.main_window import ResonanceMainWindow


@pytest.fixture
def repository(tmp_path):
    return ResonanceConfigRepository(QSettings(str(tmp_path / "auto.ini"), QSettings.Format.IniFormat))


@pytest.fixture
def window(repository, monkeypatch):
    app = QApplication.instance() or QApplication([])
    if "Microsoft YaHei UI" not in QFontDatabase.families():
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    monkeypatch.setattr(widget.settings_page, "close_on_failure_enabled", lambda: False)
    yield widget
    widget._busy = False
    widget._finish_workflow(False, "test finished")
    widget._commerce_active = False
    widget.close()
    app.processEvents()


def test_defaults_seed_and_existing_preview_missing_key(repository):
    assert repository.load_trade_inputs()["auto_book"] is False
    assert repository.load_trade_inputs()["book_profit_threshold"] == 500000
    repository.save_trade_inputs({"auto_book": True, "book_budget": 17, "book_profit_threshold": 15000})
    preview = repository.load_trade_preview_inputs()
    assert preview["auto_book"] is True
    assert preview["book_budget"] == 17
    assert preview["book_profit_threshold"] == 15000
    assert set(preview) == set(TRADE_PREVIEW_INPUT_KEYS)
    del preview["auto_book"]
    repository.settings.setValue("trade_preview/inputs_json", json.dumps(preview))
    assert repository.load_trade_preview_inputs()["auto_book"] is False
    assert repository.load_trade_inputs()["auto_book"] is True
    assert repository.load_trade_inputs()["book_profit_threshold"] == 15000


@pytest.mark.parametrize("enabled", [False, True])
def test_normalize_is_payload_only(enabled):
    inputs = {"auto_book": enabled, "book_budget": 17, "auto_sparkling_water": True,
              "recovery_snapshot": {"sentinel": [1]}}
    before = deepcopy(inputs)
    output = normalize_trade_task_inputs(inputs)
    expected = deepcopy(before)
    if enabled:
        del expected["book_budget"]
    assert output == expected
    assert output is not inputs
    assert inputs == before
    assert normalize_trade_task_inputs({"book_budget": 4}) == {"auto_book": False, "book_budget": 4}


def test_three_entries_sync_persist_and_preview_independence(window):
    formal, quick, preview = window.trade_page, window.workflow_page, window.trade_preview_page
    formal.book_budget.setValue(17)
    formal.auto_book.setChecked(True)
    assert quick.trade_auto_book.isChecked()
    assert not formal.book_budget.isEnabled()
    assert not quick.trade_books.isEnabled()
    assert quick.trade_books.value() == 17
    assert not preview.auto_book.isChecked()
    assert window._settings.load_trade_inputs()["auto_book"] is True
    preview.book_budget.setValue(9)
    preview.auto_book.setChecked(True)
    quick.trade_auto_book.setChecked(False)
    assert not formal.auto_book.isChecked()
    assert formal.book_budget.value() == 17
    assert formal.book_budget.isEnabled()
    assert preview.auto_book.isChecked()
    assert preview.book_budget.value() == 9
    assert window._settings.load_trade_inputs()["auto_book"] is False
    assert window._settings.load_trade_inputs()["book_budget"] == 17
    assert window._settings.load_trade_preview_inputs()["auto_book"] is True
    assert window._settings.load_trade_preview_inputs()["book_budget"] == 9
    for check in (formal.auto_book, quick.trade_auto_book, preview.auto_book):
        assert check.text() == ""
        assert check.accessibleName() == "Auto Book 模式"
    formal.set_busy(True)
    formal.set_auto_book(False)
    assert not formal.book_budget.isEnabled()
    formal.set_busy(False)
    assert formal.book_budget.isEnabled()


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def run_task(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return {"status": "queued", "cid": "offline-auto-book"}


def recording_bridge(monkeypatch):
    runner = RecordingRunner()
    bridge = RunnerBridge(lambda: runner)
    monkeypatch.setattr(bridge, "_start_polling", lambda: None)
    return runner, bridge


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("entry", ["formal", "quick", "overview", "combined", "preview"])
def test_actual_dispatch_vs_stored_config(window, monkeypatch, enabled, entry):
    runner, bridge = recording_bridge(monkeypatch)
    window.requestRunPcTrade.connect(bridge.run_pc_trade)
    window.requestPreviewPcTrade.connect(bridge.preview_pc_trade)
    window.requestRunPcCombinedCommerce.connect(bridge.run_pc_combined_commerce)
    formal = window.trade_page
    formal.book_budget.setValue(17)
    formal.auto_book.setChecked(enabled)
    formal.auto_sparkling_water.setChecked(False)
    expected = formal.collect_inputs()
    if entry == "formal":
        formal._request_start()
    elif entry == "preview":
        page = window.trade_preview_page
        page.book_budget.setValue(17)
        page.auto_book.setChecked(enabled)
        page.start_city.setCurrentIndex(1)
        expected = page.collect_inputs(require_start_city=True)
        page._request_preview()
    elif entry == "quick":
        for key, check in window.workflow_page._task_checks.items():
            check.setChecked(key == "commerce")
        for key, check in window.workflow_page._commerce_checks.items():
            check.setChecked(key == "trade")
        window._start_workflow()
        assert runner.calls == []
        # Simulate successful mandatory refresh; no game or runner work is performed.
        snapshot = {
            "status": {"fatigue": {"current": 120, "max": 800}},
            "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6},
                         "bento": {"available_count": 2}},
            "metadata": {"persisted": True},
        }
        window._workflow_recovery_snapshot = {"player_data": snapshot}
        window._dispatch_next_workflow_task()
        expected["recovery_snapshot"] = deepcopy(snapshot)
        expected["recovery_snapshot"]["recovery"]["sparkling_water"]["requires_refresh"] = False
    else:
        window._start_commerce_sequence(True, entry == "combined")
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["wait"] is False
    assert call["task_ref"] == (PC_TRADE_PREVIEW_TASK_REF if entry == "preview" else
                                 PC_COMBINED_COMMERCE_TASK_REF if entry == "combined" else PC_TRADE_TASK_REF)
    actual = call["inputs"]["trade_inputs"] if entry == "combined" else call["inputs"]
    assert actual == normalize_trade_task_inputs(expected)
    saved = (window._settings.load_trade_preview_inputs() if entry == "preview"
             else window._settings.load_trade_inputs())
    assert saved["book_budget"] == 17
    assert saved["auto_book"] is enabled
    assert "recovery_snapshot" not in saved
    if entry == "quick":
        actual["recovery_snapshot"]["recovery"].clear()
        assert window._workflow_recovery_snapshot["player_data"]["recovery"]


def test_bridge_preview_removes_execution_only(window, monkeypatch):
    runner, bridge = recording_bridge(monkeypatch)
    window.trade_preview_page.start_city.setCurrentIndex(1)
    inputs = {**window.trade_preview_page.collect_inputs(), "auto_book": True, "book_budget": 5,
              "auto_sparkling_water": True, "recovery_snapshot": {"private": 1},
              "arrival_timeout_seconds": 600, "auto_rubbish_recycling": True}
    original = deepcopy(inputs)
    bridge.preview_pc_trade(inputs)
    expected = {key: value for key, value in original.items() if key in TRADE_PREVIEW_INPUT_KEYS}
    assert runner.calls[0]["inputs"] == normalize_trade_task_inputs(expected)
    assert inputs == original


@pytest.mark.parametrize("combined", [False, True])
def test_auto_book_actual_recovery_dispatch_keeps_projection(window, monkeypatch, combined):
    runner, bridge = recording_bridge(monkeypatch)
    snapshot = {
        "status": {"fatigue": {"current": 120, "max": 800}, "unrelated": 123},
        "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6},
                     "bento": {"available_count": 2}},
        "metadata": {"persisted": True, "unrelated": "private"},
    }
    trade = {"auto_book": True, "book_budget": 17, "auto_sparkling_water": True}
    source = ({"trade_inputs": trade, "passenger_inputs": {"sentinel": 1},
               "recovery_snapshot": snapshot} if combined else
              {**trade, "recovery_snapshot": snapshot})
    before = deepcopy(source)
    if combined:
        bridge.run_pc_combined_commerce(source)
    else:
        bridge.run_pc_trade(source)
    actual = runner.calls[0]["inputs"]
    dispatched_trade = actual["trade_inputs"] if combined else actual
    assert dispatched_trade["auto_book"] is True
    assert dispatched_trade["auto_sparkling_water"] is True
    assert "book_budget" not in dispatched_trade
    assert actual["recovery_snapshot"] == {
        "status": {"fatigue": {"current": 120, "max": 800}},
        "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6,
                                         "requires_refresh": False},
                     "bento": {"available_count": 2}},
        "metadata": {"persisted": True},
    }
    assert source == before


@pytest.mark.parametrize("books,value,visible", [
    (2, 500001.25, True), (2, 0, True), (0, 500001, False),
    (2, None, False), (2, "bad", False), (2, float("nan"), False),
    (2, float("inf"), False), (None, None, False), (2, True, False),
])
def test_optional_average_history_and_planning(window, books, value, visible):
    fields = {"auto_book": True, "book_budget_ignored": True, "book_profit_threshold": 500000,
              "books_budget": None, "remaining_books": None, "books_used": books,
              "book_incremental_profit": 1000002.5, "book_incremental_profit_exact": "2000005/2",
              "average_book_profit": value, "average_book_profit_exact": "2000005/4"}
    summary = trade_result_summary(fields)
    for key in fields:
        assert summary[key] is fields[key] or summary[key] == fields[key]
    for page in (window.trade_page, window.trade_preview_page):
        page.show_history_result(fields)
        assert page.result_values["average_book_profit"].isHidden() is not visible
        assert "剩余0" not in page.result_values["books"].text()
        assert "计划共" in page.result_values["books"].text()
        page._render_planning_summary(summary)
        assert page.result_captions["average_book_profit"].isHidden() is not visible
        page.show_history_result({"books_used": 0})
        assert page.result_values["average_book_profit"].isHidden()
    window.workflow_page._freight_progress.summary = summary
    window.workflow_page._render_runtime_trade_plan()
    assert window.workflow_page.runtime_average_book_profit.isHidden() is not visible
    assert (average_book_profit_text(summary) is not None) is visible


def planning_event(summary, sequence=1):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "offline-plan", "sequence": sequence,
        "stage": "planning", "state": "completed",
        "data": {"summary": summary, "route": [{
            "from_city": "修格里城", "to_city": "铁盟哨站", "books_used": 2,
            "expected_profit": 1500000, "expected_fatigue_cost": 20,
        }]},
    }}


def test_formal_planning_event_updates_actual_visible_workflow(window):
    page = window.workflow_page
    window.show()
    window._switch_page(window.WORKFLOW_PAGE_INDEX)
    page._busy = True
    page.set_active_progress_cid("trade", "offline-plan")
    page.apply_progress_event("trade", planning_event({"auto_book": True,
                              "books_used": 2, "average_book_profit": 600000}))
    QApplication.processEvents()
    assert page.center_stack.currentWidget() is page.runtime_trade_plan_page
    assert page.runtime_average_book_profit.isVisible()
    assert page.runtime_average_book_profit.text() == "平均每本进货书收益  600,000"
    for sequence, summary in enumerate(({"books_used": 0, "average_book_profit": 600000},
                                        {"books_used": 2}), 2):
        page.apply_progress_event("trade", planning_event(summary, sequence))
        assert not page.runtime_average_book_profit.isVisible()


def test_offscreen_entry_screenshots(window):
    output = Path.cwd() / ".pytest_tmp" / "autobook_gui"
    output.mkdir(parents=True, exist_ok=True)
    window.resize(1440, 1000)
    window.show()
    window.trade_page.auto_book.setChecked(True)
    window.trade_page.book_budget.setValue(17)
    window._switch_page(window.WORKFLOW_PAGE_INDEX)
    window.workflow_page._select_task("commerce")
    window.workflow_page.commerce_tabs.setCurrentIndex(0)
    QApplication.processEvents()
    assert window.grab().save(str(output / "quick.png"))
    window.workflow_page.show_trade_editor()
    QApplication.processEvents()
    assert window.grab().save(str(output / "formal.png"))
    summary = {"status": "success", "auto_book": True, "books_used": 2,
               "average_book_profit": 600000, "expected_profit": 1500000,
               "expected_fatigue_used": 20, "remaining_expected_fatigue": 680,
               "remaining_books": None, "full_bargain_count": 1, "full_raise_count": 1}
    workflow = window.workflow_page
    workflow._busy = True
    workflow.apply_progress_event("trade", planning_event(summary))
    QApplication.processEvents()
    assert workflow.runtime_average_book_profit.isVisible()
    assert window.grab().save(str(output / "formal_runtime.png"))
    workflow._busy = False
    window.small_tasks_page.show_trade_preview()
    window._switch_page(window.SMALL_TASKS_PAGE_INDEX)
    preview = window.trade_preview_page
    preview.auto_book.setChecked(True)
    QApplication.processEvents()
    preview.parameter_panel.findChild(QScrollArea).ensureWidgetVisible(preview.book_budget)
    QApplication.processEvents()
    assert window.grab().save(str(output / "preview.png"))
    preview.finish_preview({**summary, "route": planning_event(summary)["payload"]["data"]["route"]})
    QApplication.processEvents()
    assert preview.result_values["average_book_profit"].isVisible()
    assert window.grab().save(str(output / "preview_result.png"))
