"""Layout restoration uses real editors and fake task dispatch, never game input."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QSettings, QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.logic import TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    widget = ResonanceMainWindow(settings=ResonanceConfigRepository(QSettings(str(tmp_path/'gui.ini'), QSettings.IniFormat)), initialize_on_startup=False)
    yield widget
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def enable_only(window, *tasks):
    for task, check in window.workflow_page._task_checks.items():
        check.setChecked(task in tasks)


def test_empty_startup_path_blocks_all_dispatch_without_registry(window, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    calls = []
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    enable_only(window, "startup", "trade")
    window._start_workflow()
    assert errors and "游戏路径为空" in errors[0]
    assert not window._workflow_active and not calls


def test_disabled_invalid_tasks_do_not_block_freight(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(args[2]))
    enable_only(window, "trade")
    calls = []
    window.requestRunPcTrade.connect(lambda *args: calls.append(args))
    window._start_workflow()
    assert len(calls) == 1
    assert calls[0][0]["fatigue_budget"] == 700
    assert calls[0][0]["cargo_capacity"] == 750
    assert not window.trade_page.fatigue_budget.isEnabled()
    window.workflow_page._select_task("passenger")
    assert "客运" in window.workflow_page.editing_label.text()
    assert "货运" in window.workflow_page.runtime_task_label.text()
    assert window.trade_page.advanced_toggle.isEnabled()


def test_stopping_waits_for_runner_and_converges_task_row(window):
    page = window.workflow_page
    page.begin_workflow(["trade"], [])
    window._workflow_active = True
    window._busy = True
    window._workflow_current = {"step": "trade", "label": "货运", "dispatch": "trade"}
    window._stop_workflow()
    assert page._tree_items["trade"].text(1) == "停止中"
    assert page.run_button.text() == "停止中…" and not page.run_button.isEnabled()
    window._on_busy_changed(False)
    assert page._tree_items["trade"].text(1) == "已停止"
    assert "已停止" in page.runtime_task_label.text()
    assert page.run_button.isEnabled()


def test_arrival_setting_has_one_value_and_game_path_label_updates(window):
    window.trade_page.arrival_timeout_minutes.setValue(90)
    assert window.settings_page.trade_arrival_timeout.value() == 90
    window.settings_page.trade_arrival_timeout.setValue(45)
    assert window.trade_page.arrival_timeout_minutes.value() == 45
    window.settings_page.gamePathSaved.emit("D:/game/雷索纳斯.exe")
    assert "D:/game/雷索纳斯.exe" in window.workflow_page.startup_path_label.text()


def test_stop_cancels_queued_dispatch_before_busy_acknowledgement(window):
    page = window.workflow_page
    page.begin_workflow(["trade"], [])
    window._workflow_active = True
    window._busy = False
    window._workflow_current = {"step": "trade", "label": "货运", "dispatch": "trade"}
    cancelled = []
    window.requestCancelCurrent.connect(lambda: cancelled.append(True))
    window._stop_workflow()
    assert cancelled == [True]
    assert window._workflow_active and window._workflow_stopping
    assert not page.run_button.isEnabled()
    window._on_busy_changed(True)
    assert window._workflow_active and not page.run_button.isEnabled()
    window._on_busy_changed(False)
    assert not window._workflow_active and page.run_button.isEnabled()


def test_progress_never_navigates_away_from_parameter_editor(window):
    page = window.workflow_page
    page._select_task("passenger")
    selected = page.center_stack.currentWidget()
    page.begin_workflow(["trade"], [])
    page.set_active_progress_cid("trade", "test")
    page.apply_progress_event("trade", {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "test", "sequence": 1,
        "stage": "planning", "state": "completed", "data": {"route": []}}})
    assert page.center_stack.currentWidget() is selected
    assert page.runtime_plan_button.isEnabled()
    assert page.workspace_splitter.orientation() == Qt.Horizontal


def test_trial_return_uses_existing_freight_editor(window):
    original = window.trade_page.parameter_panel
    window._open_freight_trial()
    window.small_tasks_page.return_to_trade_button.click()
    assert window.page_stack.currentWidget() is window.workflow_page
    assert window.workflow_page.center_panel.isAncestorOf(original)


def test_inspector_auto_folds_on_narrow_window_and_preserves_logs(window):
    window.show()
    window.resize(1366, 768)
    QApplication.processEvents()
    page = window.workflow_page
    page.append_log("保留这条日志")
    window.resize(1000, 700)
    QApplication.processEvents()
    assert page._inspector_collapsed and page.inspector_strip.isVisible()
    assert page.compact_run_button.isVisible()
    page.expand_inspector_button.click()
    QApplication.processEvents()
    assert not page._inspector_collapsed
    assert "保留这条日志" in page.log_view.toPlainText()
    window.resize(1440, 860)
    QApplication.processEvents()
    window.resize(1000, 700)
    QApplication.processEvents()
    assert page._inspector_collapsed


def test_frozen_layout_exercise_never_dispatches_game_task(window):
    from packages.resonance_gui.app import _exercise_layout_self_check
    calls = []
    window.requestRunPcTrade.connect(lambda *args: calls.append(args))
    window.requestRunPcTask.connect(lambda *args: calls.append(args))
    _exercise_layout_self_check(window, QApplication.instance())
    assert not calls and not window._workflow_active


def test_freight_operations_group_by_city_visit_even_with_leg_metadata(window):
    from PySide6.QtWidgets import QTreeWidgetItem
    page = window.workflow_page
    page._remember_operation("trade", {"stage": "buy", "city_index": 2, "leg_index": 3,
        "data": {"route_revision": 1, "operation": {"key": "confirm", "label": "购买确认", "state": "completed"}}})
    item = QTreeWidgetItem()
    page._append_operations(item, "trade", "buy", city_index=2, revision=1)
    assert item.childCount() == 1 and item.child(0).text(0) == "购买确认"


def test_explicit_parameter_save_does_not_start_task(window):
    window.workflow_page._select_task("trade")
    window.trade_page.fatigue_budget.setValue(888)
    calls = []
    window.requestRunPcTrade.connect(lambda *args: calls.append(args))
    window.workflow_page.save_parameters_button.click()
    assert window._settings.load_trade_inputs()["fatigue_budget"] == 888
    assert not calls and not window._workflow_active


def test_advanced_disclosure_keeps_its_text_visible(window):
    assert window.trade_page.advanced_toggle.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon


def test_default_inspector_width_leaves_extra_space_for_parameters(window):
    window.resize(1366, 768)
    window.show()
    QApplication.processEvents()
    page = window.workflow_page
    assert 320 <= page.right_panel.width() <= 390
    before = page.center_panel.width()
    window.resize(1920, 1080)
    QApplication.processEvents()
    assert 320 <= page.right_panel.width() <= 390
    assert page.center_panel.width() > before
