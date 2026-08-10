from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow


class _IdleRunner:
    def close(self) -> None:
        return None


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(tmp_path) -> ResonanceMainWindow:
    _app()
    settings = QSettings(str(tmp_path / "main-window.ini"), QSettings.Format.IniFormat)
    window = ResonanceMainWindow(
        bridge=RunnerBridge(runner_factory=_IdleRunner),
        settings=ResonanceConfigRepository(settings=settings),
        initialize_on_startup=False,
    )
    window.resize(1280, 820)
    window.show()
    QApplication.processEvents()
    return window


def test_available_update_is_only_shown_in_window_title(tmp_path):
    window = _window(tmp_path)
    try:
        original_child_count = len(window.findChildren(QWidget))

        window._show_available_update("v9.9.9")

        assert window.windowTitle() == "Aura 雷索纳斯控制台 · 发现新版本 v9.9.9"
        assert len(window.findChildren(QWidget)) == original_child_count
    finally:
        window.close()


def test_main_window_groups_freight_and_passenger_under_commerce_navigation(tmp_path):
    window = _window(tmp_path)
    try:
        assert [button.text() for button in window.nav_buttons] == [
            "跑商",
            "战斗",
            "任务工具",
            "历史",
            "设置",
        ]
        assert [button.text() for button in window.commerce_page.section_buttons] == [
            "总览",
            "货运",
            "客运",
        ]
        assert window.page_stack.currentWidget() is window.commerce_page
        assert window.commerce_page.section_stack.currentWidget() is window.commerce_page.overview_page
        assert window.commerce_page.overview_page.freight_checkbox.isChecked()
        assert window.commerce_page.overview_page.passenger_checkbox.isChecked()

        window.commerce_page.section_buttons[2].click()
        assert window.page_stack.currentWidget() is window.commerce_page
        assert window.commerce_page.section_stack.currentWidget() is window.passenger_page

        window.nav_buttons[1].click()
        assert window.page_stack.currentWidget() is window.battle_page
        window.nav_buttons[0].click()
        assert window.commerce_page.section_stack.currentWidget() is window.commerce_page.overview_page
    finally:
        window.close()


def test_commerce_overview_uses_live_inputs_and_runs_trade_then_passenger(tmp_path):
    window = _window(tmp_path)
    trade_requests: list[tuple[dict, float]] = []
    passenger_requests: list[tuple[dict, float]] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTrade.connect(
            lambda inputs, timeout: trade_requests.append((dict(inputs), float(timeout)))
        )
        window.requestRunPcPassenger.connect(
            lambda inputs, timeout: passenger_requests.append((dict(inputs), float(timeout)))
        )

        window.trade_page.fatigue_budget.setValue(321)
        window.passenger_page.round_trips.setValue(3)
        overview = window.commerce_page.overview_page
        overview.run_button.click()

        assert trade_requests[0][0]["fatigue_budget"] == 321
        assert passenger_requests == []
        assert overview.is_running
        assert overview.run_button.text() == "停止"
        assert window.commerce_page.section_stack.currentWidget() is overview

        trade_item = {"game_name": "resonance_pc", "kind": "trade_run", "label": "PC 自动跑商"}
        window._on_busy_changed(True)
        window._on_task_started(trade_item)
        window._on_task_dispatched(
            {"item": trade_item, "cid": "trade-cid", "dispatch": {"status": "queued"}}
        )
        assert window.commerce_page.section_stack.currentWidget() is overview
        window._on_task_finished(
            {
                "cid": "trade-cid",
                "status": "success",
                "gui_item": trade_item,
                "final_result": {"user_data": {"success": True}},
            }
        )
        window._on_busy_changed(False)

        assert passenger_requests[0][0]["round_trips"] == 3
        passenger_item = {
            "game_name": "resonance_pc",
            "kind": "passenger_run",
            "label": "PC 独立客运",
        }
        window._on_busy_changed(True)
        window._on_task_started(passenger_item)
        window._on_task_finished(
            {
                "cid": "passenger-cid",
                "status": "success",
                "gui_item": passenger_item,
                "final_result": {"user_data": {"success": True}},
            }
        )
        window._on_busy_changed(False)

        assert not overview.is_running
        assert overview.run_button.text() == "运行"
        assert overview.freight_checkbox.isEnabled()
        assert overview.passenger_checkbox.isEnabled()
    finally:
        window.close()


def test_commerce_overview_stop_cancels_current_and_drops_passenger(tmp_path):
    window = _window(tmp_path)
    trade_requests: list[dict] = []
    passenger_requests: list[dict] = []
    cancel_requests: list[bool] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTrade.connect(lambda inputs, _timeout: trade_requests.append(dict(inputs)))
        window.requestRunPcPassenger.connect(
            lambda inputs, _timeout: passenger_requests.append(dict(inputs))
        )
        window.requestCancelCurrent.connect(lambda: cancel_requests.append(True))

        overview = window.commerce_page.overview_page
        overview.run_button.click()
        assert len(trade_requests) == 1
        window._on_busy_changed(True)
        overview.run_button.click()

        assert overview.is_stopping
        assert overview.run_button.text() == "停止中…"
        assert cancel_requests == [True]

        trade_item = {"game_name": "resonance_pc", "kind": "trade_run", "label": "PC 自动跑商"}
        window._on_task_started(trade_item)
        window._on_task_finished(
            {
                "cid": "trade-cid",
                "status": "cancelled",
                "gui_item": trade_item,
                "final_result": {"user_data": {"success": False}},
            }
        )
        window._on_busy_changed(False)

        assert passenger_requests == []
        assert not overview.is_running
        assert overview.run_button.text() == "运行"
    finally:
        window.close()


def test_commerce_overview_failure_stops_sequence(tmp_path):
    window = _window(tmp_path)
    passenger_requests: list[dict] = []
    cancel_requests: list[bool] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTrade.connect(lambda _inputs, _timeout: None)
        window.requestRunPcPassenger.connect(
            lambda inputs, _timeout: passenger_requests.append(dict(inputs))
        )
        window.requestCancelCurrent.connect(lambda: cancel_requests.append(True))

        overview = window.commerce_page.overview_page
        overview.run_button.click()
        window._on_busy_changed(True)
        window._on_task_failed(
            {"stage": "poll_run", "error": "状态读取失败", "recoverable": True}
        )

        assert overview.is_stopping
        assert cancel_requests == [True]
        window._on_busy_changed(False)
        assert passenger_requests == []
        assert not overview.is_running
    finally:
        window.close()


def test_commerce_overview_requires_at_least_one_selection(tmp_path):
    window = _window(tmp_path)
    try:
        overview = window.commerce_page.overview_page
        overview.freight_checkbox.setChecked(False)
        overview.passenger_checkbox.setChecked(False)
        assert not overview.run_button.isEnabled()
    finally:
        window.close()


def test_commerce_overview_can_run_passenger_only_with_live_inputs(tmp_path):
    window = _window(tmp_path)
    trade_requests: list[dict] = []
    passenger_requests: list[dict] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTrade.connect(lambda inputs, _timeout: trade_requests.append(dict(inputs)))
        window.requestRunPcPassenger.connect(
            lambda inputs, _timeout: passenger_requests.append(dict(inputs))
        )

        overview = window.commerce_page.overview_page
        overview.freight_checkbox.setChecked(False)
        window.passenger_page.round_trips.setValue(4)
        overview.run_button.click()

        assert trade_requests == []
        assert passenger_requests[0]["round_trips"] == 4
    finally:
        window.close()


def test_commerce_overview_invalid_trade_inputs_open_trade_page(tmp_path, monkeypatch):
    window = _window(tmp_path)
    warnings: list[tuple[str, str]] = []
    try:
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda _parent, title, message: warnings.append((str(title), str(message))),
        )
        for checkbox in window.trade_page.city_checks.values():
            checkbox.setChecked(False)
        next(iter(window.trade_page.city_checks.values())).setChecked(True)

        overview = window.commerce_page.overview_page
        overview.run_button.click()

        assert not overview.is_running
        assert window.commerce_page.section_stack.currentWidget() is window.trade_page
        assert warnings and warnings[0][0] == "货运参数错误"
    finally:
        window.close()


def test_main_window_routes_pc_battle_lifecycle_away_from_trade_page(tmp_path):
    window = _window(tmp_path)
    try:
        item = {
            "game_name": "resonance_pc",
            "kind": "battle_run",
            "label": "PC 自动战斗",
        }
        window._on_task_started(item)
        window._on_task_dispatched(
            {
                "item": item,
                "cid": "battle-cid",
                "dispatch": {"cid": "battle-cid", "status": "queued"},
            }
        )

        assert window.page_stack.currentWidget() is window.battle_page
        assert window.battle_page.is_busy()
        assert not window.trade_page.is_busy()
        assert window.battle_page.cid_value.text() == "battle-cid"

        window._on_run_updated({"cid": "battle-cid", "status": "running"})
        assert window.battle_page.run_status_value.text() == "运行中"

        window._on_task_finished(
            {
                "cid": "battle-cid",
                "status": "success",
                "gui_item": item,
                "final_result": {"user_data": {"success": True}},
            }
        )
        assert window.battle_page.run_status_value.text() == "已完成"
        assert not window.battle_page.is_busy()
    finally:
        window.close()


def test_history_filter_routes_battle_rows_to_battle_page(tmp_path):
    window = _window(tmp_path)
    try:
        rows = [
            {
                "cid": "trade-cid",
                "status": "success",
                "task_ref": "tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc",
            },
            {
                "cid": "battle-cid",
                "status": "success",
                "task_ref": "tasks:auto_battle_dispatch_pc.yaml:auto_battle_dispatch_pc",
            },
        ]
        window._on_history_loaded(rows)
        window.history_filter.setCurrentIndex(window.history_filter.findData("battle"))

        assert window.history_table.rowCount() == 1
        assert window.history_table.item(0, 0).text() == "battle-cid"
        window._open_history_row(0, 0)
        assert window.page_stack.currentWidget() is window.battle_page
        assert window.battle_page.cid_value.text() == "battle-cid"
    finally:
        window.close()


def test_main_window_routes_pc_passenger_lifecycle_to_passenger_page(tmp_path):
    window = _window(tmp_path)
    try:
        item = {
            "game_name": "resonance_pc",
            "kind": "passenger_run",
            "label": "PC 独立客运",
        }
        window._on_task_started(item)
        window._on_task_dispatched(
            {
                "item": item,
                "cid": "passenger-cid",
                "dispatch": {"cid": "passenger-cid", "status": "queued"},
            }
        )

        assert window.page_stack.currentWidget() is window.commerce_page
        assert window.commerce_page.section_stack.currentWidget() is window.passenger_page
        assert window.passenger_page.is_busy()
        assert not window.trade_page.is_busy()
        assert window.passenger_page.cid_value.text() == "passenger-cid"

        window._on_task_finished(
            {
                "cid": "passenger-cid",
                "status": "success",
                "gui_item": item,
                "final_result": {
                    "user_data": {
                        "success": True,
                        "status": "completed",
                        "requested_round_trips": 1,
                        "completed_legs": [{}, {}],
                    }
                },
            }
        )
        assert window.passenger_page.run_status_value.text() == "已完成"
        assert not window.passenger_page.is_busy()
    finally:
        window.close()
