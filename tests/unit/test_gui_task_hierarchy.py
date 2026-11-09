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




def test_stop_drops_pending_and_waits_for_worker_acknowledgement(window, monkeypatch):
    page = window.workflow_page
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._begin_workflow([
        {"step": "trade", "dispatch": "trade", "inputs": {}, "label": "货运"},
        {"step": "close", "dispatch": "pc_task", "inputs": {}, "label": "关闭"},
    ], ["trade", "close"], None)
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
