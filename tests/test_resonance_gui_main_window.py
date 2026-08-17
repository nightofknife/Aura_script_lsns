from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QTreeWidget,
    QWidget,
)

from packages.resonance_gui.app import _configure_application
from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.logic import TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA


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


def test_fixed_light_theme_remains_readable_with_dark_system_palette(tmp_path):
    app = _app()
    original_palette = app.palette()
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.ColorRole.Window, QColor("#202124"))
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor("#f1f3f4"))
    dark_palette.setColor(QPalette.ColorRole.Base, QColor("#202124"))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor("#f1f3f4"))
    dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f1f3f4"))
    app.setPalette(dark_palette)
    window = None
    try:
        _configure_application(app)
        window = _window(tmp_path)
        for widget_type in (
            QLineEdit,
            QSpinBox,
            QComboBox,
            QTreeWidget,
            QCheckBox,
            QTextBrowser,
        ):
            widget = window.findChild(widget_type)
            assert widget is not None
            palette = widget.palette()
            assert palette.color(QPalette.ColorRole.Text).lightness() < 128
            assert palette.color(QPalette.ColorRole.WindowText).lightness() < 128
        line_edit = window.findChild(QLineEdit)
        assert line_edit.palette().color(QPalette.ColorRole.Base).lightness() > 200
    finally:
        if window is not None:
            window.close()
        app.setPalette(original_palette)


def test_game_path_is_not_detected_automatically_and_manual_path_is_only_validated_on_click(
    tmp_path, monkeypatch
):
    executable = tmp_path / "雷索纳斯.exe"
    executable.touch()
    settings = QSettings(str(tmp_path / "manual-path.ini"), QSettings.Format.IniFormat)
    settings.setValue("game/executable_path", str(executable))
    registry_calls: list[bool] = []
    monkeypatch.setattr(
        "packages.resonance_gui.widgets.settings_hub_page.find_registry_executables",
        lambda **_kwargs: registry_calls.append(True) or (),
    )
    window = ResonanceMainWindow(
        bridge=RunnerBridge(runner_factory=_IdleRunner),
        settings=ResonanceConfigRepository(settings=settings),
        initialize_on_startup=False,
    )
    try:
        assert window.settings_page.detect_result.text() == "未检测"
        assert registry_calls == []
        window.settings_page._detect_executable()
        assert window.settings_page.detect_result.text() == "用户路径验证通过"
        assert registry_calls == []
    finally:
        window.close()


def test_empty_game_path_is_filled_from_registry_only_after_detect_click(tmp_path, monkeypatch):
    executable = tmp_path / "雷索纳斯.exe"
    executable.touch()
    registry_calls: list[bool] = []
    monkeypatch.setattr(
        "packages.resonance_gui.widgets.settings_hub_page.find_registry_executables",
        lambda **_kwargs: registry_calls.append(True) or (executable,),
    )
    window = _window(tmp_path)
    try:
        assert window.settings_page.executable_path.text() == ""
        assert registry_calls == []
        window.settings_page._detect_executable()
        assert registry_calls == [True]
        assert window.settings_page.executable_path.text() == str(executable)
        assert window.settings_page.detect_result.text() == "已从注册表检测到游戏"
    finally:
        window.close()


def test_browsing_game_path_does_not_run_detection(tmp_path, monkeypatch):
    executable = tmp_path / "雷索纳斯.exe"
    executable.touch()
    registry_calls: list[bool] = []
    monkeypatch.setattr(
        "packages.resonance_gui.widgets.settings_hub_page.find_registry_executables",
        lambda **_kwargs: registry_calls.append(True) or (),
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(executable), "程序 (*.exe)"),
    )
    window = _window(tmp_path)
    try:
        window.settings_page._browse_executable()
        assert window.settings_page.executable_path.text() == str(executable)
        assert window.settings_page.detect_result.text() == "未检测"
        assert registry_calls == []
    finally:
        window.close()


def test_main_window_opens_with_four_task_workflow_and_independent_commerce_order(tmp_path):
    window = _window(tmp_path)
    try:
        assert window.page_stack.currentWidget() is window.workflow_page
        assert window.workflow_page.workflow_steps() == [
            "startup", "commerce", "battle", "close"
        ]
        assert window.workflow_page.commerce_steps() == ["trade", "passenger"]
        window.workflow_page._select_task("commerce")
        window.workflow_page._move_current(-1)
        assert window.workflow_page.workflow_steps() == [
            "commerce", "startup", "battle", "close"
        ]
        window.workflow_page._swap_commerce_order()
        assert window.workflow_page.commerce_steps() == ["passenger", "trade"]
        window.workflow_page.settings_button.click()
        assert window.page_stack.currentWidget() is window.settings_page
        window.settings_page.backRequested.emit()
        assert window.page_stack.currentWidget() is window.workflow_page
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
        window.passenger_page.trip_count.setValue(3)
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

        assert passenger_requests[0][0]["trip_count"] == 3
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
        window.passenger_page.trip_count.setValue(4)
        overview.run_button.click()

        assert trade_requests == []
        assert passenger_requests[0]["trip_count"] == 4
    finally:
        window.close()


def test_workflow_runs_startup_trade_passenger_and_close_in_order(tmp_path):
    window = _window(tmp_path)
    pc_tasks: list[str] = []
    trade_requests: list[dict] = []
    passenger_requests: list[dict] = []
    try:
        window.requestRunPcTask.disconnect()
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTask.connect(
            lambda task_ref, _inputs, _label, _timeout: pc_tasks.append(str(task_ref))
        )
        window.requestRunPcTrade.connect(
            lambda inputs, _timeout: trade_requests.append(dict(inputs))
        )
        window.requestRunPcPassenger.connect(
            lambda inputs, _timeout: passenger_requests.append(dict(inputs))
        )
        window.workflow_page._task_checks["battle"].setChecked(False)

        window.workflow_page.run_button.click()
        assert pc_tasks == ["tasks:game_startup_pc.yaml:enter_main"]

        startup_item = {"game_name": "resonance_pc", "kind": "workflow_task", "label": "进入主界面"}
        window._on_busy_changed(True)
        window._on_task_started(startup_item)
        window._on_task_finished({
            "status": "success", "gui_item": startup_item,
            "final_result": {"user_data": {"success": True, "status": "completed"}},
        })
        window._on_busy_changed(False)
        assert len(trade_requests) == 1
        assert trade_requests[0]["fatigue_budget"] == 624
        assert trade_requests[0]["required_end_city_ids"] == ["11", "15"]
        assert window._settings.load_trade_inputs()["fatigue_budget"] == 700

        trade_item = {"game_name": "resonance_pc", "kind": "trade_run", "label": "货运"}
        window._on_busy_changed(True)
        window._on_task_started(trade_item)
        window._on_task_finished({
            "status": "success", "gui_item": trade_item,
            "final_result": {"user_data": {
                "success": True,
                "status": "completed",
                "route": [{"from_city_id": "3", "to_city_id": "15", "to_city": "岚心城"}],
                "execution": {
                    "completed_leg_count": 1,
                    "cape_island_triggered_count": 0,
                },
                "final_sale": {"success": True, "page_state": "city_main"},
                "page_state": "city_main",
            }},
        })
        window._on_busy_changed(False)
        assert len(passenger_requests) == 1
        assert passenger_requests[0]["reposition_to_route"] is False
        assert window._settings.load_passenger_inputs()["reposition_to_route"] is True

        passenger_item = {"game_name": "resonance_pc", "kind": "passenger_run", "label": "客运"}
        window._on_busy_changed(True)
        window._on_task_started(passenger_item)
        window._on_task_finished({
            "status": "success", "gui_item": passenger_item,
            "final_result": {"user_data": {
                "success": True,
                "status": "completed",
                "requested_trips": 1,
                "completed_trips": 1,
                "expected_fatigue_used": 76,
                "requires_manual_completion": False,
                "loaded_destination": None,
                "page_state": "city_main",
            }},
        })
        window._on_busy_changed(False)
        assert pc_tasks[-1] == "tasks:game_startup_pc.yaml:close_game"

        close_item = {"game_name": "resonance_pc", "kind": "workflow_task", "label": "关闭游戏"}
        window._on_busy_changed(True)
        window._on_task_started(close_item)
        window._on_task_finished({
            "status": "success", "gui_item": close_item,
            "final_result": {"user_data": {"success": True, "status": "stopped"}},
        })
        window._on_busy_changed(False)

        assert not window.workflow_page.is_running()
        assert "全部启用任务已完成" in window.workflow_page.progress_label.text()
    finally:
        window.close()


def test_combined_workflow_passenger_first_derives_trade_budget_after_reposition(tmp_path):
    window = _window(tmp_path)
    trade_requests: list[dict] = []
    passenger_requests: list[dict] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTrade.connect(
            lambda inputs, _timeout: trade_requests.append(dict(inputs))
        )
        window.requestRunPcPassenger.connect(
            lambda inputs, _timeout: passenger_requests.append(dict(inputs))
        )
        for step in ("startup", "battle", "close"):
            window.workflow_page._task_checks[step].setChecked(False)
        window.workflow_page._swap_commerce_order()
        window.workflow_page.trade_fatigue.setValue(200)

        window.workflow_page.run_button.click()

        assert len(passenger_requests) == 1
        assert passenger_requests[0]["reposition_to_route"] is True
        assert trade_requests == []

        passenger_item = {"game_name": "resonance_pc", "kind": "passenger_run", "label": "客运"}
        window._on_busy_changed(True)
        window._on_task_started(passenger_item)
        window._on_task_finished({
            "status": "success",
            "gui_item": passenger_item,
            "final_result": {"user_data": {
                "success": True,
                "status": "completed",
                "requested_trips": 1,
                "completed_trips": 1,
                "expected_fatigue_used": 91,
                "requires_manual_completion": False,
                "loaded_destination": None,
                "page_state": "city_main",
            }},
        })
        window._on_busy_changed(False)

        assert len(trade_requests) == 1
        assert trade_requests[0]["fatigue_budget"] == 109
        assert "required_end_city_ids" not in trade_requests[0]
        assert window._settings.load_trade_inputs()["fatigue_budget"] == 200

        trade_item = {"game_name": "resonance_pc", "kind": "trade_run", "label": "货运"}
        window._on_busy_changed(True)
        window._on_task_started(trade_item)
        window._on_task_finished({
            "status": "success",
            "gui_item": trade_item,
            "final_result": {"user_data": {"success": True, "status": "completed"}},
        })
        window._on_busy_changed(False)
        assert not window.workflow_page.is_running()
    finally:
        window.close()


def test_workflow_single_commerce_modes_keep_independent_inputs(tmp_path):
    window = _window(tmp_path)
    trade_requests: list[dict] = []
    passenger_requests: list[dict] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTrade.connect(
            lambda inputs, _timeout: trade_requests.append(dict(inputs))
        )
        window.requestRunPcPassenger.connect(
            lambda inputs, _timeout: passenger_requests.append(dict(inputs))
        )
        for step in ("startup", "battle", "close"):
            window.workflow_page._task_checks[step].setChecked(False)
        window.workflow_page._commerce_checks["passenger"].setChecked(False)
        window.workflow_page.trade_fatigue.setValue(321)

        window.workflow_page.run_button.click()

        assert trade_requests[0]["fatigue_budget"] == 321
        assert "required_end_city_ids" not in trade_requests[0]
        assert passenger_requests == []
        assert window.workflow_page.trade_fatigue_label.text() == "货运疲劳预算"
        trade_item = {"game_name": "resonance_pc", "kind": "trade_run", "label": "货运"}
        window._on_busy_changed(True)
        window._on_task_started(trade_item)
        window._on_task_finished({
            "status": "success",
            "gui_item": trade_item,
            "final_result": {"user_data": {"success": True, "status": "completed"}},
        })
        window._on_busy_changed(False)
    finally:
        window.close()


def test_combined_trade_handoff_rejects_wrong_terminal_city(tmp_path):
    window = _window(tmp_path)
    passenger_requests: list[dict] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcPassenger.disconnect()
        window.requestRunPcTrade.connect(lambda _inputs, _timeout: None)
        window.requestRunPcPassenger.connect(
            lambda inputs, _timeout: passenger_requests.append(dict(inputs))
        )
        for step in ("startup", "battle", "close"):
            window.workflow_page._task_checks[step].setChecked(False)

        window.workflow_page.run_button.click()
        trade_item = {"game_name": "resonance_pc", "kind": "trade_run", "label": "货运"}
        window._on_busy_changed(True)
        window._on_task_started(trade_item)
        window._on_task_finished({
            "status": "success",
            "gui_item": trade_item,
            "final_result": {"user_data": {
                "success": True,
                "status": "completed",
                "route": [{"from_city_id": "3", "to_city_id": "8", "to_city": "淘金乐园"}],
                "execution": {"completed_leg_count": 1},
                "final_sale": {"success": True, "page_state": "city_main"},
                "page_state": "city_main",
            }},
        })
        window._on_busy_changed(False)

        assert passenger_requests == []
        assert not window.workflow_page.is_running()
        assert "任一端点" in window.workflow_page.progress_label.text()
    finally:
        window.close()


def test_workflow_task_and_commerce_order_persist(tmp_path):
    window = _window(tmp_path)
    try:
        window.workflow_page._select_task("commerce")
        window.workflow_page._move_current(-1)
        window.workflow_page._swap_commerce_order()
    finally:
        window.close()

    reopened = _window(tmp_path)
    try:
        assert reopened.workflow_page.workflow_steps() == [
            "commerce", "startup", "battle", "close"
        ]
        assert reopened.workflow_page.commerce_steps() == ["passenger", "trade"]
        assert all(row.isVisible() for row in reopened.workflow_page._task_rows.values())
        assert all(row.name_label.text() for row in reopened.workflow_page._task_rows.values())
    finally:
        reopened.close()


def test_workflow_task_drop_reorders_without_move_buttons(tmp_path):
    window = _window(tmp_path)
    try:
        assert not hasattr(window.workflow_page, "up_button")
        assert not hasattr(window.workflow_page, "down_button")
        window.workflow_page._drop_task("close", 0)
        assert window.workflow_page.workflow_steps() == [
            "close", "startup", "commerce", "battle"
        ]
        assert [
            window.workflow_page._task_rows[task_id].number_label.text()
            for task_id in window.workflow_page._task_order
        ] == ["1", "2", "3", "4"]
    finally:
        window.close()


def test_workflow_drag_is_limited_to_handle_and_shows_drop_indicator(tmp_path):
    window = _window(tmp_path)
    try:
        row = window.workflow_page._task_rows["startup"]
        assert row.cursor().shape() == Qt.CursorShape.ArrowCursor
        assert row.drag_handle.cursor().shape() == Qt.CursorShape.OpenHandCursor
        host = window.workflow_page.task_rows_host
        host._show_drop_indicator(2)
        assert host._drop_indicator.isVisible()
        assert host._drop_indicator.height() == 3
    finally:
        window.close()


def test_full_commerce_parameters_open_inside_workflow_center(tmp_path):
    window = _window(tmp_path)
    try:
        window.workflow_page._select_task("commerce")
        window.workflow_page.openTradeRequested.emit()
        assert window.page_stack.currentWidget() is window.workflow_page
        assert window.workflow_page.center_stack.currentWidget() is window.workflow_page.trade_editor_page
        assert window.trade_page.parameter_panel.parent() is window.workflow_page.trade_editor_page
        window.workflow_page.show_commerce_summary()
        window.workflow_page.openPassengerRequested.emit()
        assert window.page_stack.currentWidget() is window.workflow_page
        assert (
            window.workflow_page.center_stack.currentWidget()
            is window.workflow_page.passenger_editor_page
        )
        assert window.passenger_page.parameter_panel.parent() is window.workflow_page.passenger_editor_page
    finally:
        window.close()


def test_trade_preview_is_a_standalone_center_tool_not_a_workflow_task(tmp_path):
    window = _window(tmp_path)
    preview_requests: list[dict] = []
    try:
        window.requestPreviewPcTrade.disconnect()
        window.requestPreviewPcTrade.connect(
            lambda inputs, _timeout: preview_requests.append(dict(inputs))
        )
        original_steps = window.workflow_page.workflow_steps()
        window.workflow_page.show_trade_editor()
        window.workflow_page.trade_preview_button.click()

        assert len(preview_requests) == 1
        assert window.workflow_page.workflow_steps() == original_steps
        assert (
            window.workflow_page.center_stack.currentWidget()
            is window.workflow_page.trade_preview_page
        )
        assert window.workflow_page.trade_preview_page.isAncestorOf(
            window.trade_page.execution_panel
        )
    finally:
        window.close()


def test_trade_summary_defaults_and_advanced_arrival_timeout(tmp_path):
    window = _window(tmp_path)
    try:
        assert window.workflow_page.trade_fatigue.value() == 700
        assert window.workflow_page.trade_cargo.value() == 750
        assert not window.workflow_page.trade_medicine.isChecked()
        assert window.workflow_page.trade_investment.isChecked()
        assert not hasattr(window.workflow_page, "trade_arrival")
        assert window.settings_page.trade_arrival_timeout.value() == 60
        assert window.trade_page.arrival_timeout_minutes.parent() is not None
        assert not window.trade_page.arrival_timeout_minutes.isVisible()

        window.settings_page.trade_arrival_timeout.setValue(45)
        window.settings_page.save_values()
        assert window.trade_page.arrival_timeout_minutes.value() == 45
        assert window.trade_page.collect_inputs()["arrival_timeout_seconds"] == 2700

        merged = window.workflow_page.merge_trade_inputs(
            {"arrival_timeout_seconds": 2700, "auto_cape_island_investment": False}
        )
        assert merged["arrival_timeout_seconds"] == 2700
        assert merged["auto_cape_island_investment"] is True

        window.workflow_page.passenger_city_a.setCurrentIndex(
            window.workflow_page.passenger_city_a.findData("2")
        )
        window.workflow_page.passenger_city_b.setCurrentIndex(
            window.workflow_page.passenger_city_b.findData("3")
        )
        window.workflow_page.passenger_trips.setValue(2)
        passenger = window.workflow_page.merge_passenger_inputs({})
        assert passenger["passenger_city_a_id"] == "2"
        assert passenger["passenger_city_b_id"] == "3"
        assert "预计 62 疲劳" in window.workflow_page.passenger_route_summary.text()
    finally:
        window.close()


def test_workflow_trade_status_uses_city_tree_and_dual_progress(tmp_path):
    window = _window(tmp_path)
    try:
        page = window.workflow_page
        page.begin_workflow(
            ["commerce"],
            ["trade"],
            {"auto_cape_island_investment": True},
        )
        assert page.task_progress_bar.maximum() == 1
        assert page.task_progress_bar.value() == 0
        assert page.internal_progress_bar.maximum() == 0

        route = [
            {
                "from_city": "澄明数据中心",
                "to_city": "海角城",
                "to_city_id": "11",
                "buy_products": ["货物"],
            }
        ]
        page.apply_progress_event(
            "trade",
            {
                "name": TRADE_PROGRESS_EVENT,
                "payload": {
                    "schema": TRADE_PROGRESS_SCHEMA,
                    "cid": "cid-city-tree",
                    "sequence": 1,
                    "stage": "planning",
                    "state": "completed",
                    "city_count": 2,
                    "data": {"route": route},
                },
            },
        )
        trade = page._tree_items["trade"]
        assert trade.child(0).text(0) == "准备与路线规划"
        assert trade.child(1).text(0).startswith("城市 1/2 · 澄明数据中心")
        assert trade.child(2).text(0).startswith("城市 2/2 · 海角城")
        assert page.internal_progress_bar.maximum() == 100

        page.apply_progress_event(
            "trade",
            {
                "name": TRADE_PROGRESS_EVENT,
                "payload": {
                    "schema": TRADE_PROGRESS_SCHEMA,
                    "cid": "cid-city-tree",
                    "sequence": 2,
                    "stage": "arrival",
                    "state": "completed",
                    "leg_index": 0,
                    "city_index": 1,
                    "city_count": 2,
                    "current_city": "海角城",
                },
            },
        )
        assert trade.child(2).isExpanded()
        assert "海角城" in page.internal_progress_label.text()
        assert any("城市投资" in trade.child(2).child(i).text(0) for i in range(trade.child(2).childCount()))

        page.apply_progress_event(
            "trade",
            {
                "name": TRADE_PROGRESS_EVENT,
                "payload": {
                    "schema": TRADE_PROGRESS_SCHEMA,
                    "cid": "cid-city-tree",
                    "sequence": 3,
                    "stage": "investment",
                    "state": "started",
                    "leg_index": 0,
                    "city_index": 1,
                    "city_count": 2,
                    "current_city": "海角城",
                },
            },
        )
        assert page.run_tree.isHeaderHidden()
        assert not page.log_view.isVisible()
        assert trade.child(2).text(1) == "进行中"
        assert trade.child(2).background(0).color().name() == "#f3e4b8"
        assert page.progress_stack.currentWidget() is page.timeline_view
        assert len(page.timeline_view.rows) == 2
        assert "investment" not in page.timeline_view.rows[0].phase_keys
        assert "investment" in page.timeline_view.rows[1].phase_keys
        page.finish_workflow(success=False, message="测试结束")
        assert page.log_view.isVisible()

        page.begin_workflow(
            ["commerce"],
            ["trade"],
            {"auto_cape_island_investment": False},
        )
        page.apply_progress_event(
            "trade",
            {
                "name": TRADE_PROGRESS_EVENT,
                "payload": {
                    "schema": TRADE_PROGRESS_SCHEMA,
                    "cid": "cid-city-tree-no-investment",
                    "sequence": 1,
                    "stage": "planning",
                    "state": "completed",
                    "city_count": 2,
                    "data": {"route": route},
                },
            },
        )
        assert all(
            "investment" not in row.phase_keys
            for row in page.timeline_view.rows
        )
    finally:
        window.close()


def test_workflow_failure_remains_failed_after_close_cleanup(tmp_path):
    window = _window(tmp_path)
    trade_requests: list[dict] = []
    pc_tasks: list[str] = []
    try:
        window.requestRunPcTrade.disconnect()
        window.requestRunPcTask.disconnect()
        window.requestRunPcTrade.connect(
            lambda inputs, _timeout: trade_requests.append(dict(inputs))
        )
        window.requestRunPcTask.connect(
            lambda task_ref, _inputs, _label, _timeout: pc_tasks.append(str(task_ref))
        )
        window.workflow_page._task_checks["startup"].setChecked(False)
        window.workflow_page._task_checks["battle"].setChecked(False)
        window.workflow_page._commerce_checks["passenger"].setChecked(False)
        window.settings_page.close_on_failure.setChecked(True)

        window.workflow_page.run_button.click()
        assert len(trade_requests) == 1
        trade_item = {"game_name": "resonance_pc", "kind": "trade_run", "label": "货运"}
        window._on_busy_changed(True)
        window._on_task_started(trade_item)
        window._on_task_finished(
            {
                "status": "success",
                "gui_item": trade_item,
                "final_result": {
                    "user_data": {
                        "success": False,
                        "status": "blocked",
                        "reason": "arrival_timeout",
                    }
                },
            }
        )
        window._on_busy_changed(False)
        assert pc_tasks == ["tasks:game_startup_pc.yaml:close_game"]

        close_item = {
            "game_name": "resonance_pc",
            "kind": "workflow_task",
            "label": "关闭游戏",
        }
        window._on_busy_changed(True)
        window._on_task_started(close_item)
        window._on_task_finished(
            {
                "status": "success",
                "gui_item": close_item,
                "final_result": {"user_data": {"success": True, "status": "stopped"}},
            }
        )
        window._on_busy_changed(False)

        assert not window.workflow_page.is_running()
        assert "arrival_timeout" in window.workflow_page.progress_label.text()
        assert "全部启用任务已完成" not in window.workflow_page.progress_label.text()
    finally:
        window.close()


def test_target_refresh_and_connection_status_are_global(tmp_path):
    window = _window(tmp_path)
    refresh_requests: list[bool] = []
    try:
        window.requestRefreshTarget.disconnect()
        window.requestRefreshTarget.connect(lambda: refresh_requests.append(True))
        window.refresh_target_button.click()
        assert refresh_requests == [True]
        assert window.global_target_label.text() == "● 等待连接"

        window._set_global_target_status(
            {"ok": True, "target": {"title": "雷索纳斯", "visible": True}}
        )
        assert "雷索纳斯" in window.global_target_label.text()
        assert not hasattr(window.workflow_page, "connection_label")
        for page in (window.trade_page, window.passenger_page, window.battle_page):
            assert "刷新目标" not in [button.text() for button in page.findChildren(QPushButton)]
    finally:
        window.close()


def test_detail_pages_return_without_collecting_inputs(tmp_path, monkeypatch):
    window = _window(tmp_path)
    try:
        monkeypatch.setattr(
            window.trade_page,
            "collect_inputs",
            lambda: (_ for _ in ()).throw(AssertionError("return collected trade inputs")),
        )
        monkeypatch.setattr(
            window.passenger_page,
            "collect_inputs",
            lambda: (_ for _ in ()).throw(AssertionError("return collected passenger inputs")),
        )
        for page_index in (window.COMMERCE_PAGE_INDEX, window.BATTLE_PAGE_INDEX):
            window._switch_page(page_index)
            window.back_to_workflow_button.click()
            assert window.page_stack.currentWidget() is window.workflow_page
            assert window.isVisible()
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
        assert window.page_stack.currentWidget() is window.workflow_page
        assert window.workflow_page.center_stack.currentWidget() is window.workflow_page.trade_editor_page
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

        assert window.page_stack.currentWidget() is window.workflow_page
        assert (
            window.workflow_page.center_stack.currentWidget()
            is window.workflow_page.passenger_editor_page
        )
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
                        "requested_trips": 1,
                        "completed_legs": [{}, {}],
                    }
                },
            }
        )
        assert window.passenger_page.run_status_value.text() == "已完成"
        assert not window.passenger_page.is_busy()
    finally:
        window.close()
