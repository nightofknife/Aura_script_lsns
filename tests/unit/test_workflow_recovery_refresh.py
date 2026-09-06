"""Mandatory resource refresh gates the GUI workflow without running the game."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import PC_GAME_NAME, PC_PLAYER_DATA_REFRESH_TASK_REF
from packages.resonance_gui.main_window import ResonanceMainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    repository = ResonanceConfigRepository(
        QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
    )
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    monkeypatch.setattr(widget.settings_page, "close_on_failure_enabled", lambda: False)
    monkeypatch.setattr(widget.battle_page, "collect_inputs", lambda: {})
    yield widget
    widget._busy = False
    widget._finish_workflow(False, "test finished")
    widget.close()
    app.processEvents()


def configure(window, kinds=("trade",)):
    page = window.workflow_page
    for key, check in page._task_checks.items():
        check.setChecked(key == ("battle" if kinds == ("battle",) else "commerce"))
    for key, check in page._commerce_checks.items():
        check.setChecked(key in kinds)
    page._commerce_order = list(kinds) if len(kinds) == 2 else ["trade", "passenger"]


def payload(remaining=6, count=2):
    return {
        "status": "success", "cid": "refresh-cid",
        "gui_item": {"task_ref": PC_PLAYER_DATA_REFRESH_TASK_REF, "kind": "workflow_task"},
        "user_data": {"player_data": {
            "status": {"fatigue": {"current": 120, "max": 800}},
            "recovery": {
                "sparkling_water": {"remaining_free_uses": remaining, "daily_free_limit": 6},
                "bento": {"available_count": count, "slots": []},
            },
            "metadata": {"persisted": True, "profile_section_updated_at": {
                "fatigue": "2026-09-06T09:59:59Z",
                "sparkling_water": "2026-09-06T10:00:00Z", "bento": "2026-09-06T10:00:01Z",
            }},
        }},
    }


def finish_refresh(window, result):
    window._active_game_name = PC_GAME_NAME
    window._active_kind = "workflow_task"
    window._busy = True
    window._on_task_finished(result)
    window._on_busy_changed(False)


@pytest.mark.parametrize("kinds", [
    ("trade",), ("passenger",), ("trade", "passenger"), ("passenger", "trade"), ("battle",),
])
def test_refresh_is_first_once_and_uses_fixed_selection(window, kinds):
    configure(window, kinds)
    saved = {"stages": ["inventory"], "profile_sections": ["fatigue"], "inventory_categories": ["items"]}
    window._settings.save_player_data_inputs(saved)
    before = window._settings.load_player_data_inputs()
    calls = []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    window._start_workflow()
    assert len(calls) == 1
    assert calls[0][0] == PC_PLAYER_DATA_REFRESH_TASK_REF
    assert calls[0][1] == {"stages": ["profile"], "profile_sections": ["fatigue", "sparkling_water", "bento"]}
    assert window._workflow_current["step"] == "refresh_recovery"
    assert all(row["step"] not in {"refresh_recovery", "startup"} for row in window._workflow_pending)
    assert "refresh_recovery" not in window.workflow_page._task_checks
    assert window.workflow_page.run_tree.topLevelItem(0).text(0) == "1  刷新恢复资源"
    assert window._settings.load_player_data_inputs() == before
    finish_refresh(window, payload())
    assert len(calls) == 1
    assert window._workflow_current["step"] != "refresh_recovery"
    if len(kinds) == 2:
        assert window._workflow_current["inputs"]["order"] == (
            "trade_first" if kinds[0] == "trade" else "passenger_first"
        )


@pytest.mark.parametrize("remaining,count", [(6, 3), (0, 0)])
def test_success_preserves_snapshot_logs_resources_and_dispatches_trade(window, remaining, count):
    configure(window)
    calls = []
    window.requestRunPcTrade.connect(lambda *args: calls.append(args))
    window._start_workflow()
    assert calls == []
    result = payload(remaining, count)
    finish_refresh(window, result)
    assert len(calls) == 1
    snapshot = window._workflow_recovery_snapshot
    assert snapshot["cid"] == "refresh-cid"
    assert snapshot["player_data"] == result["user_data"]["player_data"]
    result["user_data"]["player_data"]["recovery"].clear()
    assert snapshot["player_data"]["recovery"]["bento"]["available_count"] == count
    text = window.workflow_page.log_view.toPlainText()
    assert "疲劳 120/800" in text
    assert f"气泡水 {remaining}/6 次，便当 {count} 份" in text
    assert window.workflow_page.task_progress_bar.value() == 1


@pytest.mark.parametrize("problem", ["task_failed", "missing_water", "missing_bento", "not_persisted", "bad_water", "bad_bento", "bool_count", "missing_fatigue", "invalid_fatigue"])
def test_bad_refresh_never_uses_old_snapshot_or_dispatches_business(window, problem):
    configure(window)
    calls = []
    window.requestRunPcTrade.connect(lambda *args: calls.append(args))
    window._workflow_recovery_snapshot = {"old": True}
    window._start_workflow()
    assert window._workflow_recovery_snapshot == {}
    result = payload()
    player = result["user_data"]["player_data"]
    if problem == "task_failed":
        result["status"] = "failed"
        result["error"] = "template missing"
    elif problem == "missing_water":
        del player["recovery"]["sparkling_water"]
    elif problem == "missing_bento":
        del player["recovery"]["bento"]
    elif problem == "not_persisted":
        player["metadata"]["persisted"] = False
    elif problem == "bad_water":
        player["recovery"]["sparkling_water"]["remaining_free_uses"] = 7
    elif problem == "missing_fatigue":
        del player["status"]["fatigue"]
    elif problem == "invalid_fatigue":
        player["status"]["fatigue"] = {"current": 0, "max": 0}
    else:
        player["recovery"]["bento"]["available_count"] = True if problem == "bool_count" else 4
    finish_refresh(window, result)
    assert calls == []
    assert not window._workflow_active
    assert not window._workflow_pending
    assert window._workflow_recovery_snapshot == {}
    assert window.workflow_page._tree_items["refresh_recovery"].text(1) == "失败"


def test_cancel_during_refresh_blocks_late_success_and_restart_refreshes_again(window):
    configure(window)
    cancels, trades, refreshes = [], [], []
    window.requestCancelCurrent.connect(lambda: cancels.append(True))
    window.requestRunPcTrade.connect(lambda *args: trades.append(args))
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window._start_workflow()
    window._busy = True
    window._stop_workflow()
    assert cancels == [True]
    finish_refresh(window, payload())
    assert not window._workflow_active
    assert trades == []
    assert window._workflow_recovery_snapshot == {}
    assert window.workflow_page._tree_items["refresh_recovery"].text(1) == "已停止"
    window._start_workflow()
    assert len(refreshes) == 2


def test_refresh_exception_stops_workflow(window):
    configure(window)
    window._start_workflow()
    window._busy = True
    window._on_task_failed({"stage": "run_pc_task", "error": "page not found"})
    window._on_busy_changed(False)
    assert not window._workflow_active
    assert not window._workflow_pending
    assert "page not found" in window.workflow_page.log_view.toPlainText()


@pytest.mark.parametrize("current", [0, 900])
def test_zero_and_overcap_fatigue_are_valid(window, current):
    configure(window)
    window._start_workflow()
    result = payload()
    result["user_data"]["player_data"]["status"]["fatigue"]["current"] = current
    finish_refresh(window, result)
    assert window._workflow_current["step"] == "trade"
    assert window._workflow_recovery_snapshot["player_data"]["status"]["fatigue"]["current"] == current


def test_timeout_cancel_cannot_continue_even_with_success_result(window):
    configure(window)
    window._start_workflow()
    result = payload()
    result["gui_timeout_cancelled"] = True
    finish_refresh(window, result)
    assert not window._workflow_active
    assert not window._workflow_pending
    assert window._workflow_recovery_snapshot == {}
    assert "刷新超时" in window.workflow_page.log_view.toPlainText()


def test_failure_keeps_only_existing_configured_close_cleanup(window, monkeypatch):
    configure(window)
    window.workflow_page._task_checks["close"].setChecked(True)
    monkeypatch.setattr(window.settings_page, "close_on_failure_enabled", lambda: True)
    calls = []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    window._start_workflow()
    result = payload()
    result["status"] = "failed"
    finish_refresh(window, result)
    assert window._workflow_current["step"] == "close"
    assert not window._workflow_pending
    assert calls[-1][0] == "tasks:game_startup_pc.yaml:close_game"
