"""Mandatory resource refresh gates the GUI workflow without running the game."""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import PC_GAME_NAME, PC_PLAYER_DATA_REFRESH_TASK_REF
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.bridge import RunnerBridge


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    if "Microsoft YaHei UI" not in QFontDatabase.families():
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
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
    assert calls[0][0]["recovery_snapshot"] == snapshot["player_data"]
    calls[0][0]["recovery_snapshot"]["status"]["fatigue"]["current"] = 999
    assert snapshot["player_data"]["status"]["fatigue"]["current"] == 120
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


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("kinds", [("trade",), ("trade", "passenger"), ("passenger", "trade")])
def test_workflow_snapshot_is_runtime_only_and_new_for_each_run(window, enabled, kinds):
    configure(window, kinds)
    window.workflow_page.trade_sparkling_water.setChecked(enabled)
    calls = []
    signal = window.requestRunPcTrade if len(kinds) == 1 else window.requestRunPcCombinedCommerce
    signal.connect(lambda inputs, _timeout: calls.append(inputs))
    for current in (120, 230):
        window._start_workflow()
        saved = window._settings.load_trade_inputs()
        pending = deepcopy(window._workflow_pending)
        result = payload()
        result["user_data"]["player_data"]["status"]["fatigue"]["current"] = current
        finish_refresh(window, result)
        inputs = calls[-1]
        assert inputs["recovery_snapshot"] == result["user_data"]["player_data"]
        trade = inputs if len(kinds) == 1 else inputs["trade_inputs"]
        assert trade["auto_sparkling_water"] is enabled
        assert trade["use_fatigue_medicine"] is False
        if len(kinds) == 2:
            assert "recovery_snapshot" not in trade
            assert "recovery_snapshot" not in inputs["passenger_inputs"]
        assert window._workflow_current == pending[0]
        assert window._settings.load_trade_inputs() == saved
        assert "recovery_snapshot" not in saved
        inputs["recovery_snapshot"]["metadata"].clear()
        assert window._workflow_recovery_snapshot["player_data"]["metadata"]["persisted"]
        window._finish_workflow(True, "done")
    assert calls[0]["recovery_snapshot"]["status"]["fatigue"]["current"] == 120
    assert calls[1]["recovery_snapshot"]["status"]["fatigue"]["current"] == 230


def test_sparkling_checkbox_syncs_persists_and_never_enables_freight_medicine(window):
    quick = window.workflow_page.trade_sparkling_water
    full = window.trade_page.auto_sparkling_water
    assert quick.text() == full.text() == "自动喝气泡水"
    assert not quick.isChecked() and not full.isChecked()
    assert window._settings.load_trade_inputs()["auto_sparkling_water"] is False
    passenger_before = window.passenger_page.collect_inputs()
    quick.setChecked(True)
    assert full.isChecked()
    assert window._settings.load_trade_inputs()["auto_sparkling_water"] is True
    reloaded = ResonanceConfigRepository(window._settings.settings)
    assert reloaded.load_trade_inputs()["auto_sparkling_water"] is True
    assert window.trade_page.collect_inputs()["auto_sparkling_water"] is True
    merged = window.workflow_page.merge_trade_inputs({"use_fatigue_medicine": True})
    assert merged["auto_sparkling_water"] is True
    assert merged["use_fatigue_medicine"] is False
    full.setChecked(False)
    assert not quick.isChecked()
    assert reloaded.load_trade_inputs()["auto_sparkling_water"] is False
    assert window.passenger_page.collect_inputs() == passenger_before
    assert not any("疲劳药" in check.text() for check in window.trade_page.findChildren(QCheckBox))
    assert not hasattr(window.trade_page, "allowed_medicines")
    assert not hasattr(window.trade_page, "medicine_max_uses")


def test_old_medicine_setting_does_not_opt_in_to_sparkling_water(window):
    window._settings.save_trade_inputs({
        "use_fatigue_medicine": True,
        "allowed_fatigue_medicines": ["old medicine"], "fatigue_medicine_max_uses": 4,
    })
    saved = window._settings.load_trade_inputs()
    assert saved["auto_sparkling_water"] is False
    assert saved["use_fatigue_medicine"] is False
    assert saved["allowed_fatigue_medicines"] == []
    assert saved["fatigue_medicine_max_uses"] == 0


@pytest.mark.parametrize("entry", ["full", "overview_trade", "overview_combined"])
def test_opted_in_standalone_freight_refreshes_before_actual_run(window, entry):
    window.trade_page.auto_sparkling_water.setChecked(True)
    trades, refreshes = [], []
    window.requestRunPcTrade.connect(lambda inputs, _timeout: trades.append(inputs))
    window.requestRunPcCombinedCommerce.connect(lambda inputs, _timeout: trades.append(inputs))
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window._workflow_recovery_snapshot = {"player_data": {"stale": True}}
    if entry == "full":
        window.trade_page._request_start()
    else:
        window._start_commerce_sequence(True, entry == "overview_combined")
    assert trades == []
    assert len(refreshes) == 1
    assert refreshes[0][0] == PC_PLAYER_DATA_REFRESH_TASK_REF
    assert window._workflow_recovery_snapshot == {}
    assert window._workflow_current["step"] == "refresh_recovery"
    saved = window._settings.load_trade_inputs()
    finish_refresh(window, payload())
    assert len(trades) == 1
    assert trades[0]["recovery_snapshot"] == payload()["user_data"]["player_data"]
    assert window._settings.load_trade_inputs() == saved


@pytest.mark.parametrize("problem", ["invalid", "failed", "timeout", "cancelled"])
def test_standalone_freight_preflight_blocks_on_failure(window, problem):
    trades = []
    window.requestRunPcTrade.connect(lambda *args: trades.append(args))
    window._run_pc_trade({"auto_sparkling_water": True}, 0.0)
    result = payload()
    if problem == "invalid":
        result["user_data"]["player_data"]["metadata"]["persisted"] = False
    elif problem == "failed":
        result["status"] = "failed"
    elif problem == "timeout":
        result["gui_timeout_cancelled"] = True
    else:
        window._busy = True
        window._stop_workflow()
    finish_refresh(window, result)
    assert trades == []
    assert not window._workflow_active


def test_unchecked_standalone_trade_and_passenger_never_inject_old_snapshot(window):
    trades, passengers, refreshes = [], [], []
    window._workflow_recovery_snapshot = {"player_data": {"stale": True}}
    window.requestRunPcTrade.connect(lambda inputs, _timeout: trades.append(inputs))
    window.requestRunPcPassenger.connect(lambda inputs, _timeout: passengers.append(inputs))
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window._run_pc_trade(window.trade_page.collect_inputs(), 0.0)
    window._run_pc_passenger(window.passenger_page.collect_inputs(), 0.0)
    assert len(trades) == len(passengers) == 1
    assert "recovery_snapshot" not in trades[0]
    assert "recovery_snapshot" not in passengers[0]
    assert refreshes == []


def test_workflow_passenger_does_not_receive_snapshot(window):
    configure(window, ("passenger",))
    calls = []
    window.requestRunPcPassenger.connect(lambda inputs, _timeout: calls.append(inputs))
    window._start_workflow()
    finish_refresh(window, payload())
    assert len(calls) == 1
    assert "recovery_snapshot" not in calls[0]
    assert "auto_sparkling_water" not in calls[0]


def test_trade_preview_strips_runtime_recovery_fields_without_refresh(window, monkeypatch):
    window.trade_page.auto_sparkling_water.setChecked(True)
    calls, refreshes = [], []
    window.requestPreviewPcTrade.connect(lambda inputs, _timeout: calls.append(inputs))
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    source = {"auto_sparkling_water": True, "recovery_snapshot": payload()["user_data"]["player_data"],
              "start_city_id": "11", "fatigue_budget": 100}
    before = deepcopy(source)
    window._preview_pc_trade(source, 0.0)
    window._preview_workflow_trade()
    assert len(calls) == 2
    assert refreshes == []
    for inputs in calls:
        assert "recovery_snapshot" not in inputs
        assert "auto_sparkling_water" not in inputs
    bridge = RunnerBridge(runner_factory=lambda: pytest.fail("preview must not launch a live runner"))
    monkeypatch.setattr(bridge, "_run_next", lambda: None)
    bridge.preview_pc_trade(source)
    assert bridge._queue[0]["inputs"] == {"start_city_id": "11", "fatigue_budget": 100}
    assert source == before


@pytest.mark.parametrize("combined_run", [False, True])
def test_bridge_preserves_actual_trade_recovery_inputs(window, monkeypatch, combined_run):
    bridge = RunnerBridge(runner_factory=lambda: pytest.fail("must not launch a live runner"))
    monkeypatch.setattr(bridge, "_run_next", lambda: None)
    source = {"recovery_snapshot": payload()["user_data"]["player_data"]}
    if combined_run:
        source["trade_inputs"] = {"auto_sparkling_water": True}
        bridge.run_pc_combined_commerce(source)
    else:
        source["auto_sparkling_water"] = True
        bridge.run_pc_trade(source)
    assert bridge._queue[0]["inputs"] == source


def test_standalone_global_cancel_blocks_late_success(window):
    trades = []
    window.requestRunPcTrade.connect(lambda *args: trades.append(args))
    window._run_pc_trade({"auto_sparkling_water": True}, 0.0)
    window._busy = True
    window._on_commerce_cancel_requested({"cid": "refresh-cid"})
    finish_refresh(window, payload())
    assert trades == []
    assert not window._workflow_active


def test_standalone_refresh_without_result_cannot_dispatch_trade(window):
    trades = []
    window.requestRunPcTrade.connect(lambda *args: trades.append(args))
    window._run_pc_trade({"auto_sparkling_water": True}, 0.0)
    window._on_busy_changed(False)
    assert trades == []
    assert not window._workflow_active
    assert "没有返回可用结果" in window.workflow_page.log_view.toPlainText()


@pytest.mark.parametrize("editor", ["quick", "full"])
def test_sparkling_water_checkbox_render_and_busy_state(window, tmp_path, editor):
    configure(window)
    window.resize(1500, 960)
    window.workflow_page._select_task("commerce")
    if editor == "full":
        window.workflow_page.show_trade_editor()
        check = window.trade_page.auto_sparkling_water
    else:
        check = window.workflow_page.trade_sparkling_water
    window.show()
    QApplication.processEvents()
    assert check.isVisible()
    assert check.width() >= check.sizeHint().width()
    assert not window.grab().isNull()
    assert window.grab().save(str(tmp_path / f"sparkling-{editor}.png"))
    window._start_workflow()
    QApplication.processEvents()
    assert not check.isEnabled()
    window._finish_workflow(True, "done")
    assert check.isEnabled()
