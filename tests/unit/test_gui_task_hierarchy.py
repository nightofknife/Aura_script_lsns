"""The simplified GUI uses PC tasks, one workflow and explicit lazy diagnostics."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.logic import (
    PC_GAME_NAME, PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF, PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF,
    extract_deep_dive_scan_result, extract_deep_dive_plan_result,
    extract_deep_dive_planned_run_result,
)

DEVELOPER_ROWS = [{"role": "developer", "task_ref": ref} for ref in (
    PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF, PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF,
)]


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    repo = ResonanceConfigRepository(QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat))
    widget = ResonanceMainWindow(settings=repo, initialize_on_startup=False)
    yield widget
    widget._workflow_active = False
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_only_real_pages_and_visible_history_navigation(window):
    assert set(window._pages) == {"workflow", "tools", "history", "settings"}
    assert window.page_stack.count() == 4
    assert not hasattr(window, "workbench_page")
    assert not hasattr(window, "commerce_page")
    assert not hasattr(window, "WORKBENCH_PAGE_INDEX")
    assert not hasattr(window.workflow_page, "battle_config_page")
    assert window.settings_page.categories.count() == 2
    calls = []
    window.requestRefreshHistory.connect(lambda: calls.append(True))
    window.primary_nav_buttons["history"].click()
    assert calls == [True]
    assert window.page_stack.currentWidget() is window.history_page
    window._history_rows = [{"cid": "past-idle", "status": "success", "task_ref": "tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc"}]
    window._render_history()
    window._open_history_row(0, 0)
    assert window.page_stack.currentWidget() is window.history_page
    assert "past-idle" in window.run_detail.toPlainText()


def test_developer_tools_require_available_catalog_and_build_once(window):
    page = window.small_tasks_page
    user = page.consciousness_deep_dive_panel
    assert page.data_collection_panel is None
    assert page.deep_dive_developer_panel is None
    assert not hasattr(user, "scan_button")
    assert not hasattr(user, "plan_button")
    assert not hasattr(user, "runSingleRunRequested")
    page.show_developer_tools()
    assert page.deep_dive_developer_panel is None
    window._on_tasks_loaded(DEVELOPER_ROWS)
    page.developer_button.click()
    developer = page.deep_dive_developer_panel
    assert developer is not None
    assert not hasattr(developer, "loop_button")
    assert not hasattr(developer, "planned_run_button")
    calls = []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    developer.scan_button.click()
    assert calls[0][0] == PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF
    window._on_busy_changed(True)
    assert developer.cancel_button.isEnabled()
    window._on_tasks_loaded([])
    assert developer._task_running and developer.cancel_button.isEnabled()
    assert window._small_task_active_ref == PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF
    window._on_task_failed({"task_ref": PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF, "error": "device stopped"})
    window._on_busy_changed(False)
    page.show_task("consciousness_deep_dive")
    assert page.detail_stack.currentWidget() is user
    assert user.planned_run_button.isEnabled()
    page.show_developer_tools()
    window._on_tasks_loaded(DEVELOPER_ROWS)
    page.show_developer_tools()
    assert page.deep_dive_developer_panel is developer
    assert developer.scan_button.isEnabled()
    window._on_tasks_loaded([])
    assert page.developer_button.isHidden()


@pytest.mark.parametrize("runner_status,terminal,expected", [
    ("success", True, "success"), ("cancelled", True, "error"),
    ("success", False, "error"),
])
def test_first_planned_run_without_developer_page(window, runner_status, terminal, expected):
    page = window.small_tasks_page
    panel = page.consciousness_deep_dive_panel
    calls, cancels = [], []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    window.requestCancelCurrent.connect(lambda: cancels.append(True))
    panel.planned_run_safety_limit_spin.setValue(17)
    panel.planned_run_button.click()
    assert calls == [(PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF,
                      {"strategy": "chase", "safety_round_limit": 17}, "识海深潜关卡内自动运行", 0.0)]
    assert page.deep_dive_developer_panel is None
    window._active_game_name = PC_GAME_NAME
    window._on_busy_changed(True)
    assert panel.cancel_button.isEnabled()
    if runner_status == "cancelled":
        panel.cancel_button.click()
        assert cancels == [True]
        assert panel._task_running
    window._on_task_finished({"status": runner_status,
        "gui_item": {"task_ref": PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF, "kind": "workflow_task"},
        "user_data": {"deep_dive_planned_run": {"success": True, "status": "completed", "terminal_reached": terminal}}})
    window._on_busy_changed(False)
    assert panel.status_label.property("resultState") == expected
    assert not panel._task_running
    assert panel.planned_run_button.isEnabled()
    assert page.deep_dive_developer_panel is None


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
@pytest.mark.parametrize("recovery", [False, True])
def test_composition_uses_shared_budget_and_snapshot(window, monkeypatch, order, recovery):
    page = window.workflow_page
    for key, check in page._task_checks.items():
        check.setChecked(key in {"trade", "passenger", "close"})
    if order == "passenger_first":
        page._drop_task("passenger", 1)
    window.trade_page.fatigue_budget.setValue(333)
    window.trade_page.auto_sparkling_water.setChecked(recovery)
    window.trade_page.sparkling_water_start_threshold.setValue(600)
    window.trade_page.sparkling_water_stop_threshold.setValue(200)
    window.passenger_page.trip_count.setValue(3)
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._start_workflow()
    pending = window._workflow_pending
    commerce = next(row for row in pending if row["dispatch"] == "combined_commerce")
    assert commerce["inputs"]["order"] == order
    assert commerce["inputs"]["total_fatigue_budget"] == 333
    assert commerce["inputs"]["passenger_inputs"]["trip_count"] == 3
    if recovery:
        assert commerce["inputs"]["trade_inputs"]["sparkling_water_start_threshold"] == 600
        assert commerce["inputs"]["trade_inputs"]["sparkling_water_stop_threshold"] == 200
    assert [row["dispatch"] for row in pending] == (["pc_task"] if recovery else []) + ["combined_commerce", "pc_task"]
    assert pending[-1]["step"] == "close"
    window.trade_page.fatigue_budget.setValue(700)
    assert commerce["inputs"]["total_fatigue_budget"] == 333


def test_stop_drops_pending_and_waits_for_worker_acknowledgement(window, monkeypatch):
    page = window.workflow_page
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._begin_workflow([
        {"step": "trade", "dispatch": "trade", "inputs": {}, "label": "货运"},
        {"step": "close", "dispatch": "pc_task", "inputs": {}, "label": "关闭"},
    ], ["trade", "close"], [], None)
    window._workflow_current = window._workflow_pending.pop(0)
    window._busy = True
    cancellations = []
    window.requestCancelCurrent.connect(lambda: cancellations.append(True))
    window._stop_workflow()
    assert cancellations == [True]
    assert not window._workflow_pending
    assert window._workflow_active and window._workflow_stopping
    assert not page.run_button.isEnabled()
    window._on_busy_changed(False)
    assert not window._workflow_active
    assert not window._workflow_stopping
    assert window._workflow_current is None


def test_bridge_has_no_android_or_queue_and_waits_for_worker_exit(monkeypatch):
    bridge = RunnerBridge()
    assert not hasattr(bridge, "enqueue_task")
    assert not hasattr(bridge, "run_task_now")
    assert not hasattr(bridge, "_queue")
    calls, finished = [], []
    state = {"pending": True}
    runner = SimpleNamespace(
        list_tasks=lambda name: calls.append(name) or [],
        run_task=lambda **kwargs: {"status": "queued", "cid": "run"},
        poll_events=lambda **kwargs: [],
        get_run=lambda cid: {"status": "cancelled", "execution_pending": state["pending"]},
    )
    bridge._runner = runner
    bridge.refresh_tasks()
    assert calls == [PC_GAME_NAME]
    monkeypatch.setattr(bridge, "refresh_history", lambda: None)
    monkeypatch.setattr(bridge, "refresh_target", lambda: None)
    bridge.taskFinished.connect(finished.append)
    bridge.run_pc_task("tasks:example.yaml:example", {}, "example", 0)
    bridge.poll_current()
    assert bridge.busy and not finished
    state["pending"] = False
    bridge.poll_current()
    assert not bridge.busy and len(finished) == 1


def test_nonadjacent_commerce_rejects_without_dispatch(window, monkeypatch):
    page = window.workflow_page
    for key, check in page._task_checks.items():
        check.setChecked(key in {"trade", "battle", "passenger"})
    page._drop_task("battle", 2)
    monkeypatch.setattr(window.battle_page, "collect_inputs", lambda: {"jobs": [{"route_id": "mock"}]})
    warnings, calls = [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    for signal in (window.requestRunPcTrade, window.requestRunPcPassenger, window.requestRunPcCombinedCommerce):
        signal.connect(lambda *args: calls.append(args))
    window._start_workflow()
    assert warnings and "相邻" in warnings[0]
    assert not calls and not window._workflow_active


@pytest.mark.parametrize("order,first,second", [
    ("trade_first", "trade", "passenger"), ("passenger_first", "passenger", "trade"),
])
def test_combined_handoff_failure_preserves_completed_child_and_cancels_pending(
        window, monkeypatch, order, first, second):
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._begin_workflow([
        {"step": first, "dispatch": "combined_commerce", "inputs": {}, "label": "组合"},
        {"step": "close", "dispatch": "pc_task", "inputs": {}, "label": "关闭"},
    ], [first, second, "close"], [first, second], None)
    window._workflow_current = window._workflow_pending.pop(0)
    window._finish_combined_workflow_result(result={
        "order": order, "failure_stage": f"{second}_handoff", "reason": f"{second}_handoff_invalid",
        first: {"success": True, "status": "completed"},
        second: {"success": False, "status": "blocked"},
    }, succeeded=False)
    assert window.workflow_page._tree_items[first].text(1) == "完成"
    assert window.workflow_page._tree_items[second].text(1) == "失败"
    assert not window._workflow_pending and window._workflow_stopping


@pytest.mark.parametrize("first", ["trade", "passenger"])
def test_combined_stop_marks_both_children_and_blocks_dispatch(window, monkeypatch, first):
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._begin_workflow([], ["trade", "passenger"], ["trade", "passenger"], None)
    window._workflow_current = {"step": first, "dispatch": "combined_commerce", "inputs": {}, "label": "组合"}
    window._busy = True
    window._stop_workflow()
    assert all(window.workflow_page._tree_items[k].text(1) == "停止中" for k in ("trade", "passenger"))
    window._on_busy_changed(False)
    assert all(window.workflow_page._tree_items[k].text(1) == "已停止" for k in ("trade", "passenger"))
    assert not window._workflow_active


@pytest.mark.parametrize("extractor,field", [
    (extract_deep_dive_scan_result, "deep_dive_scan"),
    (extract_deep_dive_plan_result, "deep_dive_plan"),
    (extract_deep_dive_planned_run_result, "deep_dive_planned_run"),
])
def test_business_results_require_declared_returns(extractor, field):
    business = {"success": True, "status": "completed", "terminal_reached": True}
    assert extractor({"status": "success", "final_result": {
        "framework_data": {"nodes": {"private_implementation": {"output": business}}},
    }}) == {}
    assert extractor({"final_result": {"user_data": {field: business}}}) == business


def test_pending_tool_dispatch_blocks_new_workflow_and_hotkey_stop_cancels(window):
    requests, workflows, cancellations = [], [], []
    window.requestRunPcTask.connect(lambda *args: requests.append(args))
    window.requestRunPcTrade.connect(lambda *args: workflows.append(args))
    window.requestCancelCurrent.connect(lambda: cancellations.append(True))
    window._run_small_task_team_recommendation()
    assert len(requests) == 1 and not window._busy
    window._run_small_task_player_data({})
    window._start_workflow()
    window._on_global_hotkey("start")
    assert len(requests) == 1 and not workflows and not window._workflow_active
    window._on_global_hotkey("stop")
    assert cancellations == [True]


def test_history_during_run_keeps_live_editors_and_execution_state(window, monkeypatch):
    window._busy = True
    window._history_rows = [{"cid": "past", "status": "success", "task_ref": "tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc"}]
    window._render_history()
    window._switch_page("history")
    monkeypatch.setattr(window.trade_page, "show_history_result", lambda *_: pytest.fail("running editor was replaced"))
    window._open_history_row(0, 0)
    assert window.page_stack.currentWidget() is window.history_page
    assert window._busy
    assert "past" in window.run_detail.toPlainText()


def test_history_refresh_failure_does_not_abort_active_workflow(window):
    window._workflow_active = True
    window._workflow_pending = [{"step": "battle", "dispatch": "battle", "inputs": {}}]
    window._on_task_failed({"stage": "list_runs", "error": "history unavailable"})
    assert window._workflow_active and not window._workflow_stopping
    assert window._workflow_pending[0]["step"] == "battle"


def test_combined_framework_success_requires_both_business_receipts(window, monkeypatch):
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._begin_workflow([], ["trade", "passenger"], ["trade", "passenger"], None)
    window._workflow_current = {"step": "trade", "dispatch": "combined_commerce", "inputs": {}, "label": "组合"}
    window._finish_combined_workflow_result(result={
        "success": True, "trade": {"success": True, "status": "completed"},
    }, succeeded=True)
    assert window.workflow_page._tree_items["trade"].text(1) == "完成"
    assert window.workflow_page._tree_items["passenger"].text(1) == "失败"
    assert window._workflow_stopping
