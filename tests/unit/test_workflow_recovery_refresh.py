"""Opted-in recovery gates the GUI workflow without running the game."""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QPushButton, QScrollArea
from packages.resonance_gui.widgets.toggle_button import ToggleButton as QCheckBox

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import PC_GAME_NAME, PC_PLAYER_DATA_REFRESH_TASK_REF
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.bridge import RunnerBridge


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    families = []
    for name in ("msyh.ttc", "msyhbd.ttc"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        font_id = QFontDatabase.addApplicationFont(str(font))
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    if families:
        app.setFont(QFont(families[0], 10))
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    repository = ResonanceConfigRepository(
        QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
    )
    executable = tmp_path / "雷索纳斯.exe"
    executable.write_bytes(b"offline fixture; never executed")
    repository.set_value("game/executable_path", str(executable))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    monkeypatch.setattr(widget.settings_page, "close_on_failure_enabled", lambda: False)
    # Explicit user configuration is required before either recovery can run.
    widget.trade_page.sparkling_water_start_threshold.setValue(600)
    widget.trade_page.sparkling_water_stop_threshold.setValue(300)
    widget.trade_page.bento_start_threshold.setValue(700)
    widget.trade_page.bento_stop_threshold.setValue(200)
    widget.trade_page.save_ui_state()
    monkeypatch.setattr(widget.battle_page, "collect_inputs", lambda: {
        "jobs": [{"route_id": "ct.tie_an.shoggolith_city.bounty"}],
    })
    yield widget
    widget._busy = False
    widget._finish_workflow(False, "test finished")
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_run_button_waits_for_cancelled_worker_exit(window, monkeypatch):
    from types import SimpleNamespace

    pending = [True]
    bridge = RunnerBridge()
    bridge._runner = SimpleNamespace(
        poll_events=lambda **kwargs: [],
        get_run=lambda cid: {"cid": cid, "status": "cancelled", "execution_pending": pending[0]},
    )
    bridge._current_cid = "test-button-drain"
    bridge._current_item = {"label": "test", "timeout_sec": 0}
    bridge._cancel_sent = True
    bridge.busyChanged.connect(window._on_busy_changed)
    monkeypatch.setattr(bridge, "refresh_history", lambda: None)
    monkeypatch.setattr(bridge, "refresh_target", lambda: None)
    bridge._set_busy(True)
    bridge.poll_current()
    assert not window.workflow_page.run_button.isEnabled()
    assert window._busy
    pending[0] = False
    bridge.poll_current()
    assert window.workflow_page.run_button.isEnabled()
    assert not window._busy


def configure(window, kinds=("trade",)):
    # Recovery is a preparation child of the independent freight task.
    window.trade_page.auto_sparkling_water.setChecked(True)
    page = window.workflow_page
    for key, check in page._task_checks.items():
        check.setChecked(key in kinds)
    page._task_order = ["startup", *kinds, *[key for key in ("trade", "passenger", "battle") if key not in kinds], "close"]
    page._rebuild_task_rows()


def finish_preceding_tasks(window):
    while window._workflow_current and window._workflow_current["step"] == "startup":
        step = window._workflow_current["step"]
        window._active_game_name = PC_GAME_NAME
        window._active_kind = "passenger_run" if step == "passenger" else "workflow_task"
        window._busy = True
        window._on_task_finished({"status": "success", "gui_item": {"kind": window._active_kind}})
        window._on_busy_changed(False)


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("kinds", [("trade",), ("trade", "passenger"), ("passenger", "trade"), ("passenger",), ("battle",)])
def test_disabled_recovery_still_refreshes_prison_only_for_freight(window, monkeypatch, startup, kinds):
    configure(window, kinds)
    window.trade_page.auto_sparkling_water.setChecked(False)
    window.trade_page.auto_bento.setChecked(False)
    window.workflow_page._task_checks["startup"].setChecked(startup)
    window._workflow_recovery_snapshot = {"player_data": {"stale": True}}
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._start_workflow()
    queue = window._workflow_pending
    assert queue
    preparations = [row for row in queue if row["step"] == "refresh_recovery"]
    assert len(preparations) == (1 if "trade" in kinds else 0)
    if preparations:
        assert preparations[0]["inputs"]["stages"] == ["prison_products"]
    assert ("refresh_recovery" in window.workflow_page._tree_items) is ("trade" in kinds)
    assert window._workflow_recovery_snapshot == {}
    assert (queue[0]["step"] == "startup") is startup


def payload(remaining=6, count=2):
    return {
        "status": "success", "cid": "refresh-cid",
        "gui_item": {"task_ref": PC_PLAYER_DATA_REFRESH_TASK_REF, "kind": "workflow_task"},
        "user_data": {"player_data": {
            "prison_products": {
                "success": True, "scope": "visible", "visible_complete": True,
                "returned_to_profile": True, "inventory_complete": False,
                "goods": [{"goods_id": "1", "goods": "涤纶", "quantity": 20}],
                "unresolved": [], "requested_goods": None, "refreshed_at": "2026-10-24T10:00:00Z",
            },
            "status": {"fatigue": {"current": 120, "max": 800}},
            "recovery": {
                "sparkling_water": {"remaining_free_uses": remaining, "daily_free_limit": 6},
                "bento_count": {"count": count},
            },
            "metadata": {"persisted": True, "executed_stages": ["profile", "prison_products"], "executed_profile_sections": ["fatigue", "sparkling_water", "bento_count"], "profile_section_updated_at": {
                "fatigue": "2026-09-06T09:59:59Z",
                "sparkling_water": "2026-09-06T10:00:00Z",
                "bento_count": "2026-09-06T10:00:01Z",
            }},
        }},
    }


@pytest.mark.parametrize(("water", "bento", "priority", "expected"), [
    (True, False, ["work_meals", "love_bentos"], ["fatigue", "sparkling_water"]),
    (False, True, ["work_meals", "love_bentos"], ["fatigue", "bento_count"]),
    (False, True, ["work_meals"], ["fatigue", "bento_count"]),
    (False, True, ["love_bentos"], ["fatigue", "bento_count"]),
    (True, True, ["love_bentos", "work_meals"], ["fatigue", "sparkling_water", "bento_count"]),
])
def test_recovery_refresh_reads_only_enabled_resources(window, water, bento, priority, expected):
    trade = window._settings.load_trade_inputs()
    trade.update(auto_sparkling_water=water, auto_bento=bento, bento_priority=priority)
    step = window._recovery_refresh_step(trade)
    assert step["inputs"] == {"stages": ["profile", "prison_products"], "profile_sections": expected, "prison_product_goods": []}


def finish_refresh(window, result):
    window._active_game_name = PC_GAME_NAME
    window._active_kind = "workflow_task"
    window._busy = True
    window._on_task_finished(result)
    window._on_busy_changed(False)


@pytest.mark.parametrize("kinds", [
    ("trade",), ("trade", "passenger"), ("passenger", "trade"),
])
def test_refresh_is_first_once_and_uses_fixed_selection(window, kinds):
    configure(window, kinds)
    saved = {"stages": ["inventory"], "profile_sections": ["fatigue"], "inventory_categories": ["items"]}
    window._settings.save_player_data_inputs(saved)
    before = window._settings.load_player_data_inputs()
    calls, passengers = [], []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    window.requestRunPcPassenger.connect(lambda *args: passengers.append(args))
    window._start_workflow()
    assert passengers == []
    finish_preceding_tasks(window)
    assert len(calls) == 1
    assert calls[0][0] == PC_PLAYER_DATA_REFRESH_TASK_REF
    assert calls[0][1] == {"stages": ["profile", "prison_products"], "profile_sections": ["fatigue", "sparkling_water"], "prison_product_goods": []}
    assert window._workflow_current["step"] == "refresh_recovery"
    assert window._workflow_current["parent"] == "trade"
    assert all(row["step"] not in {"refresh_recovery", "startup"} for row in window._workflow_pending)
    assert "refresh_recovery" not in window.workflow_page._task_checks
    prepare = window.workflow_page._tree_items["refresh_recovery"]
    assert prepare.parent() is window.workflow_page._tree_items["trade"]
    assert window.workflow_page.run_tree.topLevelItemCount() == len(kinds)
    assert window.workflow_page.task_progress_bar.maximum() == len(kinds)
    assert window._settings.load_player_data_inputs() == before
    finish_refresh(window, payload())
    assert len(calls) == 1
    assert window._workflow_current["step"] == kinds[0]
    assert window.workflow_page.task_progress_bar.value() == 0


@pytest.mark.parametrize("remaining,count", [(6, 3), (0, 0), (1, 12)])
def test_success_preserves_snapshot_logs_resources_and_dispatches_trade(window, remaining, count):
    configure(window)
    window.trade_page.auto_bento.setChecked(True)
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
    assert snapshot["player_data"]["recovery"]["bento_count"]["count"] == count
    text = window.workflow_page.log_view.toPlainText()
    assert "疲劳 120/800" in text
    assert f"气泡水 {remaining}/6 次，便当 {count} 份" in text
    assert window.workflow_page.task_progress_bar.value() == 0
    assert window.workflow_page.task_progress_bar.maximum() == 1


def test_startup_precedes_combined_preparation_for_passenger_first(window):
    configure(window, ("passenger", "trade"))
    window.workflow_page._task_checks["startup"].setChecked(True)
    events = []
    window.requestRunPcTask.connect(
        lambda task_ref, *_args: events.append("prepare" if task_ref == PC_PLAYER_DATA_REFRESH_TASK_REF else "startup")
    )
    window.requestRunPcPassenger.connect(lambda *_args: events.append("passenger"))
    window._start_workflow()
    assert events == ["startup"]
    assert window._workflow_current["step"] == "startup"
    finish_preceding_tasks(window)
    assert events == ["startup", "prepare"]
    page = window.workflow_page
    assert page.task_progress_bar.maximum() == 3
    assert page.task_progress_bar.value() == 1
    assert page._tree_items["refresh_recovery"].parent() is page._tree_items["trade"]
    finish_refresh(window, payload())
    assert window._workflow_current["step"] == "passenger"
    assert window._workflow_current["dispatch"] == "combined_commerce"
    assert page.task_progress_bar.value() == 1


def test_bento_only_refresh_does_not_require_water(window):
    configure(window)
    window.trade_page.auto_sparkling_water.setChecked(False)
    window.trade_page.auto_bento.setChecked(True)
    window._start_workflow()
    result = payload(count=9)
    player = result["user_data"]["player_data"]
    del player["recovery"]["sparkling_water"]
    player["metadata"]["executed_profile_sections"] = ["fatigue", "bento_count"]
    finish_refresh(window, result)
    assert window._workflow_recovery_snapshot["player_data"]["recovery"] == {"bento_count": {"count": 9}}
    assert "便当 9 份" in window.workflow_page.log_view.toPlainText()


@pytest.mark.parametrize("problem", [
    "task_failed", "missing_water", "missing_bento_count", "missing_execution",
    "not_persisted", "bad_water", "bool_count", "negative_count", "over_limit_count",
    "missing_fatigue", "invalid_fatigue",
])
def test_bad_refresh_never_uses_old_snapshot_or_dispatches_business(window, problem):
    configure(window)
    window.trade_page.auto_bento.setChecked(True)
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
    elif problem == "missing_bento_count":
        del player["recovery"]["bento_count"]
    elif problem == "missing_execution":
        player["metadata"]["executed_profile_sections"] = ["fatigue", "sparkling_water"]
    elif problem == "not_persisted":
        player["metadata"]["persisted"] = False
    elif problem == "bad_water":
        player["recovery"]["sparkling_water"]["remaining_free_uses"] = 7
    elif problem == "missing_fatigue":
        del player["status"]["fatigue"]
    elif problem == "invalid_fatigue":
        player["status"]["fatigue"] = {"current": 0, "max": 0}
    elif problem == "negative_count":
        player["recovery"]["bento_count"]["count"] = -1
    elif problem == "over_limit_count":
        player["recovery"]["bento_count"]["count"] = 13
    else:
        player["recovery"]["bento_count"]["count"] = True
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
@pytest.mark.parametrize("bento_enabled", [False, True])
@pytest.mark.parametrize("kinds", [("trade",), ("trade", "passenger"), ("passenger", "trade")])
def test_workflow_snapshot_is_runtime_only_and_new_for_each_run(window, enabled, bento_enabled, kinds):
    configure(window, kinds)
    window.trade_page.auto_sparkling_water.setChecked(enabled)
    # Edit the draft while its controls are enabled, then retain it when disabled.
    window.trade_page.auto_bento.setChecked(True)
    window.trade_page.bento_move_buttons["love_bentos", -1].click()
    window.trade_page.auto_bento.setChecked(bento_enabled)
    refreshes, calls, passengers = [], [], []
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window.requestRunPcTrade.connect(lambda inputs, _timeout: calls.append(inputs))
    window.requestRunPcCombinedCommerce.connect(lambda inputs, _timeout: calls.append(inputs))
    window.requestRunPcPassenger.connect(lambda inputs, _timeout: passengers.append(inputs))
    for current in (120, 230):
        window._start_workflow()
        saved = window._settings.load_trade_inputs()
        finish_preceding_tasks(window)
        result = payload()
        result["user_data"]["player_data"]["status"]["fatigue"]["current"] = current
        if not enabled and not bento_enabled:
            assert len(refreshes) == len(calls) + 1
            finish_refresh(window, result)
            assert "recovery_snapshot" not in calls[-1]
            assert window._workflow_recovery_snapshot["recovery_requested"] is False
        else:
            pending = deepcopy(window._workflow_pending)
            finish_refresh(window, result)
            inputs = calls[-1]
            assert inputs["recovery_snapshot"] == result["user_data"]["player_data"]
            trade_inputs = inputs.get("trade_inputs", inputs)
            assert trade_inputs["auto_sparkling_water"] is enabled
            assert trade_inputs["auto_bento"] is bento_enabled
            if bento_enabled:
                assert trade_inputs["bento_priority"] == ["love_bentos", "work_meals"]
            else:
                assert "bento_priority" not in trade_inputs
            assert saved["bento_priority"] == ["love_bentos", "work_meals"]
            assert trade_inputs["use_fatigue_medicine"] is False
            assert window._workflow_current == pending[0]
            inputs["recovery_snapshot"]["metadata"].clear()
            assert window._workflow_recovery_snapshot["player_data"]["metadata"]["persisted"]
        assert window._settings.load_trade_inputs() == saved
        assert "recovery_snapshot" not in saved
        assert all("recovery_snapshot" not in inputs and "auto_bento" not in inputs and "bento_priority" not in inputs for inputs in passengers)
        window._finish_workflow(True, "done")
    assert len(calls) == 2
    if enabled or bento_enabled:
        assert calls[0]["recovery_snapshot"]["status"]["fatigue"]["current"] == 120
        assert calls[1]["recovery_snapshot"]["status"]["fatigue"]["current"] == 230
    assert len(refreshes) == 2


def test_sparkling_checkbox_syncs_persists_and_never_enables_freight_medicine(window):
    full = window.trade_page.auto_sparkling_water
    assert full.text() == "自动喝气泡水"
    assert not full.isChecked()
    assert window._settings.load_trade_inputs()["auto_sparkling_water"] is False
    passenger_before = window.passenger_page.collect_inputs()
    full.setChecked(True)
    reloaded = ResonanceConfigRepository(window._settings.settings)
    assert reloaded.load_trade_inputs()["auto_sparkling_water"] is True
    assert window.trade_page.collect_inputs()["auto_sparkling_water"] is True
    assert window.trade_page.collect_inputs()["use_fatigue_medicine"] is False
    full.setChecked(False)
    assert reloaded.load_trade_inputs()["auto_sparkling_water"] is False
    assert window.passenger_page.collect_inputs() == passenger_before


def test_old_medicine_setting_does_not_opt_in_to_sparkling_water(window):
    window._settings.save_trade_inputs({
        "use_fatigue_medicine": True,
        "allowed_fatigue_medicines": ["old medicine"], "fatigue_medicine_max_uses": 4,
    })
    saved = window._settings.load_trade_inputs()
    assert saved["auto_sparkling_water"] is False
    assert saved["use_fatigue_medicine"] is True
    assert saved["allowed_fatigue_medicines"] == ["old medicine"]
    assert saved["fatigue_medicine_max_uses"] == 4


@pytest.mark.parametrize("entry", ["full"])
@pytest.mark.parametrize("water,bento", [(True, False), (False, True), (True, True), (False, False)])
def test_opted_in_standalone_freight_refreshes_before_actual_run(window, entry, water, bento):
    window.trade_page.auto_sparkling_water.setChecked(water)
    window.trade_page.auto_bento.setChecked(bento)
    trades, refreshes = [], []
    window.requestRunPcTrade.connect(lambda inputs, _timeout: trades.append(inputs))
    window.requestRunPcCombinedCommerce.connect(lambda inputs, _timeout: trades.append(inputs))
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window._workflow_recovery_snapshot = {"player_data": {"stale": True}}
    window.trade_page._request_start()
    assert trades == []
    assert len(refreshes) == 1
    assert refreshes[0][0] == PC_PLAYER_DATA_REFRESH_TASK_REF
    assert window._workflow_recovery_snapshot == {}
    assert window._workflow_current["step"] == "refresh_recovery"
    saved = window._settings.load_trade_inputs()
    result = payload()
    if not (water or bento):
        player = result["user_data"]["player_data"]
        player.pop("status")
        player.pop("recovery")
        player["metadata"]["executed_stages"] = ["prison_products"]
    finish_refresh(window, result)
    assert len(trades) == 1
    assert len(refreshes) == 1
    trade = trades[0]
    assert trade["auto_sparkling_water"] is water
    assert trade["auto_bento"] is bento
    if bento:
        assert trade["bento_priority"] == ["work_meals", "love_bentos"]
    else:
        assert "bento_priority" not in trade
    assert saved["bento_priority"] == ["work_meals", "love_bentos"]
    if water or bento:
        assert trades[0]["recovery_snapshot"] == result["user_data"]["player_data"]
    else:
        assert "recovery_snapshot" not in trades[0]
    assert window._settings.load_trade_inputs() == saved


@pytest.mark.parametrize("problem", ["invalid", "failed", "timeout", "cancelled"])
@pytest.mark.parametrize("option", ["auto_sparkling_water", "auto_bento"])
def test_standalone_freight_preflight_blocks_on_failure(window, problem, option):
    trades = []
    window.requestRunPcTrade.connect(lambda *args: trades.append(args))
    window._run_pc_trade({option: True}, 0.0)
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
    assert trades == []
    assert len(refreshes) == len(passengers) == 1
    result = payload()
    result["user_data"]["player_data"]["metadata"]["executed_stages"] = ["prison_products"]
    finish_refresh(window, result)
    assert len(trades) == 1
    assert "recovery_snapshot" not in trades[0]
    assert "recovery_snapshot" not in passengers[0]
    assert len(refreshes) == 1


def test_workflow_passenger_does_not_receive_snapshot(window):
    configure(window, ("passenger",))
    window.trade_page.auto_bento.setChecked(True)
    calls = []
    window.requestRunPcPassenger.connect(lambda inputs, _timeout: calls.append(inputs))
    window._start_workflow()
    assert len(calls) == 1
    assert "recovery_snapshot" not in calls[0]
    assert "auto_sparkling_water" not in calls[0]
    assert "auto_bento" not in calls[0]
    assert "bento_priority" not in calls[0]


def test_trade_preview_strips_runtime_recovery_fields_without_refresh(window, monkeypatch):
    window.trade_page.auto_sparkling_water.setChecked(True)
    calls, refreshes = [], []
    window.requestPreviewPcTrade.connect(lambda inputs, _timeout: calls.append(inputs))
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    source = {"auto_sparkling_water": True, "recovery_snapshot": payload()["user_data"]["player_data"],
              "auto_bento": True, "bento_priority": ["love_bentos"],
              "sparkling_water_start_threshold": 600, "sparkling_water_stop_threshold": 300,
              "bento_start_threshold": 700, "bento_stop_threshold": 200,
              "arrival_timeout_seconds": 3600, "use_fatigue_medicine": True,
              "start_city_id": "11", "fatigue_budget": 100}
    before = deepcopy(source)
    window._preview_pc_trade(source, 0.0)
    window.small_tasks_page.show_trade_preview()
    preview = window.small_tasks_page.trade_preview_panel
    preview.start_city.setCurrentIndex(preview.start_city.findData("11"))
    preview.preview_button.click()
    assert len(calls) == 2
    assert refreshes == []
    for inputs in calls:
        assert "recovery_snapshot" not in inputs
        assert "auto_sparkling_water" not in inputs
        assert "auto_bento" not in inputs
        assert "bento_priority" not in inputs
        assert not {"sparkling_water_start_threshold", "sparkling_water_stop_threshold",
                    "bento_start_threshold", "bento_stop_threshold"} & inputs.keys()
        assert "arrival_timeout_seconds" not in inputs
        assert "use_fatigue_medicine" not in inputs
    assert preview.auto_bento.isHidden()
    assert preview.bento_priority_panel.isHidden()
    assert "auto_bento" not in window._settings.load_trade_preview_inputs()
    assert "bento_priority" not in window._settings.load_trade_preview_inputs()
    bridge = RunnerBridge(runner_factory=lambda: pytest.fail("preview must not launch a live runner"))
    dispatched = []
    monkeypatch.setattr(bridge, "_run_item", dispatched.append)
    bridge.preview_pc_trade(source)
    assert dispatched[0]["inputs"] == {"start_city_id": "11", "fatigue_budget": 100}
    assert source == before


@pytest.mark.parametrize("combined_run", [False, True])
def test_bridge_preserves_actual_trade_recovery_inputs(window, monkeypatch, combined_run):
    bridge = RunnerBridge(runner_factory=lambda: pytest.fail("must not launch a live runner"))
    dispatched = []
    monkeypatch.setattr(bridge, "_run_item", dispatched.append)
    source = {"recovery_snapshot": payload()["user_data"]["player_data"]}
    trade = {"auto_sparkling_water": True, "auto_bento": True,
             "bento_priority": ["love_bentos", "work_meals"]}
    if combined_run:
        source["trade_inputs"] = trade
        bridge.run_pc_combined_commerce(source)
    else:
        source.update(trade)
        bridge.run_pc_trade(source)
    assert dispatched[0]["inputs"] == source


def test_standalone_global_cancel_blocks_late_success(window):
    trades = []
    window.requestRunPcTrade.connect(lambda *args: trades.append(args))
    window._run_pc_trade({"auto_sparkling_water": True}, 0.0)
    window._busy = True
    window._on_workflow_cancel_requested({"cid": "refresh-cid"})
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


@pytest.mark.parametrize("problem", ["missing", "partial", "wrong_scope", "no_return", "unresolved", "bad_quantity", "no_stage", "full_inventory"])
def test_prison_refresh_failure_blocks_freight_without_recovery(window, problem):
    trades = []
    window.requestRunPcTrade.connect(lambda *args: trades.append(args))
    window._run_pc_trade({}, 0.0)
    result = payload()
    player = result["user_data"]["player_data"]
    stock = player["prison_products"]
    if problem == "missing":
        del player["prison_products"]
    elif problem == "partial":
        stock["visible_complete"] = False
    elif problem == "wrong_scope":
        stock["scope"] = "all"
    elif problem == "no_return":
        stock["returned_to_profile"] = False
    elif problem == "unresolved":
        stock["unresolved"] = [{"reason": "unknown"}]
    elif problem == "bad_quantity":
        stock["goods"][0]["quantity"] = True
    elif problem == "no_stage":
        player["metadata"]["executed_stages"] = ["profile"]
    else:
        stock["inventory_complete"] = True
    finish_refresh(window, result)
    assert trades == []
    assert not window._workflow_active


@pytest.mark.parametrize("kinds", [("trade",), ("trade", "passenger"), ("passenger", "trade")])
def test_selected_prison_goods_forwarded_to_preparation_only(window, kinds):
    configure(window, kinds)
    values = window.trade_page.collect_ui_state()
    values["prison_product_goods"] = ["1", "2"]
    window.trade_page.set_inputs(values)
    window.trade_page.save_ui_state()
    refreshes, business = [], []
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window.requestRunPcTrade.connect(lambda inputs, timeout: business.append(inputs))
    window.requestRunPcCombinedCommerce.connect(lambda inputs, timeout: business.append(inputs))
    window._start_workflow()
    assert refreshes[0][1]["prison_product_goods"] == ["1", "2"]
    assert window._settings.load_trade_inputs()["prison_product_goods"] == ["1", "2"]
    finish_refresh(window, payload())
    assert len(business) == 1
    assert "prison_product_goods" not in business[0]
    assert "prison_product_goods" not in business[0].get("trade_inputs", {})


def test_prison_product_selector_cancel_clear_and_busy(window):
    from packages.resonance_gui.widgets.prison_product_selector import PrisonProductDialog
    page = window.trade_page
    dialog = PrisonProductDialog([], page)
    try:
        assert dialog.selected_goods() == []
        first = dialog.list.item(0)
        first.setCheckState(Qt.CheckState.Checked)
        assert dialog.selected_goods() == [first.data(Qt.ItemDataRole.UserRole)]
        dialog.search.setText(first.text())
        assert not first.isHidden()
        dialog.clear_selection()
        assert dialog.selected_goods() == []
        dialog.reject()
        assert page.collect_ui_state()["prison_product_goods"] == []
        page.set_busy(True)
        assert not page.prison_products_button.isEnabled()
        page.set_busy(False)
        assert page.prison_products_button.isEnabled()
    finally:
        dialog.deleteLater()


def test_standalone_prison_filter_uses_saved_ui_without_leaking_into_trade(window):
    page = window.trade_page
    values = page.collect_ui_state()
    values["prison_product_goods"] = ["1"]
    page.set_inputs(values)
    refreshes, trades = [], []
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window.requestRunPcTrade.connect(lambda inputs, timeout: trades.append(inputs))
    page._request_start()
    assert refreshes[0][1]["prison_product_goods"] == ["1"]
    result = payload()
    stock = result["user_data"]["player_data"]["prison_products"]
    stock.update(goods=[], requested_goods=["1"], not_observed=["1"])
    finish_refresh(window, result)
    assert len(trades) == 1
    assert "prison_product_goods" not in trades[0]
    assert "recovery_snapshot" not in trades[0]


def test_player_data_tool_retains_selected_prison_goods(window):
    panel = window.small_tasks_page.player_data_panel
    panel._select_stages(("prison_products",))
    panel._prison_product_goods = ["1", "2"]
    inputs = panel.collect_inputs()
    assert inputs["stages"] == ["prison_products"]
    assert inputs["prison_product_goods"] == ["1", "2"]
    panel._prison_product_goods = []
    panel._load_inputs()
    assert panel._prison_product_goods == ["1", "2"]
    panel.apply_refresh_result(payload()["user_data"]["player_data"])
    assert "涤纶 × 20" in panel.prison_stock_label.text()
    assert "未显示商品库存未知" in panel.prison_stock_label.text()


@pytest.mark.parametrize("size_name,size", [("normal", (1440, 860)), ("minimum", (1180, 720))])
def test_sparkling_water_checkbox_render_and_busy_state(window, tmp_path, size_name, size):
    configure(window)
    window.resize(*size)
    window.workflow_page.show_trade_editor()
    window.show()
    QApplication.processEvents()
    assert (window.width(), window.height()) == size
    page = window.trade_page
    page.recovery_section.toggle.setChecked(True)
    check, bento = page.auto_sparkling_water, page.auto_bento
    scroll = page.parameter_panel.findChild(QScrollArea)
    scroll.ensureWidgetVisible(page.bento_priority_panel)
    QApplication.processEvents()
    assert scroll.horizontalScrollBar().maximum() == 0
    assert check.isVisible() and check.width() >= check.sizeHint().width()
    assert bento.isVisible() and bento.width() >= bento.sizeHint().width()
    assert check.geometry().right() < bento.geometry().left()
    assert check.geometry().center().y() == bento.geometry().center().y()
    for key, check_type in page.bento_type_checks.items():
        row = page._bento_rows[key]
        assert check_type.width() >= check_type.sizeHint().width()
        for delta in (-1, 1):
            button = page.bento_move_buttons[key, delta]
            assert button.toolTip()
            assert button.width() == button.height() == 28
            assert row.rect().contains(button.geometry())
    assert window.grab().save(str(tmp_path / f"recovery-{size_name}.png"))
    window._start_workflow()
    QApplication.processEvents()
    assert not check.isEnabled() and not bento.isEnabled()
    assert all(not button.isEnabled() for button in page.bento_type_checks.values())
    assert all(not button.isEnabled() for button in page.bento_move_buttons.values())
    assert window.workflow_page.task_progress_bar.maximum() == 1
    window._finish_workflow(True, "done")
    assert check.isEnabled() and bento.isEnabled()
    assert page.bento_priority_panel.isEnabled() is bento.isChecked()
    bento.setChecked(True)
    assert all(button.isEnabled() for button in page.bento_type_checks.values())


def test_bento_checkbox_and_priority_persist_without_consumption(window):
    full = window.trade_page.auto_bento
    page = window.trade_page
    assert full.text() == "自动吃便当"
    assert not full.isChecked()
    assert window._settings.load_trade_inputs()["auto_bento"] is False
    assert "bento_priority" not in page.collect_inputs()
    assert page.collect_ui_state()["bento_priority"] == ["work_meals", "love_bentos"]
    passenger_before = window.passenger_page.collect_inputs()
    calls = []
    for signal in (window.requestRunPcTask, window.requestRunPcTrade,
                   window.requestRunPcCombinedCommerce, window.requestRunPcPassenger):
        signal.connect(lambda *args: calls.append(args))
    full.setChecked(True)
    assert full.isChecked()
    assert window._settings.load_trade_inputs()["auto_bento"] is True
    assert not page.bento_move_buttons["work_meals", -1].isEnabled()
    assert not page.bento_move_buttons["love_bentos", 1].isEnabled()
    page.bento_move_buttons["love_bentos", -1].click()
    assert page.collect_inputs()["bento_priority"] == ["love_bentos", "work_meals"]
    full.setChecked(False)
    assert not full.isChecked()
    saved = window._settings.load_trade_inputs()
    assert saved["auto_bento"] is False
    assert saved["bento_priority"] == ["love_bentos", "work_meals"]
    reloaded = ResonanceConfigRepository(
        QSettings(window._settings.settings.fileName(), QSettings.Format.IniFormat)
    )
    from packages.resonance_gui.widgets.trade_page import TradePage

    restored = TradePage(reloaded)
    try:
        assert "bento_priority" not in restored.collect_inputs()
        assert restored.collect_ui_state()["bento_priority"] == saved["bento_priority"]
        assert not restored.auto_bento.isChecked()
        restored.bento_type_checks["work_meals"].setChecked(False)
        assert reloaded.load_trade_inputs()["bento_priority"] == ["love_bentos"]
        restored.auto_bento.setChecked(True)
        assert not restored.bento_type_checks["love_bentos"].isEnabled()
        restored.bento_type_checks["love_bentos"].click()
        assert reloaded.load_trade_inputs()["bento_priority"] == ["love_bentos"]
        restored.bento_type_checks["work_meals"].setChecked(True)
        assert restored.bento_type_checks["love_bentos"].isEnabled()
        restored.bento_type_checks["love_bentos"].click()
        assert restored.collect_inputs()["bento_priority"] == ["work_meals"]
        assert reloaded.load_trade_inputs()["bento_priority"] == ["work_meals"]
        restored.auto_bento.setChecked(False)
        assert not restored.bento_type_checks["work_meals"].isEnabled()
        restored.bento_type_checks["work_meals"].click()
        assert reloaded.load_trade_inputs()["bento_priority"] == ["work_meals"]
    finally:
        restored.close()
        restored.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert window.passenger_page.collect_inputs() == passenger_before
    assert not any("便当" in check.text() for check in window.passenger_page.findChildren(QCheckBox))
    assert calls == []


@pytest.mark.parametrize("priority", [None, "work_meals", ["unknown"],
                                     ["work_meals", "work_meals"], [["work_meals"]]])
def test_invalid_bento_priority_rejected_without_overwriting_settings(window, priority):
    before = window._settings.load_trade_inputs()
    with pytest.raises(ValueError, match="便当类型"):
        window._settings.save_trade_inputs({**before, "bento_priority": priority})
    assert window._settings.load_trade_inputs() == before


def test_empty_priority_draft_can_be_repaired_after_enabling_bento(window, monkeypatch):
    page = window.trade_page
    values = window._settings.load_trade_inputs()
    values.update(auto_bento=False, bento_priority=[])
    window._settings.save_trade_inputs(values)
    page.set_inputs(values)
    assert window._settings.load_trade_inputs()["bento_priority"] == []
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    window.trade_page.auto_bento.click()
    assert not warnings
    assert page.auto_bento.isChecked()
    assert not page.start_button.isEnabled()
    with pytest.raises(ValueError, match="至少选择一种"):
        page.collect_task_inputs()
    page.bento_type_checks["work_meals"].click()
    assert page.collect_task_inputs()["bento_priority"] == ["work_meals"]


def freight_event(sequence, stage, state, **fields):
    from packages.resonance_gui.logic import TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA

    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "freight-gui", "sequence": sequence,
        "stage": stage, "state": state, **fields,
    }}


@pytest.mark.parametrize("outcome", ["completed", "failed", "skipped", "cancelled"])
def test_bento_progress_keeps_final_sale_completed(window, outcome):
    from packages.resonance_gui.logic import reduce_trade_progress
    from PySide6.QtWidgets import QLabel

    page = window.workflow_page
    page.begin_workflow(["trade"], [], trade_inputs={"auto_bento": True})
    events = [
        freight_event(0, "planning", "completed", data={"route": [
            {"from_city": "海角城", "to_city": "岚心城", "buy_products": ["test"]},
            {"from_city": "岚心城", "to_city": "海角城", "buy_products": ["test"]},
        ]}),
        freight_event(1, "final_sale", "completed", city_index=2, current_city="海角城"),
        freight_event(2, "bento", "started", city_index=2, current_city="海角城",
                      data={"phase": "fatigue_refresh"}),
        freight_event(3, "bento", "progress", city_index=2, current_city="海角城",
                      data={"phase": "planned", "plan": {"meals": [{"kind": "work_meals"}]}}),
    ]
    for event in events:
        page.apply_progress_event("trade", event)
    progress = page._freight_progress
    assert progress.active_city_index == 2
    assert progress.active_phase == "bento"
    assert progress.current_label == "海角城 · 吃便当 · 计划 1 份"
    terminal = progress.cities[-1]
    assert [phase.key for phase in terminal.phases][-2:] == ["final_sale", "bento"]
    assert all(phase.key != "bento" for phase in progress.cities[0].phases)
    assert progress.percent < 100
    if outcome == "cancelled":
        event = freight_event(4, "task", "cancelled", data={"message": "cancelled during bento"})
    else:
        event = freight_event(4, "bento", "completed" if outcome == "skipped" else outcome,
                              city_index=2, current_city="海角城", data={
                                  "plan": {"meals": [] if outcome == "skipped" else [{"kind": "work_meals"}]},
                                  "result": {"success": outcome != "failed", "status": outcome,
                                             **({} if outcome == "skipped" else {"consumed_count": 1})},
                              })
        assert reduce_trade_progress(None, event).stage_label == "吃便当"
    page.apply_progress_event("trade", event)
    if outcome == "failed":
        page.apply_progress_event("trade", freight_event(5, "task", "failed", data={"message": "bento failed"}))
    progress = page._freight_progress
    phases = {phase.key: phase for phase in progress.cities[-1].phases}
    assert phases["final_sale"].state == "completed"
    assert phases["bento"].state == outcome
    assert "吃便当" in progress.current_label
    assert "吃便当" in page.internal_progress_label.text()
    def tree_labels(item):
        yield item.text(0)
        for index in range(item.childCount()):
            yield from tree_labels(item.child(index))
    assert page.progress_stack.currentWidget() is page.run_tree
    assert any("吃便当" in text for text in tree_labels(page.run_tree.invisibleRootItem()))
    if outcome == "failed":
        assert "已确认 1 份" in progress.current_label
        assert progress.cities[-1].state == "failed"
    if outcome in {"completed", "skipped"}:
        page.apply_progress_event("trade", freight_event(5, "task", "completed"))
        assert page._freight_progress.percent == 100


def test_disabled_bento_not_added_to_planned_city_phases():
    from packages.resonance_gui.logic import reduce_workflow_freight_progress

    event = freight_event(0, "planning", "completed", data={"route": [
        {"from_city": "海角城", "to_city": "岚心城"},
    ]})
    progress = reduce_workflow_freight_progress(None, event)
    assert all(phase.key != "bento" for city in progress.cities for phase in city.phases)
    progress = reduce_workflow_freight_progress(
        progress, freight_event(1, "final_sale", "completed", city_index=1)
    )
    progress = reduce_workflow_freight_progress(
        progress, freight_event(2, "bento", "failed", city_index=1)
    )
    assert progress.active_phase == "bento"
    assert next(phase for phase in progress.cities[-1].phases if phase.key == "final_sale").state == "completed"
