"""Attached passenger UI persists drafts and leaves freight dispatch independent."""
from __future__ import annotations

import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import (
    PC_GAME_NAME, TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA, normalize_trade_task_inputs,
    reduce_trade_progress, reduce_workflow_freight_progress,
    freight_passenger_summary_text, trade_result_summary,
    extract_freight_passenger_cleanup_evidence,
)
from packages.resonance_gui.widgets.trade_page import TradePage
from tests.unit.test_workflow_recovery_refresh import window


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    repository = ResonanceConfigRepository(QSettings(str(tmp_path / "attached.ini"), QSettings.IniFormat))
    widget = TradePage(repository)
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def event(sequence, stage, state="completed", **kwargs):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "attached", "sequence": sequence,
        "stage": stage, "state": state, **kwargs,
    }}


def test_seat_is_required_only_when_enabled_and_draft_survives(page):
    assert not page.auto_passenger.isChecked()
    assert page.passenger_panel.seat_capacity.value() == 0
    assert page.collect_task_inputs()["auto_passenger"] is False
    page.auto_passenger.setChecked(True)
    assert "座位" in page._form_error()
    with pytest.raises(ValueError, match="座位"):
        page.collect_task_inputs()
    page.save_ui_state()
    draft = page._settings.load_trade_inputs()
    assert draft["auto_passenger"] is True
    assert draft["passenger_options"]["seat_capacity"] is None
    page.passenger_panel.seat_capacity.setValue(64)
    page.passenger_panel.flyer_target.setValue(91)
    page.passenger_panel.furniture_bonus_bps.setValue(5.0)
    snapshot = page.collect_task_inputs()
    assert snapshot["passenger_options"]["seat_capacity"] == 64
    assert snapshot["passenger_options"]["flyer_target"] == 91
    assert snapshot["passenger_options"]["furniture_bonus_bps"] == 500
    page.save_ui_state()
    page.set_inputs(page._settings.load_trade_inputs())
    assert page.collect_task_inputs()["passenger_options"] == snapshot["passenger_options"]


def test_fixed_mode_disables_attached_without_erasing_free_draft(page):
    page.passenger_panel.seat_capacity.setValue(128)
    page.auto_passenger.setChecked(True)
    page.mode_buttons["fixed"].setChecked(True)
    page.add_route_city("1")
    page.add_route_city("2")
    assert not page.auto_passenger.isEnabled()
    assert page.collect_task_inputs()["auto_passenger"] is False
    assert page.collect_ui_state()["auto_passenger"] is True
    page.save_ui_state()
    page.set_inputs(page._settings.load_trade_inputs())
    assert page.auto_passenger.isChecked()
    page.mode_buttons["free"].setChecked(True)
    assert page.auto_passenger.isEnabled()
    assert page.collect_task_inputs()["auto_passenger"] is True
    assert page.passenger_panel.seat_capacity.value() == 128
    with pytest.raises(ValueError, match="固定线路"):
        normalize_trade_task_inputs({"trade_mode": "fixed", "auto_passenger": True,
                                    "passenger_options": {"seat_capacity": 64}})


def test_preview_contains_attachment_and_old_settings_default_off(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = ResonanceConfigRepository(QSettings(str(tmp_path / "preview.ini"), QSettings.IniFormat))
    assert settings.load_trade_inputs()["auto_passenger"] is False
    widget = TradePage(settings, preview_mode=True)
    try:
        widget.passenger_panel.seat_capacity.setValue(64)
        widget.auto_passenger.setChecked(True)
        widget.start_city.setCurrentIndex(widget.start_city.findData("1"))
        snapshot = widget.collect_task_inputs(preview=True)
        assert snapshot["auto_passenger"] is True
        assert snapshot["passenger_options"]["seat_capacity"] == 64
        assert "auto_trailer" not in snapshot
        widget.save_ui_state()
        assert settings.load_trade_preview_inputs()["passenger_options"]["seat_capacity"] == 64
    finally:
        widget.close()
        app.processEvents()


def test_workflow_keeps_attached_passenger_on_freight_without_budget_reallocation(window, monkeypatch):
    window.trade_page.passenger_panel.seat_capacity.setValue(64)
    window.trade_page.auto_passenger.setChecked(True)
    window.trade_page.fatigue_budget.setValue(701)
    for key, check in window.workflow_page._task_checks.items():
        check.setChecked(key == "trade")
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._start_workflow()
    assert [row["dispatch"] for row in window._workflow_pending] == ["trade"]
    inputs = window._workflow_pending[0]["inputs"]
    assert inputs["fatigue_budget"] == 701 and inputs["auto_passenger"] is True
    assert inputs["passenger_options"]["seat_capacity"] == 64
    assert "passenger" not in window.workflow_page._task_checks
    window.trade_page.passenger_panel.seat_capacity.setValue(128)
    assert inputs["passenger_options"]["seat_capacity"] == 64


def test_progress_updates_attachment_without_replacing_freight_route_or_revision():
    route = [{"from_city": "A", "to_city": "B"}, {"from_city": "B", "to_city": "C"}]
    visits = [{"city_index": i, "city_count": 3, "city_name": name,
               "phases": [{"key": "travel" if i < 2 else "final_sale", "status": "waiting"}]} for i, name in enumerate("ABC")]
    state = reduce_workflow_freight_progress(None, event(1, "planning", data={
        "route": route, "city_visits": visits, "route_revision": 1, "summary": {"expected_profit": 123}}), expected_cid="attached")
    original = deepcopy(state.route)
    passenger = {"target": 150, "consumed": 70, "remaining_target": 80, "inventory": 200,
                 "loaded_destination": {"city_name": "C"}, "expected_revenue": 777}
    update = event(2, "passenger_recruit", city_index=0, data={"route_revision": 1, "passenger": passenger})
    state = reduce_workflow_freight_progress(state, update, expected_cid="attached")
    assert state.route == original and state.route_revision == 1
    assert not state.route_history
    assert state.summary["expected_profit"] == 123
    assert state.summary["passenger"] == passenger
    assert "C" in state.cities[0].phases[0].detail
    basic = reduce_trade_progress(None, update, expected_cid="attached")
    assert basic.summary["passenger"] == passenger
    state = reduce_workflow_freight_progress(state, event(3, "passenger_planning", data={
        "route_revision": 1, "passenger": {"plan": {"trips": [
            {"from_index": 1, "to_index": 2, "from_city": "B", "to_city": "C"}
        ]}}}), expected_cid="attached")
    assert state.route == original and state.route_revision == 1 and not state.route_history
    assert next(p for p in state.cities[0].phases if p.key == "passenger_recruit").state == "completed"
    assert next(p for p in state.cities[1].phases if p.key == "passenger_recruit").state == "waiting"
    assert next(p for p in state.cities[2].phases if p.key == "passenger_settlement").state == "waiting"
    state = reduce_workflow_freight_progress(state, event(4, "passenger_planning", data={
        "route_revision": 1, "passenger": {"plan": {"trips": []}}}), expected_cid="attached")
    assert next(p for p in state.cities[1].phases if p.key == "passenger_recruit").state == "skipped"


def test_summary_keeps_unknown_revenue_and_freight_profit_separate(page):
    passenger = {"target": 150, "consumed": 100, "remaining_target": 50, "inventory": 0,
                 "expected_revenue": 500, "ticket_revenue": None, "extra_revenue": 200,
                 "total_revenue": None, "stop_reason": "route_exhausted"}
    summary = trade_result_summary({"status": "completed", "expected_profit": 1000, "passenger": passenger})
    assert summary["passenger"] == passenger
    text = freight_passenger_summary_text(passenger)
    assert "未知" in text and "不补跑" in text
    page._render_result(summary)
    assert page.result_values["expected_profit"].text() == "1,000"
    assert "500" in page.passenger_summary_label.text()

    passenger['revenue_reading_enabled'] = False
    text = freight_passenger_summary_text(passenger)
    assert '已结算票款' not in text and '额外收入' not in text
    assert '客运预计收入 500' in text


def pending_passenger():
    return {"enabled": True, "target": 150, "flyers_used": 0,
            "remaining_target": 150, "inventory": 200,
            "loaded_destination": {"city_name": "C", "state": "submitted_unknown"},
            "requires_manual_completion": True, "stop_reason": "cancelled"}


def test_cancel_and_empty_terminal_preserve_pending_passenger_evidence(page):
    page._current_cid = "attached"
    page.apply_progress(event(1, "passenger_recruit", "cancelled", data={"passenger": pending_passenger()}))
    page.cancel_requested({})
    assert "可能已载客" in page.stage_detail.text() and "C" in page.stage_detail.text()
    page.finish_run({"cid": "attached", "status": "cancelled"})
    assert page._current_plan["passenger"]["loaded_destination"]["state"] == "submitted_unknown"
    assert "需要人工" in page.passenger_summary_label.text()
    assert "勿重复揽客" in page.stage_detail.text()
    # An explicit fresh receipt that confirmed settlement replaces old uncertainty.
    settled = {"enabled": True, "loaded_destination": None, "requires_manual_completion": False,
               "flyers_used": 70, "stop_reason": "target_met"}
    page.finish_run({"cid": "attached", "status": "cancelled", "passenger": settled})
    assert page._current_plan["passenger"] == settled
    assert "可能已载客" not in page.passenger_summary_label.text()


def test_terminal_framework_evidence_is_read_only_and_run_bound():
    receipt = pending_passenger()
    payload = {"cid": "attached", "status": "cancelled", "detail": {
        "framework_data": {"freight_passenger": receipt}}}
    copy = deepcopy(payload)
    assert extract_freight_passenger_cleanup_evidence(payload, expected_cid="attached") == receipt
    assert extract_freight_passenger_cleanup_evidence(payload, expected_cid="other") == {}
    assert payload == copy


def test_workflow_accepts_pending_receipt_while_stopping(window):
    workflow = window.workflow_page
    workflow._busy = True
    workflow._freight_progress.cid = "attached"
    workflow.set_stopping()
    workflow.apply_progress_event("trade", event(1, "passenger_recruit", "cancelled", data={
        "passenger": pending_passenger()}))
    assert workflow._freight_progress.summary["passenger"]["requires_manual_completion"] is True
    assert "可能已载客" in workflow.current_operation_label.text()
    workflow.finish_workflow(success=False, message="执行端已停止")
    assert "C" in workflow.current_operation_label.text() and "需要人工" in workflow.current_operation_label.text()


@pytest.mark.parametrize("terminal", ["cancelled", "failed"])
def test_interrupted_passenger_planning_preserves_phases_and_terminal_state(terminal):
    visits = [{"city_index": i, "city_count": 2, "city_name": name,
               "phases": [{"key": "passenger_recruit" if i == 0 else "passenger_settlement",
                           "status": "waiting"}]} for i, name in enumerate("AC")]
    state = reduce_workflow_freight_progress(None, event(1, "planning", data={
        "route": [{"from_city": "A", "to_city": "C"}], "city_visits": visits,
        "route_revision": 1}), expected_cid="attached")
    state = reduce_workflow_freight_progress(state, event(2, "passenger_recruit", "started", city_index=0),
                                            expected_cid="attached")
    snapshot = pending_passenger()
    snapshot["plan"] = {"trips": []}
    state = reduce_workflow_freight_progress(state, event(3, "passenger_planning", terminal,
        data={"passenger": snapshot}), expected_cid="attached")
    assert state.state == terminal
    assert state.cities[0].phases[0].state == terminal
    assert state.cities[1].phases[0].state == "waiting"
    assert state.summary["passenger"] == snapshot
    assert state.route_revision == 1 and not state.route_history


def test_workflow_terminal_framework_receipt_keeps_manual_completion_warning(window):
    window._active_game_name = PC_GAME_NAME
    window._active_kind = "trade_run"
    window._workflow_active = True
    window._workflow_stopping = True
    window._workflow_current = {"step": "trade", "dispatch": "trade", "inputs": {}}
    window.workflow_page.begin_workflow(["trade"], {})
    window.workflow_page.set_stopping()
    window.trade_page._current_cid = "attached"
    window._on_task_finished({"cid": "attached", "status": "cancelled", "detail": {
        "framework_data": {"freight_passenger": pending_passenger()}}})
    assert window.workflow_page._freight_progress.summary["passenger"] == pending_passenger()
    window._on_busy_changed(False)
    text = window.workflow_page.current_operation_label.text()
    assert "可能已载客" in text and "C" in text and "需要人工" in text
