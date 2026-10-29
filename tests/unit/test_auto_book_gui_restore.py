"""Book-state migration and GUI dispatch; recording runners never touch the game."""
from __future__ import annotations

import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository, TRADE_PREVIEW_INPUT_KEYS, TRADE_INPUTS_KEY
from packages.resonance_gui.logic import (
    PC_TRADE_PREVIEW_TASK_REF, PC_TRADE_TASK_REF, average_book_profit_text,
    normalize_trade_task_inputs, trade_result_summary, TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA,
)
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.trade_page import TradePage


@pytest.fixture
def repository(tmp_path):
    return ResonanceConfigRepository(QSettings(str(tmp_path / "books.ini"), QSettings.Format.IniFormat))


@pytest.fixture
def window(repository, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    monkeypatch.setattr(widget.settings_page, "close_on_failure_enabled", lambda: False)
    yield widget
    widget._busy = False
    widget._workflow_active = False
    widget._commerce_active = False
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def run_task(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return {"status": "queued", "cid": "offline-freight"}


def recording_bridge(monkeypatch):
    runner = RecordingRunner()
    bridge = RunnerBridge(lambda: runner)
    monkeypatch.setattr(bridge, "_start_polling", lambda: None)
    return runner, bridge


def test_defaults_seed_and_new_unlimited_preview_state(repository):
    assert repository.load_trade_inputs()["book_budget"] == 0
    assert repository.load_trade_inputs()["book_profit_threshold"] == 500000
    repository.save_trade_inputs({"books_enabled": True, "books_unlimited": True,
                                  "finite_book_budget": 17, "book_budget": None, "book_profit_threshold": 15000})
    preview = repository.load_trade_preview_inputs()
    assert preview["book_budget"] is None
    assert preview["finite_book_budget"] == 17
    assert preview["books_unlimited"] is True
    assert "auto_book" not in preview
    assert preview["book_profit_threshold"] == 15000
    assert set(preview) == set(TRADE_PREVIEW_INPUT_KEYS)


@pytest.mark.parametrize("budget", [0, 17, None])
def test_normalize_preserves_total_book_budget_without_mutating_source(budget):
    inputs = {"trade_mode": "free", "book_budget": budget,
              "book_policy": "fill", "negotiation_policy": "disabled",
              "recovery_snapshot": {"sentinel": [1]}}
    before = deepcopy(inputs)
    output = normalize_trade_task_inputs(inputs)
    assert output["book_budget"] == budget
    if budget == 0:
        assert "book_policy" not in output
    else:
        assert output["book_policy"] == "fill"
    assert output["negotiation_policy"] == "disabled"
    assert inputs == before


@pytest.mark.parametrize("stop", [200, 0])
def test_recovery_threshold_save_reload_and_formal_dispatch(window, monkeypatch, stop):
    page = window.trade_page
    page.sparkling_water_stop_threshold.setValue(stop)
    page.save_ui_state()
    reloaded = TradePage(window._settings)
    assert reloaded.sparkling_water_stop_threshold.value() == stop
    reloaded.close()
    reloaded.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    runner, bridge = recording_bridge(monkeypatch)
    window.requestRunPcTrade.connect(bridge.run_pc_trade)
    page._request_start()
    assert len(runner.calls) == 1
    assert runner.calls[0]["task_ref"] == PC_TRADE_TASK_REF
    assert "sparkling_water_stop_threshold" not in runner.calls[0]["inputs"]


def test_recovery_threshold_rejects_negative_without_overwriting_saved_zero(window):
    page = window.trade_page
    page.sparkling_water_stop_threshold.setValue(0)
    page.save_ui_state()
    saved = window._settings.load_trade_inputs()
    raw = window._settings.settings.value(TRADE_INPUTS_KEY)
    with pytest.raises(ValueError, match="sparkling_water_stop_threshold 必须为非负整数"):
        window._settings.save_trade_inputs({**saved, "sparkling_water_stop_threshold": -1})
    assert window._settings.settings.value(TRADE_INPUTS_KEY) == raw


@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_actual_bridge_dispatch_covers_two_modes_and_book_limits(window, monkeypatch, mode):
    page = window.trade_page
    baseline = page.collect_ui_state()
    for budget in (0, 17, None):
        page.restore_inputs(baseline)
        page.restore_inputs({"trade_mode": mode, "book_budget": budget, "fixed_route_city_ids": ["1", "2", "1"],
                             "target_profit": 1000000, "target_profit_enabled": True, "start_city_id": "11"})
        runner, bridge = recording_bridge(monkeypatch)
        inputs = page.collect_task_inputs()
        bridge.run_pc_trade(inputs)
        assert runner.calls[0]["inputs"] == inputs, (mode, budget)
        assert runner.calls[0]["inputs"]["book_budget"] == budget, (mode, budget)
        assert not {"auto_book", "trade_level", "active_events", "fixed_route_repeat_count", "start_city_id"} & runner.calls[0]["inputs"].keys(), (mode, budget)
        preview = page.collect_task_inputs(preview=True)
        preview_runner, preview_bridge = recording_bridge(monkeypatch)
        preview_bridge.preview_pc_trade(preview)
        assert preview_runner.calls[0]["task_ref"] == PC_TRADE_PREVIEW_TASK_REF, (mode, budget)
        expected = normalize_trade_task_inputs({key: value for key, value in preview.items() if key in TRADE_PREVIEW_INPUT_KEYS})
        assert preview_runner.calls[0]["inputs"] == expected, (mode, budget)
        assert expected["book_budget"] == budget, (mode, budget)


def test_bridge_preview_removes_execution_and_ui_state_only(window, monkeypatch):
    runner, bridge = recording_bridge(monkeypatch)
    window.trade_page.start_city.setCurrentIndex(1)
    inputs = {**window.trade_page.collect_task_inputs(preview=True), "books_unlimited": True,
              "auto_sparkling_water": True, "recovery_snapshot": {"private": 1},
              "base_fatigue_reserve": 0, "auto_pickup": True}
    original = deepcopy(inputs)
    bridge.preview_pc_trade(inputs)
    assert not {"books_unlimited", "auto_sparkling_water", "recovery_snapshot", "base_fatigue_reserve", "auto_pickup"} & runner.calls[0]["inputs"].keys()
    assert inputs == original


def test_unlimited_recovery_dispatch_keeps_validated_projection(window, monkeypatch):
    runner, bridge = recording_bridge(monkeypatch)
    snapshot = {"status": {"fatigue": {"current": 120, "max": 800}, "unrelated": 123},
                "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6}},
                "metadata": {"persisted": True, "unrelated": "private"}}
    source = {"trade_mode": "free", "book_budget": None, "auto_sparkling_water": True,
              "recovery_snapshot": snapshot}
    before = deepcopy(source)
    bridge.run_pc_trade(source)
    actual = runner.calls[0]["inputs"]
    assert actual["book_budget"] is None
    assert actual["recovery_snapshot"] == {
        "status": {"fatigue": {"current": 120, "max": 800}},
        "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6, "requires_refresh": False}},
        "metadata": {"persisted": True}}
    assert source == before


def test_optional_average_history_and_planning(window):
    for books, value, visible in (
        (2, 500001.25, True), (2, 0, True), (0, 500001, False), (2, None, False),
        (2, "bad", False), (2, float("nan"), False), (2, float("inf"), False),
        (None, None, False), (2, True, False),
    ):
        fields = {"trade_mode": "free", "book_budget": None, "remaining_books": None, "books_used": books,
                  "book_incremental_profit": 1000002.5, "book_incremental_profit_exact": "2000005/2",
                  "average_book_profit": value, "average_book_profit_exact": "2000005/4"}
        summary = trade_result_summary(fields)
        for key in fields:
            assert summary[key] == fields[key] or summary[key] is fields[key], (books, value, key)
        for page in (window.trade_page, window.trade_preview_page):
            page.show_history_result({"books_used": 0})
            page.show_history_result(fields)
            assert page.result_values["average_book_profit"].isHidden() is not visible, (books, value)
            page._render_planning_summary(summary)
            assert page.result_captions["average_book_profit"].isHidden() is not visible, (books, value)
            page.show_history_result({"books_used": 0})
            assert page.result_values["average_book_profit"].isHidden(), (books, value)
        assert (average_book_profit_text(summary) is not None) is visible, (books, value)


def test_formal_planning_event_updates_visible_workflow(window):
    page = window.workflow_page
    window.show()
    window._switch_page("workflow")
    page._busy = True
    page.set_active_progress_cid("trade", "offline-plan")
    page.apply_progress_event("trade", {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "offline-plan", "sequence": 1,
        "stage": "planning", "state": "completed", "data": {
            "summary": {"trade_mode": "free", "books_used": 2, "average_book_profit": 600000},
            "route": [{"from_city": "修格里城", "to_city": "铁盟哨站", "books_used": 2,
                       "expected_profit": 1500000, "expected_fatigue_cost": 20}]}}})
    QApplication.processEvents()
    assert page.center_stack.currentWidget() is not page.runtime_trade_plan_page
    page.runtime_plan_button.click()
    assert page.center_stack.currentWidget() is page.runtime_trade_plan_page
    assert page.runtime_average_book_profit.isVisible()
    assert "600,000" in page.runtime_average_book_profit.text()


@pytest.mark.parametrize("budget", [1, None])
def test_fill_waste_limit_reaches_formal_and_preview_runner(window, monkeypatch, budget):
    page = window.trade_page
    page.restore_inputs({"book_budget": budget, "book_policy": "fill",
                         "book_waste_limit": 200, "book_profit_threshold": 9990000,
                         "start_city_id": "1"})
    runner, bridge = recording_bridge(monkeypatch)
    bridge.run_pc_trade(page.collect_task_inputs())
    preview_runner, preview_bridge = recording_bridge(monkeypatch)
    preview_bridge.preview_pc_trade(page.collect_task_inputs(preview=True))
    calls = [*runner.calls, *preview_runner.calls]
    assert [call["task_ref"] for call in calls] == [PC_TRADE_TASK_REF, PC_TRADE_PREVIEW_TASK_REF]
    for call in calls:
        assert call["inputs"]["book_budget"] == budget
        assert call["inputs"]["book_waste_limit"] == 200
        assert "book_profit_threshold" not in call["inputs"]
