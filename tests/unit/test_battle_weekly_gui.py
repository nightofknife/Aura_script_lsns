"""Whole-week editing uses isolated preferences and never starts a game runner."""
from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QBoxLayout

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets import battle_page


@pytest.fixture
def page(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(battle_page, "battle_game_day", lambda: ("2026-10-29", "thu"))
    settings = ResonanceConfigRepository(QSettings(str(tmp_path / "battle.ini"), QSettings.IniFormat))
    widget = battle_page.BattlePage(settings)
    yield widget
    widget.close()
    app.processEvents()


def day(page, key):
    return page.collect_inputs()["weekly_plan"]["days"][key]


def test_default_today_and_empty_week_are_valid(page):
    assert page._weekday == "thu"
    assert page.weekday_buttons["thu"].isChecked()
    assert "今日" in page.weekday_buttons["thu"].text()
    assert set(page.collect_inputs()["weekly_plan"]["days"]) == set(battle_page.WEEKDAYS)
    assert all(not day(page, key)["jobs"] for key in battle_page.WEEKDAYS)
    assert "体力不足" in page.stop_rule_label.text()
    assert not hasattr(page, "stop_on_failure")


def test_editing_days_keeps_independent_jobs_and_persists_whole_week(page):
    page.weekday_buttons["mon"].click()
    page.run_count_spin.setValue(3)
    page.add_button.click()
    page.weekday_buttons["sun"].click()
    page.run_count_spin.setValue(8)
    page.add_button.click()
    page.weekday_buttons["mon"].click()
    assert page._jobs[0]["run_count"] == 3
    assert day(page, "sun")["jobs"][0]["run_count"] == 8
    assert day(page, "thu")["jobs"] == []
    assert page._settings.load_battle_inputs() == page.collect_inputs()


def test_table_counts_edit_copy_and_reorder_do_not_cross_days(page):
    page._add_job()
    page.job_table.cellWidget(0, 4).setValue(5)
    page._duplicate_selected()
    page.job_table.cellWidget(1, 4).setValue(2)
    page._move_selected(-1)
    assert [job["run_count"] for job in page._jobs] == [2, 5]
    page._delete_selected()
    assert [job["run_count"] for job in page._jobs] == [5]
    assert page._settings.load_battle_inputs() == page.collect_inputs()


def test_structural_types_are_separate_ordered_and_have_no_run_count(page):
    page.structural_buttons["echo_buoy"].click()
    page.structural_buttons["hetero_branches"].click()
    page.structural_list.setCurrentRow(1)
    page.structural_up_button.click()
    assert day(page, "thu") == {"jobs": [], "structural_types": ["hetero_branches", "echo_buoy"]}
    page.weekday_buttons["fri"].click()
    assert day(page, "fri")["structural_types"] == []
    page.weekday_buttons["thu"].click()
    assert page.structural_list.item(0).text().endswith("异构厄枝")
    page.structural_buttons["hetero_branches"].click()
    assert day(page, "thu")["structural_types"] == ["echo_buoy"]


def test_ordinary_selector_filters_weekday_openings_and_excludes_structural(page):
    page.category_buttons["gp"].click()
    assert page.subcategory_combo.count() == 1
    page.weekday_buttons["mon"].click()
    routes = [page.route_combo.itemData(index) for index in range(page.route_combo.count())]
    assert "gp.action_summary.global_supply.standard" in routes
    assert "gp.action_summary.global_supply.savior" not in routes
    assert not any("structural_exploration" in route for route in routes)
    page.weekday_buttons["sun"].click()
    routes = [page.route_combo.itemData(index) for index in range(page.route_combo.count())]
    assert "gp.action_summary.global_supply.savior" in routes


def test_saved_weekly_closed_day_choice_remains_visible_for_correction(page):
    job = {"route_id": "gp.action_summary.global_supply.savior", "run_count": 4, "difficulty": 6}
    page.set_inputs({"weekly_plan": {"mon": {"jobs": [job]}}})
    page._select_day("mon")
    assert page._jobs == [job]
    assert page.job_table.item(0, 1).text() == "全境特供\n救世"
    assert "行动汇总" in page.job_table.item(0, 1).toolTip()
    assert job["route_id"] in page.job_table.item(0, 1).toolTip()


def test_busy_locks_edits_and_week_switch_but_preserves_snapshot(page):
    page._add_job()
    snapshot = deepcopy(page.collect_inputs())
    page.set_busy(True)
    page.weekday_buttons["mon"].click()
    page._add_job()
    page._delete_selected()
    page._move_selected(1)
    page._set_run_count(0, 7)
    page._toggle_structural("echo_buoy", True)
    assert page.collect_inputs() == snapshot
    assert not page.job_table.cellWidget(0, 4).isEnabled()
    assert not page.structural_buttons["echo_buoy"].isEnabled()
    page.set_busy(False)
    assert page.job_table.cellWidget(0, 4).isEnabled()


def test_start_emits_whole_week_even_while_editing_other_day(page):
    page._select_day("mon")
    page._add_job()
    page.set_target_status({"ok": True, "target": {"visible": True, "title": "fake"}})
    requests = []
    page.startRequested.connect(lambda inputs, timeout: requests.append((inputs, timeout)))
    page.start_button.click()
    assert len(requests) == 1 and requests[0][1] == 0.
    assert requests[0][0]["weekly_plan"]["days"]["mon"]["jobs"]
    assert requests[0][0]["weekly_plan"]["days"]["thu"]["jobs"] == []
    page.set_busy(True)  # Workflow may lock all editors before the start signal.
    page.begin_run({"cid": "fake-battle"})
    assert page._weekday == "thu"


def test_result_cannot_mark_another_weekdays_rows_as_completed(page):
    page._select_day("mon")
    page._add_job()
    payload = {"schema": "resonance_pc.battle_plan_result.v1", "weekday": "thu",
               "effective_date": "2026-10-29", "status": "completed", "stop_reason": None,
               "job_results": [{"status": "completed"}], "counts": {"swept": 1, "won": 0, "lost": 0}}
    page.finish_run(payload)
    assert page._job_statuses == ["等待"]
    assert page.job_table.item(0, 5).text() == "等待"
    assert "周四" in page.stage_detail.text()


def test_plan_changed_signal_contains_saved_week_not_edited_day_only(page):
    emitted = []
    page.planChanged.connect(emitted.append)
    page.structural_buttons["birch_buoy"].click()
    assert emitted[-1] == page._settings.load_battle_inputs()
    assert set(emitted[-1]["weekly_plan"]["days"]) == set(battle_page.WEEKDAYS)


@pytest.mark.parametrize("window_width,editor_width,horizontal", [(1220, 568, False), (1440, 788, True)])
def test_real_workflow_embed_keeps_form_and_stage_names_usable(
        tmp_path, monkeypatch, window_width, editor_width, horizontal):
    from PySide6.QtCore import QCoreApplication, QEvent
    from packages.resonance_gui.main_window import ResonanceMainWindow
    from packages.resonance_gui.style import APP_STYLE

    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    previous_font = app.font()
    if font_id >= 0:
        app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(battle_page, "battle_game_day", lambda: ("2026-10-29", "thu"))
    repo = ResonanceConfigRepository(QSettings(str(tmp_path / "embedded.ini"), QSettings.IniFormat))
    window = ResonanceMainWindow(settings=repo, initialize_on_startup=False)
    try:
        window.setStyleSheet(APP_STYLE)
        page = window.battle_page
        page.set_inputs({"weekly_plan": {"thu": {"jobs": [{
            "route_id": "gp.action_summary.blade_encirclement.special_order",
            "run_count": 9999, "difficulty": 6,
        }], "structural_types": ["echo_buoy", "disordered_roots"]}}})
        window.resize(window_width, 950)
        window.show()
        window._open_battle_editor()
        for _ in range(4):
            app.processEvents()
        assert abs(page.width()-editor_width) <= 10
        assert page.job_form.column_count == 2
        direction = QBoxLayout.Direction.LeftToRight if horizontal else QBoxLayout.Direction.TopToBottom
        assert page._body_layout.direction() == direction
        if not horizontal:
            assert page.job_form.width() < 560  # Regression: global threshold made eight rows.
            assert page._ordinary_panel.width() >= page.width()-34
            assert page._structural_panel.y() > page._ordinary_panel.geometry().bottom()
        item = page.job_table.item(0, 1)
        assert item.text() == "利刃围剿\n特殊订单"
        assert page.job_table.columnWidth(1) >= page.job_table.fontMetrics().horizontalAdvance("特殊订单")+12
        assert page.job_table.cellWidget(0, 4).value() == 9999
        today = page.weekday_buttons["thu"]
        assert today.width() >= today.fontMetrics().horizontalAdvance("周四 · 今日")+6
        assert window.grab().save(str(tmp_path / f"embedded_{window_width}.png"))
    finally:
        window._workflow_active = False
        window._close_ready = True
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        app.setFont(previous_font)
