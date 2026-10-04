"""Independent tasks share a wide editor and bottom execution dock."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPointF, QSettings, Qt
from PySide6.QtGui import QDropEvent, QFont
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QStyleFactory, QWidget

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.app import _light_application_palette
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.toggle_button import ToggleButton
from packages.resonance_gui.widgets.workflow_page import WORKFLOW_TASKS, WorkflowPage
from packages.resonance_gui.widgets.small_tasks_page import SMALL_TASKS


@pytest.fixture
def repository(tmp_path):
    app = QApplication.instance() or QApplication([])
    return ResonanceConfigRepository(QSettings(str(tmp_path / "workspace.ini"), QSettings.IniFormat))


@pytest.fixture
def window(repository, monkeypatch):
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    # Restyling an existing QApplication also touches windows from other tests.
    # Own this style with the fixture window and configure only its descendants.
    style = QStyleFactory.create("Fusion")
    style.setParent(widget)
    widget.setStyle(style)
    for child in widget.findChildren(QWidget):
        child.setStyle(style)
    widget.setPalette(_light_application_palette())
    widget.setFont(QFont("Microsoft YaHei", 10))
    yield widget
    widget._workflow_active = False
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def test_legacy_commerce_expands_in_place_and_keeps_enabled_subset(repository):
    repository.set_value("workflow/task_order", "close,commerce,startup,battle")
    repository.set_value("workflow/commerce_order", "passenger,trade")
    repository.set_value("workflow/enabled", "startup,commerce")
    repository.set_value("workflow/commerce_enabled", "passenger")
    page = WorkflowPage(repository)
    assert page._task_order == ["close", "passenger", "trade", "startup", "battle"]
    assert page.workflow_steps() == ["passenger", "startup"]
    assert list(page._task_checks) == [task for task, _ in WORKFLOW_TASKS]
    assert "commerce" not in page._task_checks
    page._save_state()
    restored = WorkflowPage(repository)
    assert restored._task_order == page._task_order
    assert restored.workflow_steps() == page.workflow_steps()
    page.close()
    restored.close()
    page.deleteLater()
    restored.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_disabled_legacy_commerce_does_not_enable_business_tasks(repository):
    repository.set_value("workflow/task_order", "startup,commerce,battle,close")
    repository.set_value("workflow/enabled", "startup,close")
    repository.set_value("workflow/commerce_enabled", "trade,passenger")
    page = WorkflowPage(repository)
    assert page.workflow_steps() == ["startup", "close"]
    page.close()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_drop_event_reorders_tasks_and_persists_numbers(window):
    page = window.workflow_page
    window.show()
    QApplication.processEvents()
    mime = QMimeData()
    mime.setData("application/x-aura-workflow-task", b"close")
    event = QDropEvent(QPointF(1, 0), Qt.DropAction.MoveAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    page.task_rows_host.dropEvent(event)
    assert event.isAccepted()
    assert page._task_order == ["close", "startup", "trade", "passenger", "battle"]
    assert [page._task_rows[key].number_label.text() for key in page._task_order] == ["1", "2", "3", "4", "5"]
    assert window._settings.value("workflow/task_order").split(",") == page._task_order
    assert page._selected_task == "close"


def test_five_tasks_have_flat_runtime_tree_and_busy_lock(window):
    page = window.workflow_page
    steps = [key for key, _ in WORKFLOW_TASKS]
    page.begin_workflow(steps, [])
    assert page.run_tree.topLevelItemCount() == 5
    assert [page.run_tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) for index in range(5)] == steps
    assert all(page.run_tree.topLevelItem(index).childCount() == 0 for index in range(5))
    assert page.task_progress_bar.maximum() == 5
    page.add_freight_preparation()
    assert page._tree_items["refresh_recovery"].parent() is page._tree_items["trade"]
    page.mark_step("refresh_recovery", "success", "prepared")
    assert page.task_progress_bar.value() == 0
    assert page.task_progress_bar.maximum() == 5
    page.mark_step("startup", "success", "ready")
    assert page.task_progress_bar.value() == 1
    assert "1 / 5" in page.task_progress_label.text()
    assert not page.center_panel.isEnabled()
    assert not page.task_rows_host.isEnabled()
    assert all(not button.isEnabled() for button in page._task_checks.values())
    assert not window.trade_page.fatigue_budget.isEnabled()
    assert not window.passenger_page.trip_count.isEnabled()
    assert page.run_button.isEnabled()
    order = list(page._task_order)
    page._drop_task("close", 0)
    assert page._task_order == order
    page.finish_workflow(success=True, message="done")
    assert page.center_panel.isEnabled()
    assert page.task_rows_host.isEnabled()
    assert window.trade_page.fatigue_budget.isEnabled()


@pytest.mark.parametrize("size", [(1440, 860), (1180, 720)])
@pytest.mark.parametrize("task", ["trade", "passenger", "battle"])
def test_workspace_is_wide_with_bottom_dock(window, tmp_path, size, task):
    window.resize(*size)
    page = window.workflow_page
    page._select_task(task)
    window.show()
    QApplication.processEvents()
    assert page.workspace_splitter.orientation() == Qt.Orientation.Vertical
    assert page.workspace_splitter.widget(0) is page.center_panel
    assert page.workspace_splitter.widget(1) is page.right_panel
    assert page.center_panel.width() > page.left_panel.width() * 3
    assert page.center_panel.geometry().bottom() < page.right_panel.geometry().top()
    assert page.center_panel.width() == page.right_panel.width()
    assert page.run_button.isVisible()
    if task in {"trade", "passenger"}:
        editor = window.trade_page if task == "trade" else window.passenger_page
        assert editor.parameter_panel.width() > 500
        assert page.center_panel.isAncestorOf(editor.parameter_panel)
    else:
        assert page.center_stack.currentWidget() is window.battle_page
    assert window.grab().save(str(tmp_path / f"workspace-{task}-{size[0]}.png"))


def test_business_tasks_stay_separate_without_budget_combination_or_auto_book(window, monkeypatch):
    page = window.workflow_page
    for key, check in page._task_checks.items():
        check.setChecked(key in {"trade", "passenger"})
    page._drop_task("passenger", 1)
    trade = window.trade_page
    trade.fatigue_budget.setValue(333)
    trade.cargo_capacity.setValue(950)
    window.passenger_page.trip_count.setValue(3)
    window.passenger_page.trade_during_trip.setChecked(True)
    monkeypatch.setattr(window, "_dispatch_next_workflow_task", lambda: None)
    window._start_workflow()
    pending = window._workflow_pending
    assert [row["step"] for row in pending] == ["passenger", "trade"]
    assert [row["dispatch"] for row in pending] == ["passenger", "trade"]
    assert all("parent" not in row and "budget_pending" not in row for row in pending)
    assert "fatigue_budget" not in pending[0]["inputs"]
    assert pending[0]["inputs"]["trade_during_trip"] is True
    assert pending[1]["inputs"]["fatigue_budget"] == 333
    assert pending[1]["inputs"]["cargo_capacity"] == 950
    assert all("auto_book" not in row["inputs"] for row in pending)
    assert not hasattr(trade, "auto_book")
    assert not any("自动进货书" in button.text() for button in window.findChildren(ToggleButton))
    assert page.run_tree.topLevelItemCount() == 2
    trade.fatigue_budget.setValue(999)
    window.passenger_page.trip_count.setValue(6)
    assert pending[0]["inputs"]["trip_count"] == 3
    assert pending[1]["inputs"]["fatigue_budget"] == 333


def assert_no_sibling_control_overlap(window):
    types = (QPushButton, QComboBox, QSpinBox, QLineEdit, QLabel)
    for parent in window.findChildren(QWidget):
        children = [child for child in parent.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
                    if isinstance(child, types) and child.isVisibleTo(window) and child.width() > 0 and child.height() > 0]
        for index, first in enumerate(children):
            for second in children[index + 1:]:
                assert not first.geometry().intersects(second.geometry()), (
                    f"overlap: {parent.objectName()} {first.objectName()} {getattr(first, 'text', lambda: '')()} / "
                    f"{second.objectName()} {getattr(second, 'text', lambda: '')()}"
                )


@pytest.mark.parametrize("size", [(1180, 720), (1440, 860)])
@pytest.mark.parametrize("surface", ["startup", "trade-profit", "trade-quick", "trade-fixed", "trade-target",
                                      "passenger", "battle", "close", "settings",
                                      *[f"feature-{key}" for key, _ in SMALL_TASKS]])
def test_full_window_visual_qa_all_surfaces(window, size, surface):
    window.resize(*size)
    page = window.workflow_page
    if surface == "settings":
        window._switch_page(window.SETTINGS_PAGE_INDEX)
    elif surface.startswith("feature-"):
        window.small_tasks_page.show_task(surface.removeprefix("feature-"))
        window._switch_page(window.SMALL_TASKS_PAGE_INDEX)
    else:
        task = "trade" if surface.startswith("trade-") else surface
        page._select_task(task)
        if task == "trade":
            window.trade_page.mode_buttons[surface.removeprefix("trade-")].click()
    window.show()
    QApplication.processEvents()
    assert (window.width(), window.height()) == size
    folder = Path(".pytest_tmp/gui-workspace/visual-qa")
    folder.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(folder / f"{surface}-{size[0]}x{size[1]}.png"))
    assert_no_sibling_control_overlap(window)
    if not surface.startswith("feature-") and surface != "settings":
        assert page.right_panel.height() >= 170
        assert page.center_panel.height() > 0
        assert page.run_button.isVisible()
        assert all(row.rect().contains(row.enabled_check.geometry()) for row in page._task_rows.values())
        if surface == "passenger":
            scroll = window.passenger_page.parameter_panel
            for control in (window.passenger_page.trip_count, window.passenger_page.trade_during_trip,
                            window.passenger_page.auto_reposition,
                            *window.passenger_page.endpoint_selector_buttons.values()):
                assert scroll.viewport().rect().contains(control.mapTo(scroll.viewport(), control.rect().topLeft()))
                assert scroll.viewport().rect().contains(control.mapTo(scroll.viewport(), control.rect().bottomRight()))


def test_workflow_startup_and_close_parameters_sync_with_settings(window):
    workflow, settings = window.workflow_page, window.settings_page
    for task_field, setting_field in ((workflow.startup_window_timeout, settings.window_timeout),
                                      (workflow.startup_rounds, settings.settle_rounds),
                                      (workflow.close_timeout, settings.close_timeout)):
        task_field.setValue(15)
        assert setting_field.value() == 15
        setting_field.setValue(23)
        assert task_field.value() == 23
    workflow.startup_launch.setChecked(False)
    assert not settings.launch_if_needed.isChecked()
    settings.launch_if_needed.setChecked(True)
    assert workflow.startup_launch.isChecked()
    workflow.close_force.setChecked(False)
    assert settings.close_mode.currentData() is False
    settings.close_mode.setCurrentIndex(0)
    assert workflow.close_force.isChecked()


@pytest.mark.parametrize("mode", ["profit", "quick", "fixed", "target"])
def test_freight_trial_link_navigates_without_changing_parameters_or_calculating(window, mode):
    trade = window.trade_page
    trade.mode_buttons[mode].click()
    trade.fatigue_budget.setValue(333)
    trade.cargo_capacity.setValue(950)
    trade.start_city.setCurrentIndex(trade.start_city.findData("11"))
    trade.target_profit.setValue(12.3456)
    if mode == "fixed":
        trade.add_route_city("11")
        trade.add_route_city("15")
    window.trade_preview_page.fatigue_budget.setValue(444)
    window.trade_preview_page.cargo_capacity.setValue(850)
    window.trade_preview_page.start_city.setCurrentIndex(window.trade_preview_page.start_city.findData("1"))
    previous_preview = window.trade_preview_page.collect_ui_state()
    calls, runs = [], []
    window.requestPreviewPcTrade.connect(lambda inputs, _timeout: calls.append(inputs))
    for signal in (window.requestRunPcTask, window.requestRunPcTrade, window.requestRunPcPassenger):
        signal.connect(lambda *_args: runs.append(True))
    window.trade_preview_page.tabs.setCurrentIndex(1)
    window.freight_trial_button.click()
    assert calls == []
    assert window.trade_preview_page.collect_ui_state() == previous_preview
    assert window.trade_page.trade_mode() == mode
    assert window.trade_page.fatigue_budget.value() == 333
    assert window.trade_page.cargo_capacity.value() == 950
    assert window.trade_preview_page.tabs.currentIndex() == 0
    assert window.small_tasks_page.current_task_id == "trade_preview"
    assert window.page_stack.currentIndex() == window.SMALL_TASKS_PAGE_INDEX
    assert not runs
    assert not window._workflow_active
    assert window._workflow_pending == []


@pytest.mark.parametrize("mode", ["fixed", "target"])
def test_freight_trial_link_accepts_incomplete_parameters_without_validation(window, mode):
    window.trade_page.mode_buttons[mode].click()
    window.trade_page.fixed_route.clear()
    window.trade_page.target_profit.setValue(0)
    requests = []
    window.requestPreviewPcTrade.connect(lambda *_args: requests.append(True))
    window.freight_trial_button.click()
    assert requests == []
    assert window.small_tasks_page.current_task_id == "trade_preview"
    assert window.page_stack.currentIndex() == window.SMALL_TASKS_PAGE_INDEX


@pytest.mark.parametrize("size", [(1180, 720), (1440, 860)])
def test_populated_freight_result_keeps_readable_rows_and_can_scroll(window, size):
    page = window.trade_preview_page
    route = [{"from_city": "修格里城", "to_city": "岚心城", "from_city_id": "1", "to_city_id": "11"}]
    page._render_route(route)
    page._render_result({"status": "planned", "trade_mode": "profit", "planning_status": "ok",
                         "expected_profit": 3347181, "expected_fatigue_used": 700,
                         "books_used": 7, "route": route,
                         "warnings": ["该结果为预计收益，实际收益需要在交易后确认。"]})
    page.tabs.setCurrentIndex(1)
    window.small_tasks_page.show_trade_preview()
    window._switch_page(window.SMALL_TASKS_PAGE_INDEX)
    window.resize(*size)
    window.show()
    QApplication.processEvents()
    assert_no_sibling_control_overlap(window)
    scroll = page.execution_scroll
    for label in [*page.result_captions.values(), *page.result_values.values()]:
        if label.isHidden():
            continue
        assert label.height() >= label.fontMetrics().height()
        scroll.ensureWidgetVisible(label, 0, 0)
        QApplication.processEvents()
        assert scroll.viewport().rect().contains(label.mapTo(scroll.viewport(), label.rect().topLeft()))
        assert scroll.viewport().rect().contains(label.mapTo(scroll.viewport(), label.rect().bottomRight()))
    folder = Path(".pytest_tmp/gui-layout-fix/visual")
    folder.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(folder / f"result-{size[0]}x{size[1]}.png"))


def test_freight_additional_and_bento_buttons_share_compact_rows(window):
    window.workflow_page._select_task("trade")
    window.resize(1180, 720)
    window.show()
    QApplication.processEvents()
    page = window.trade_page
    buttons = [page.auto_pickup, page.use_fatigue_medicine,
               page.auto_cape_island_investment, page.auto_rubbish_recycling]
    assert len({button.y() for button in buttons}) == 1
    assert all(button.width() < 250 for button in buttons)
    assert len({row.y() for row in page._bento_rows.values()}) == 1
    page._move_bento_type("love_bentos", -1)
    QApplication.processEvents()
    assert page._bento_rows_layout.itemAt(0).widget() is page._bento_rows["love_bentos"]
    assert page._bento_rows_layout.itemAt(1).widget() is page._bento_rows["work_meals"]
    assert_no_sibling_control_overlap(window)
    scroll = page.parameter_panel.findChild(QScrollArea)
    scroll.ensureWidgetVisible(page.additional_options)
    QApplication.processEvents()
    folder = Path(".pytest_tmp/gui-layout-fix/visual")
    folder.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(folder / "compact-options-1180x720.png"))


@pytest.mark.parametrize("size,columns", [((1180, 720), 2), ((1440, 860), 3), ((1920, 1080), 3)])
@pytest.mark.parametrize("mode", ["profit", "quick", "fixed", "target"])
def test_freight_parameters_reflow_without_changing_values(window, size, columns, mode):
    window.workflow_page._select_task("trade")
    page = window.trade_page
    page.mode_buttons[mode].click()
    page.book_usage.setCurrentIndex(page.book_usage.findData("finite"))
    page.book_budget.setValue(31)
    page.fatigue_budget.setValue(321)
    page.cargo_capacity.setValue(999)
    page.target_profit.setValue(90)
    page.start_city.setCurrentIndex(page.start_city.findData("11"))
    page.add_route_city("11")
    page.add_route_city("15")
    expected = page.collect_task_inputs(preview=True)
    window.resize(*size)
    window.show()
    QApplication.processEvents()
    grid = page.common_parameters
    assert grid.column_count == columns
    editors = [page.fatigue_budget, page.cargo_capacity, page.book_usage, page.book_budget,
               page.book_profit_threshold, page.book_policy, page.negotiation_policy,
               page.start_city, page.target_profit]
    visible = [editor for editor in editors if editor.isVisible()]
    assert all(editor.width() <= 220 for editor in visible)
    assert len({editor.parentWidget().y() for editor in visible}) == (len(visible) + columns - 1) // columns
    assert grid.height() <= (210 if columns == 2 else 150)
    assert page.collect_task_inputs(preview=True) == expected
    assert_no_sibling_control_overlap(window)
    page.book_usage.setCurrentIndex(page.book_usage.findData("none"))
    QApplication.processEvents()
    assert page.book_budget.parentWidget().isHidden()
    assert page.target_profit.parentWidget().isHidden() is (mode != "target")
    folder = Path(".pytest_tmp/gui-compact-fields/visual")
    folder.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(folder / f"{mode}-{size[0]}x{size[1]}.png"))
