"""Deep-dive task wiring preserves existing entry points and business status."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.bridge import RunnerBridge, DEEP_DIVE_PLANNED_RUN_PROGRESS_EVENT
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import (
    PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF,
    PC_GAME_NAME,
)
from packages.resonance_gui.main_window import ResonanceMainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    settings = ResonanceConfigRepository(QSettings(str(tmp_path / "deep-dive.ini"), QSettings.IniFormat))
    widget = ResonanceMainWindow(settings=settings, initialize_on_startup=False)
    yield widget
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_plan_button_dispatches_selected_layout_and_strategy(window):
    panel = window.small_tasks_page.consciousness_deep_dive_panel
    panel.plan_strategy_combo.setCurrentIndex(panel.plan_strategy_combo.findData("inspiration"))
    panel.plan_turn_budget_spin.setValue(8)
    panel.plan_layout_edit.setText("chosen-layout.json")
    calls = []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    panel.plan_button.click()
    assert len(calls) == 1
    assert calls[0][0] == PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF
    assert calls[0][1] == {"strategy": "inspiration", "turn_budget": 8,
                           "layout_path": "chosen-layout.json", "time_budget_sec": 30}
    assert calls[0][3] == 0.0
    assert panel._task_running and not panel.plan_button.isEnabled()
    # The mocked bridge does not emit the busy transition after dispatch.
    panel.set_runner_busy(True)
    assert panel.cancel_button.isEnabled()


def test_planned_run_uses_its_safety_limit_and_consumes_progress_snapshot(window):
    panel = window.small_tasks_page.consciousness_deep_dive_panel
    panel.planned_run_safety_limit_spin.setValue(19)
    calls = []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    panel.planned_run_button.click()
    assert calls[0][0] == PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF
    assert calls[0][1] == {"strategy": "chase", "safety_round_limit": 19}
    assert calls[0][3] == 0.0
    window._active_game_name = PC_GAME_NAME
    window._on_run_updated({"user_data": {"deep_dive_planned_run": {
        "schema": "resonance_pc.deep_dive_planned_run.v1", "phase": "scan_turn",
        "scan_progress": {"view_index": 2}, "turns_completed": 3}}})
    assert "视角 2/4" in panel.status_label.text()
    assert "已完成回合：3" in panel.summary_label.text()


@pytest.mark.parametrize("entry,task_ref", [
    ("capture_button", PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF),
    ("sensitivity_probe_button", PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF),
])
def test_data_collection_entries_still_dispatch_through_existing_panel(window, entry, task_ref):
    page = window.small_tasks_page
    page.show_task("data_collection")
    assert page.detail_stack.currentWidget() is page.data_collection_panel
    calls = []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    getattr(page.data_collection_panel, entry).click()
    assert len(calls) == 1 and calls[0][0] == task_ref
    assert page.data_collection_panel._task_running


@pytest.mark.parametrize("task_ref,field,result,runner_status,expected", [
    (PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF, "deep_dive_plan",
     {"success": True, "status": "solved"}, "success", "success"),
    (PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF, "deep_dive_plan",
     {"success": False, "status": "search_budget_exhausted"}, "success", "error"),
    (PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF, "deep_dive_planned_run",
     {"success": True, "status": "completed", "terminal_reached": False}, "success", "error"),
    (PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF, "deep_dive_planned_run",
     {"success": True, "status": "completed", "terminal_reached": True}, "success", "success"),
    (PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF, "deep_dive_planned_run",
     {"success": True, "status": "completed", "terminal_reached": True}, "cancelled", "error"),
])
def test_task_completion_renders_business_status_and_preserves_runner_cancellation(
        window, task_ref, field, result, runner_status, expected):
    window._active_game_name = PC_GAME_NAME
    window._small_task_active_ref = task_ref
    if field == "deep_dive_plan":
        window.small_tasks_page.begin_consciousness_deep_dive_plan()
    else:
        window.small_tasks_page.begin_consciousness_deep_dive_planned_run()
    window._on_task_finished({"status": runner_status, "gui_item": {"task_ref": task_ref, "kind": "pc_task"},
                              "user_data": {field: result}})
    panel = window.small_tasks_page.consciousness_deep_dive_panel
    assert panel.status_label.property("resultState") == expected
    assert not panel._task_running
    assert window._small_task_active_ref == ""
    if runner_status == "cancelled":
        assert "取消" in panel.status_label.text()


def test_planned_run_bridge_ignores_stale_foreign_and_duplicate_events(window):
    bridge = RunnerBridge()
    bridge._current_cid = "current"
    bridge._current_item = {"task_ref": PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF}
    events = []
    bridge.deepDivePlannedRunProgress.connect(events.append)

    def event(cid, sequence, schema="resonance_pc.deep_dive_planned_run.v1"):
        return {"name": DEEP_DIVE_PLANNED_RUN_PROGRESS_EVENT,
                "payload": {"cid": cid, "sequence": sequence, "schema": schema, "phase": "scan_turn"}}

    bridge._consume_events([event("foreign", 9), event("current", 1), event("current", 1),
                            event("current", 0), event("current", 2, "wrong"), event("current", 2)])
    assert [row["payload"]["sequence"] for row in events] == [1, 2]
    bridge._current_item = {"task_ref": PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF}
    bridge._consume_events([event("current", 3)])
    assert len(events) == 2
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
