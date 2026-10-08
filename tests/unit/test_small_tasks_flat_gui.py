"""Flat small-task navigation and unchanged run signal contracts."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets.small_tasks_page import SmallTasksPage
from packages.resonance_gui.widgets.team_recommendation_panel import TeamRecommendationPanel


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication(["small-tasks-flat-test"])
    repository = ResonanceConfigRepository(
        QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    widget = SmallTasksPage(repository)
    yield widget
    widget.close()
    app.processEvents()


def test_flat_navigation_reserves_detail_workspace(page):
    page.resize(1200, 800)
    page.show()
    QApplication.processEvents()
    assert [page.task_list.item(i).text() for i in range(page.task_list.count())] == [
        "刷新用户数据", "货运试算", "配队推荐", "识海深潜", "无垠乱斗", "数据采集",
    ]
    assert page.layout().count() == 2
    assert page.category_list.isHidden()
    assert not hasattr(page, "category_panel")
    assert hasattr(page, "data_collection_panel")
    assert "data_collection" in page._task_pages
    assert page.detail_stack.count() == 6
    assert page.task_panel.minimumWidth() == 140
    assert page.task_panel.maximumWidth() == 210
    assert page.task_panel.sizeHint().width() == 170
    assert 140 <= page.task_panel.width() <= 210
    assert page.detail_panel.width() > page.task_panel.width() * 3
    assert page.task_list.item(0).sizeHint().height() == 42
    assert all(page.task_list.item(i).toolTip() for i in range(page.task_list.count()))
    assert "任务分类" not in {label.text() for label in page.findChildren(QLabel)}


def test_each_task_selects_existing_panel_and_preview_api(page):
    for index in range(page.task_list.count()):
        page.task_list.setCurrentRow(index)
        task_id = page.task_list.item(index).data(Qt.ItemDataRole.UserRole)
        assert page.current_task_id == task_id
        assert page.detail_stack.currentWidget() is page._task_pages[task_id]
    page.show_trade_preview()
    assert page.current_task_id == "trade_preview"
    assert page.detail_stack.currentWidget() is page.trade_preview_panel
    page.show_task("missing-task")
    assert page.current_task_id == "trade_preview"
    team_category = page.category_list.findItems("配队工具", Qt.MatchFlag.MatchExactly)[0]
    page.category_list.setCurrentItem(team_category)
    assert page.current_task_id == "team_recommendation"
    assert page.task_list.count() == 6


def test_run_cancel_scan_and_data_snapshot_contracts(page):
    player_requests, team_requests, scan_requests, cancellations = [], [], [], []
    page.runPlayerDataRequested.connect(lambda inputs: player_requests.append(dict(inputs)))
    page.runTeamRecommendationRequested.connect(lambda: team_requests.append(True))
    page.runConsciousnessDeepDiveScanRequested.connect(lambda: scan_requests.append(True))
    page.cancelRequested.connect(lambda: cancellations.append(True))
    page.run_button.click()
    assert player_requests == [{
        "stages": ["location", "profile", "inventory", "characters"],
        "inventory_categories": ["items"],
        "profile_sections": ["cargo", "clarity", "fatigue"],
    }]
    assert page.player_data_panel.tabs.tabText(1) == "数据快照"
    page.show_task("team_recommendation")
    page.team_recommendation_panel.run_button.click()
    assert team_requests == [True]
    page.begin_team_recommendation_run()
    page.set_runner_busy(True)
    assert not page.task_list.isEnabled()
    assert not page.team_recommendation_panel.run_button.isEnabled()
    page.team_recommendation_panel.cancel_button.click()
    assert cancellations == [True]
    page.set_runner_busy(False)
    page.show_team_recommendation_error("测试取消")
    assert page.task_list.isEnabled()
    page.show_task("consciousness_deep_dive")
    page.consciousness_deep_dive_panel.scan_button.click()
    assert scan_requests == [True]
    assert hasattr(page.consciousness_deep_dive_panel, "scan_report_button")
    assert hasattr(page.consciousness_deep_dive_panel, "plan_report_button")


def test_team_status_and_controls_stay_below_result_workspace():
    app = QApplication.instance() or QApplication(["team-status-test"])
    panel = TeamRecommendationPanel()
    layout = panel.layout()
    assert layout.itemAt(layout.count() - 2).widget() is panel.status_label
    action_band = layout.itemAt(layout.count() - 1).widget()
    assert isinstance(action_band, QFrame)
    assert panel.run_status.parentWidget() is action_band
    assert "不会自动刷新" in panel.run_button.toolTip()
    assert "不校验数量" in panel.result_tree.toolTip()
    assert "不会关闭游戏" in panel.cancel_button.toolTip()
    panel.apply_result({"status": "blocked", "message": "先更新数据"})
    assert panel.status_label.text() == "先更新数据"
    assert panel.run_button.isEnabled()
    panel.close()
    app.processEvents()


def test_capture_status_api_keeps_existing_panel(page):
    page.begin_consciousness_deep_dive_capture_run({"sensitivity": "slow"})
    page.set_runner_busy(True)
    assert not page.task_list.isEnabled()
    page.set_runner_busy(False)
    page.apply_consciousness_deep_dive_capture_result({"success": True})
    assert page.task_list.isEnabled()
    page.begin_consciousness_deep_dive_sensitivity_probe_run()
    page.show_consciousness_deep_dive_sensitivity_probe_error("测试失败")
    assert page.task_list.isEnabled()
    assert hasattr(page, "data_collection_panel")
