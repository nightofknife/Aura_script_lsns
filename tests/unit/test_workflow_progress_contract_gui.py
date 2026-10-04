"""City visits and resource snapshots in the bottom execution dock."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA
from packages.resonance_gui.widgets.workflow_page import WorkflowPage, _CityTimelineRow


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    widget = WorkflowPage(ResonanceConfigRepository(QSettings(str(tmp_path / "progress.ini"), QSettings.IniFormat)))
    widget.begin_workflow(["trade", "passenger"], [])
    widget.set_active_progress_cid("trade", "current-run")
    yield widget
    widget.close()
    app.processEvents()


def event(sequence, stage, state, **fields):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "current-run",
        "sequence": sequence, "stage": stage, "state": state, **fields,
    }}


def plan_event():
    return event(1, "planning", "completed", data={
        "route": [{"from_city": "修格里城", "to_city": "海角城", "buy_products": ["铁矿石"]}],
        "city_visits": [
            {"city_index": 0, "city_id": "1", "city_name": "修格里城", "role": "initial",
             "phases": [{"key": key, "status": "waiting"} for key in ("sell", "books", "buy", "travel")]},
            {"city_index": 1, "city_id": "11", "city_name": "海角城", "role": "terminal",
             "phases": [{"key": key, "status": "waiting"} for key in ("arrival", "final_sale", "bento")]},
        ], "summary": {"trade_mode": "fixed", "status": "planned", "planning_status": "ok"},
        "reposition": {"required": True, "expected_fatigue": 20},
        "progress": {"completed_units": 1, "total_units": 10},
        "resources": {"confirmed_books_used": 0, "confirmed_negotiation_fatigue": 0},
    })


def test_preparation_is_inside_freight_not_a_sixth_task(page):
    page.add_freight_preparation()
    page.mark_step("trade", "running", "准备货运")
    page.mark_step("refresh_recovery", "success", "准备完成")
    assert page.run_tree.topLevelItemCount() == 2
    assert page._tree_items["refresh_recovery"].parent() is page._tree_items["trade"]
    assert page.task_progress_bar.value() == 0
    assert page._tree_items["trade"].text(1) == "执行中"


def test_timeline_shows_book_phase_and_preserves_backend_order(page):
    page.apply_progress_event("trade", plan_event())
    city = page._freight_progress.cities[0]
    assert [phase.key for phase, _ in _CityTimelineRow._phase_positions(city.phases)] == ["sell", "books", "buy", "travel"]
    assert page.internal_progress_bar.value() == 10


def test_reposition_remains_separate_from_trade_visits(page):
    page.apply_progress_event("trade", plan_event())
    page.apply_progress_event("trade", event(2, "reposition", "started", data={"reposition": {"required": True}, "reason": "前往线路起点"}))
    assert page.progress_stack.currentWidget() is page.run_tree
    assert len(page._freight_progress.cities) == 2
    assert page._freight_progress.active_city_index is None


def test_resource_totals_are_overwritten_and_hidden_for_next_task(page):
    page.apply_progress_event("trade", plan_event())
    for sequence in (2, 3):
        page.apply_progress_event("trade", event(sequence, "books", "completed", city_index=0,
            data={"resources": {"confirmed_books_used": 2, "confirmed_negotiation_fatigue": 3}}))
    assert "2 本" in page.resource_progress_label.text()
    assert "不含行车" in page.resource_progress_label.text()
    assert page._freight_progress.resources["confirmed_books_used"] == 2
    page.mark_step("passenger", "running", "客运")
    assert page.resource_progress_label.isHidden()


def test_indeterminate_progress_stops_animating_after_failure(page):
    page.mark_step("trade", "running", "规划中")
    assert page.internal_progress_bar.maximum() == 0
    page.mark_step("trade", "failed", "规划失败")
    assert page.internal_progress_bar.maximum() == 100
    assert page.internal_progress_label.text() == "规划失败"
