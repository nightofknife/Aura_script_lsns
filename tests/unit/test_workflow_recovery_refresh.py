"""Mandatory resource refresh gates the GUI workflow without running the game."""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QMessageBox, QPushButton, QScrollArea

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
    assert not window.run_button.isEnabled()
    assert window._busy
    pending[0] = False
    bridge.poll_current()
    assert window.run_button.isEnabled()
    assert not window._busy


def configure(window, kinds=("trade",)):
    page = window.workflow_page
    for key, check in page._task_checks.items():
        check.setChecked(key == ("battle" if kinds == ("battle",) else "commerce"))
    for key, check in page._commerce_checks.items():
        check.setChecked(key in kinds)
    page._commerce_order = list(kinds) if len(kinds) == 2 else ["trade", "passenger"]


def payload(remaining=6, count=2, love_items=()):
    return {
        "status": "success", "cid": "refresh-cid",
        "gui_item": {"task_ref": PC_PLAYER_DATA_REFRESH_TASK_REF, "kind": "workflow_task"},
        "user_data": {"player_data": {
            "status": {"fatigue": {"current": 120, "max": 800}},
            "recovery": {
                "sparkling_water": {"remaining_free_uses": remaining, "daily_free_limit": 6},
                "work_meals": {"available_count": count, "slots": []},
                "love_bentos": {"count": len(love_items), "items": deepcopy(list(love_items))},
            },
            "metadata": {"persisted": True, "profile_section_updated_at": {
                "fatigue": "2026-09-06T09:59:59Z",
                "sparkling_water": "2026-09-06T10:00:00Z",
                "work_meals": "2026-09-06T10:00:01Z", "love_bentos": "2026-09-06T10:00:02Z",
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
    assert calls[0][1] == {
        "stages": ["profile"],
        "profile_sections": ["fatigue", "sparkling_water", "work_meals", "love_bentos"],
    }
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
@pytest.mark.parametrize("love_items", [[], [
    {"role_name": "Test role", "food_name": "Test meal", "remaining_days": 3},
]])
def test_success_preserves_snapshot_logs_resources_and_dispatches_trade(window, remaining, count, love_items):
    configure(window)
    calls = []
    window.requestRunPcTrade.connect(lambda *args: calls.append(args))
    window._start_workflow()
    assert calls == []
    result = payload(remaining, count, love_items)
    finish_refresh(window, result)
    assert len(calls) == 1
    snapshot = window._workflow_recovery_snapshot
    assert snapshot["cid"] == "refresh-cid"
    assert snapshot["player_data"] == result["user_data"]["player_data"]
    assert calls[0][0]["recovery_snapshot"] == snapshot["player_data"]
    calls[0][0]["recovery_snapshot"]["status"]["fatigue"]["current"] = 999
    assert snapshot["player_data"]["status"]["fatigue"]["current"] == 120
    result["user_data"]["player_data"]["recovery"].clear()
    assert snapshot["player_data"]["recovery"]["work_meals"]["available_count"] == count
    assert snapshot["player_data"]["recovery"]["love_bentos"] == {
        "count": len(love_items), "items": love_items,
    }
    text = window.workflow_page.log_view.toPlainText()
    assert "疲劳 120/800" in text
    assert f"气泡水 {remaining}/6 次，工作餐 {count} 份，爱心便当 {len(love_items)} 份" in text
    assert window.workflow_page.task_progress_bar.value() == 1


@pytest.mark.parametrize("problem", [
    "task_failed", "missing_water", "missing_work_meals", "missing_love_bentos",
    "legacy_bento_only", "not_persisted", "bad_water", "bad_work_meals", "bool_count",
    "negative_love_count", "bool_love_count", "mismatched_love_count", "invalid_love_items",
    "missing_fatigue", "invalid_fatigue",
])
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
    elif problem == "missing_work_meals":
        del player["recovery"]["work_meals"]
    elif problem == "missing_love_bentos":
        del player["recovery"]["love_bentos"]
    elif problem == "legacy_bento_only":
        player["recovery"]["bento"] = player["recovery"].pop("work_meals")
        del player["recovery"]["love_bentos"]
    elif problem == "not_persisted":
        player["metadata"]["persisted"] = False
    elif problem == "bad_water":
        player["recovery"]["sparkling_water"]["remaining_free_uses"] = 7
    elif problem == "missing_fatigue":
        del player["status"]["fatigue"]
    elif problem == "invalid_fatigue":
        player["status"]["fatigue"] = {"current": 0, "max": 0}
    elif problem == "negative_love_count":
        player["recovery"]["love_bentos"]["count"] = -1
    elif problem == "bool_love_count":
        player["recovery"]["love_bentos"]["count"] = False
    elif problem == "mismatched_love_count":
        player["recovery"]["love_bentos"]["count"] = 1
    elif problem == "invalid_love_items":
        player["recovery"]["love_bentos"]["items"] = {}
    else:
        player["recovery"]["work_meals"]["available_count"] = True if problem == "bool_count" else 4
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
    window.workflow_page.trade_sparkling_water.setChecked(enabled)
    window.workflow_page.trade_auto_bento.setChecked(bento_enabled)
    window.trade_page.bento_move_buttons["love_bentos", -1].click()
    refreshes = []
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
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
        assert trade["auto_bento"] is bento_enabled
        assert trade["bento_priority"] == ["love_bentos", "work_meals"]
        assert trade["use_fatigue_medicine"] is False
        if len(kinds) == 2:
            assert "recovery_snapshot" not in trade
            assert "recovery_snapshot" not in inputs["passenger_inputs"]
            assert "auto_bento" not in inputs["passenger_inputs"]
            assert "bento_priority" not in inputs["passenger_inputs"]
        assert window._workflow_current == pending[0]
        assert window._settings.load_trade_inputs() == saved
        assert "recovery_snapshot" not in saved
        inputs["recovery_snapshot"]["metadata"].clear()
        assert window._workflow_recovery_snapshot["player_data"]["metadata"]["persisted"]
        window._finish_workflow(True, "done")
    assert calls[0]["recovery_snapshot"]["status"]["fatigue"]["current"] == 120
    assert calls[1]["recovery_snapshot"]["status"]["fatigue"]["current"] == 230
    assert len(refreshes) == 2


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
@pytest.mark.parametrize("water,bento", [(True, False), (False, True), (True, True), (False, False)])
def test_opted_in_standalone_freight_refreshes_before_actual_run(window, entry, water, bento):
    window.trade_page.auto_sparkling_water.setChecked(water)
    window.trade_page.auto_bento.setChecked(bento)
    trades, refreshes = [], []
    window.requestRunPcTrade.connect(lambda inputs, _timeout: trades.append(inputs))
    window.requestRunPcCombinedCommerce.connect(lambda inputs, _timeout: trades.append(inputs))
    window.requestRunPcTask.connect(lambda *args: refreshes.append(args))
    window._workflow_recovery_snapshot = {"player_data": {"stale": True}}
    if entry == "full":
        window.trade_page._request_start()
    else:
        window._start_commerce_sequence(True, entry == "overview_combined")
    if not (water or bento):
        assert refreshes == []
        assert len(trades) == 1
        assert "recovery_snapshot" not in trades[0]
        return
    assert trades == []
    assert len(refreshes) == 1
    assert refreshes[0][0] == PC_PLAYER_DATA_REFRESH_TASK_REF
    assert window._workflow_recovery_snapshot == {}
    assert window._workflow_current["step"] == "refresh_recovery"
    saved = window._settings.load_trade_inputs()
    finish_refresh(window, payload())
    assert len(trades) == 1
    assert len(refreshes) == 1
    trade = trades[0]["trade_inputs"] if entry == "overview_combined" else trades[0]
    assert trade["auto_sparkling_water"] is water
    assert trade["auto_bento"] is bento
    assert trade["bento_priority"] == ["work_meals", "love_bentos"]
    assert trades[0]["recovery_snapshot"] == payload()["user_data"]["player_data"]
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
    assert len(trades) == len(passengers) == 1
    assert "recovery_snapshot" not in trades[0]
    assert "recovery_snapshot" not in passengers[0]
    assert refreshes == []


def test_workflow_passenger_does_not_receive_snapshot(window):
    configure(window, ("passenger",))
    window.trade_page.auto_bento.setChecked(True)
    calls = []
    window.requestRunPcPassenger.connect(lambda inputs, _timeout: calls.append(inputs))
    window._start_workflow()
    finish_refresh(window, payload())
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
              "auto_bento": True, "bento_priority": ["love_bentos"], "base_fatigue_reserve": 200,
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
        assert "base_fatigue_reserve" not in inputs
        assert "arrival_timeout_seconds" not in inputs
        assert "use_fatigue_medicine" not in inputs
    assert preview.auto_bento.isHidden()
    assert preview.bento_priority_panel.isHidden()
    assert "auto_bento" not in window._settings.load_trade_preview_inputs()
    assert "bento_priority" not in window._settings.load_trade_preview_inputs()
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
    trade = {"auto_sparkling_water": True, "auto_bento": True,
             "bento_priority": ["love_bentos", "work_meals"]}
    if combined_run:
        source["trade_inputs"] = trade
        bridge.run_pc_combined_commerce(source)
    else:
        source.update(trade)
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
@pytest.mark.parametrize("size_name,size", [("normal", (1440, 860)), ("minimum", (1180, 720))])
def test_sparkling_water_checkbox_render_and_busy_state(window, tmp_path, editor, size_name, size):
    configure(window)
    window.resize(*size)
    window.workflow_page._select_task("commerce")
    if editor == "full":
        window.workflow_page.show_trade_editor()
        check = window.trade_page.auto_sparkling_water
        bento = window.trade_page.auto_bento
    else:
        check = window.workflow_page.trade_sparkling_water
        bento = window.workflow_page.trade_auto_bento
    window.show()
    QApplication.processEvents()
    assert (window.width(), window.height()) == size
    if editor == "full":
        scroll = window.trade_page.parameter_panel.findChild(QScrollArea)
        scroll.ensureWidgetVisible(window.trade_page.bento_priority_panel)
        QApplication.processEvents()
    else:
        page = window.workflow_page
        scroll = page.commerce_config_scroll
        assert scroll.horizontalScrollBar().maximum() == 0
        if size_name == "minimum":
            assert scroll.verticalScrollBar().maximum() > 0
        for row in page._commerce_rows.values():
            for button in (row.up_button, row.down_button):
                assert row.rect().contains(button.geometry())
                assert button.height() >= button.sizeHint().height()
        scroll.ensureWidgetVisible(page.trade_auto_bento)
        QApplication.processEvents()
        for spin in (page.trade_fatigue, page.trade_books, page.trade_cargo):
            assert spin.height() >= spin.sizeHint().height()
            edit = spin.findChild(QLineEdit)
            assert edit.contentsRect().height() >= edit.fontMetrics().height()
            assert scroll.viewport().rect().contains(spin.mapTo(scroll.viewport(), spin.rect().topLeft()))
            assert scroll.viewport().rect().contains(spin.mapTo(scroll.viewport(), spin.rect().bottomRight()))
    assert check.isVisible()
    assert check.width() >= check.sizeHint().width()
    assert bento.isVisible()
    assert bento.width() >= bento.sizeHint().width()
    assert check.geometry().right() < bento.geometry().left()
    assert check.geometry().center().y() == bento.geometry().center().y()
    assert not window.grab().isNull()
    screenshot = tmp_path / "screenshots"
    screenshot.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(screenshot / f"{editor}-{size_name}.png"))
    if editor == "quick" and size_name == "minimum":
        before = scroll.verticalScrollBar().value()
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        QApplication.processEvents()
        assert scroll.verticalScrollBar().value() > before
        button = next(button for button in page.commerce_tabs.currentWidget().findChildren(QPushButton)
                      if button.text() == "打开完整货运参数")
        assert scroll.viewport().rect().contains(button.mapTo(scroll.viewport(), button.rect().bottomRight()))
        assert window.grab().save(str(screenshot / "quick-minimum-scrolled.png"))
    if editor == "full":
        page = window.trade_page
        scroll = page.parameter_panel.findChild(QScrollArea)
        scroll.ensureWidgetVisible(page.bento_priority_panel)
        QApplication.processEvents()
        for key, check_type in page.bento_type_checks.items():
            row = page._bento_rows[key]
            assert check_type.width() >= check_type.sizeHint().width()
            assert row.mapTo(scroll.viewport(), row.rect().bottomRight()).y() < scroll.viewport().height()
            for delta in (-1, 1):
                button = page.bento_move_buttons[key, delta]
                assert button.toolTip()
                assert button.width() == button.height() == 28
        assert window.grab().save(str(tmp_path / "bento-priority-full.png"))
    window._start_workflow()
    QApplication.processEvents()
    assert not check.isEnabled()
    assert not bento.isEnabled()
    assert not window.trade_page.bento_priority_panel.isEnabled()
    window._finish_workflow(True, "done")
    assert check.isEnabled()
    assert bento.isEnabled()
    assert window.trade_page.bento_priority_panel.isEnabled()


def test_bento_checkbox_and_priority_persist_without_consumption(window):
    quick, full = window.workflow_page.trade_auto_bento, window.trade_page.auto_bento
    page = window.trade_page
    assert quick.text() == full.text() == "自动吃便当"
    assert not quick.isChecked() and not full.isChecked()
    assert window._settings.load_trade_inputs()["auto_bento"] is False
    assert page.collect_inputs()["bento_priority"] == ["work_meals", "love_bentos"]
    passenger_before = window.passenger_page.collect_inputs()
    calls = []
    for signal in (window.requestRunPcTask, window.requestRunPcTrade,
                   window.requestRunPcCombinedCommerce, window.requestRunPcPassenger):
        signal.connect(lambda *args: calls.append(args))
    quick.setChecked(True)
    assert full.isChecked()
    assert window._settings.load_trade_inputs()["auto_bento"] is True
    assert not page.bento_move_buttons["work_meals", -1].isEnabled()
    assert not page.bento_move_buttons["love_bentos", 1].isEnabled()
    page.bento_move_buttons["love_bentos", -1].click()
    assert page.collect_inputs()["bento_priority"] == ["love_bentos", "work_meals"]
    full.setChecked(False)
    assert not quick.isChecked()
    saved = window._settings.load_trade_inputs()
    assert saved["auto_bento"] is False
    assert saved["bento_priority"] == ["love_bentos", "work_meals"]
    reloaded = ResonanceConfigRepository(
        QSettings(window._settings.settings.fileName(), QSettings.Format.IniFormat)
    )
    from packages.resonance_gui.widgets.trade_page import TradePage

    restored = TradePage(reloaded)
    try:
        assert restored.collect_inputs()["bento_priority"] == saved["bento_priority"]
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
        restored.bento_type_checks["work_meals"].click()
        assert reloaded.load_trade_inputs()["bento_priority"] == []
    finally:
        restored.close()
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


def test_empty_priority_is_saved_only_when_disabled(window, monkeypatch):
    page = window.trade_page
    for check in page.bento_type_checks.values():
        check.click()
    assert window._settings.load_trade_inputs()["bento_priority"] == []
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    window.workflow_page.trade_auto_bento.click()
    assert warnings
    assert not page.auto_bento.isChecked()
    assert not window.workflow_page.trade_auto_bento.isChecked()
    with pytest.raises(ValueError, match="至少选择一种"):
        window._settings.save_trade_inputs({"auto_bento": True, "bento_priority": []})


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
    page.begin_workflow(["commerce"], ["trade"], trade_inputs={"auto_bento": True})
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
    assert phases["bento"].state == ("failed" if outcome == "cancelled" else outcome)
    assert "吃便当" in progress.current_label
    assert "吃便当" in page.internal_progress_label.text()
    assert any("吃便当" in label.text() for label in page.timeline_view.findChildren(QLabel))
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
