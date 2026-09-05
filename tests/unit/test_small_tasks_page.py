"""Smoke checks for the first small task and its primary navigation."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets import SmallTasksPage
from packages.resonance_gui.widgets.workflow_page import WorkflowPage


def _application() -> QApplication:
    return QApplication.instance() or QApplication(["aura-small-task-smoke"])


def test_small_tasks_page_exposes_player_data_refresh(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    page = SmallTasksPage(ResonanceConfigRepository(settings))

    labels = {label.text() for label in page.findChildren(QLabel)}
    assert {"小任务", "任务列表", "任务详情"}.issubset(labels)
    assert page.category_list.currentItem().text() == "用户数据"
    assert page.task_list.count() == 0
    assert page.task_panel.isHidden()
    assert page.current_task_id == "player_data_refresh"
    assert "独立运行" not in "".join(labels)

    requests: list[dict] = []
    page.runPlayerDataRequested.connect(lambda inputs: requests.append(dict(inputs)))
    page.run_button.click()

    assert requests == [
        {
            "stages": ["location", "profile", "inventory", "characters"],
            "inventory_categories": ["items"],
        }
    ]
    panel = page.player_data_panel
    assert set(panel._inventory_category_checks) == {"items", "materials", "equipment"}
    assert panel._inventory_category_checks["items"].isChecked()
    assert not panel._inventory_category_checks["equipment"].isChecked()


def test_player_data_equipment_config_migration_and_snapshot(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue(
        "player_data/inputs_json",
        json.dumps(
            {
                "stages": ["inventory"],
                "inventory_categories": ["materials"],
            },
            ensure_ascii=False,
        ),
    )
    settings.setValue("player_data/inputs_schema_version", 2)
    repository = ResonanceConfigRepository(settings)
    page = SmallTasksPage(repository)
    panel = page.player_data_panel

    assert repository.load_player_data_inputs() == {
        "stages": ["inventory"],
        "inventory_categories": ["materials"],
    }
    assert not panel._inventory_category_checks["equipment"].isChecked()
    panel._inventory_category_checks["materials"].setChecked(False)
    panel._inventory_category_checks["equipment"].setChecked(True)
    assert panel.collect_inputs() == {
        "stages": ["inventory"],
        "inventory_categories": ["equipment"],
    }

    panel.set_snapshot(
        {
            "inventory": {
                "schema_version": 2,
                "categories": {
                    "equipment": {
                        "category": "equipment",
                        "matched_card_count": 3,
                        "matched_equipment_count": 2,
                        "pages_scanned": 4,
                        "equipment": [
                            {"equipment_id": "a", "name": "甲", "count": 2},
                            {"equipment_id": "b", "name": "乙", "count": 1},
                        ],
                    }
                },
            },
            "metadata": {
                "updated_at": "2026-08-25T12:00:00+00:00",
                "section_updated_at": {"inventory": "2026-08-25T12:00:00+00:00"},
                "inventory_category_updated_at": {
                    "equipment": "2026-08-25T12:00:00+00:00"
                },
            },
        }
    )
    equipment_index = panel.inventory_category_combo.findData("equipment")
    assert equipment_index >= 0
    panel.inventory_category_combo.setCurrentIndex(equipment_index)

    assert panel.inventory_table.horizontalHeaderItem(0).text() == "装备"
    assert panel.inventory_table.isColumnHidden(2)
    assert panel.inventory_table.rowCount() == 2
    assert panel.inventory_table.item(0, 0).text() == "甲"
    assert panel.inventory_table.item(0, 1).text() == "2"
    assert "2 种装备" in panel.inventory_summary.text()
    assert "3 件" in panel.inventory_summary.text()
    assert "4 页" in panel.inventory_summary.text()


def test_small_tasks_page_runs_and_renders_team_recommendations(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    page = SmallTasksPage(ResonanceConfigRepository(settings))
    team_category = page.category_list.findItems("配队推荐", Qt.MatchFlag.MatchExactly)[0]
    page.category_list.setCurrentItem(team_category)

    assert page.task_list.count() == 0
    assert page.task_panel.isHidden()
    assert page.current_task_id == "team_recommendation"
    requests: list[bool] = []
    page.runTeamRecommendationRequested.connect(lambda: requests.append(True))
    page.team_recommendation_panel.run_button.click()
    assert requests == [True]

    page.begin_team_recommendation_run()
    page.set_runner_busy(True)
    assert not page.category_list.isEnabled()
    assert page.team_recommendation_panel.cancel_button.isEnabled()
    page.set_runner_busy(False)
    page.apply_team_recommendation_result(
        {
            "status": "blocked",
            "message": "请先更新武器数据。",
            "recommendations": [],
        }
    )
    assert page.team_recommendation_panel.status_label.text() == "请先更新武器数据。"

    members = [
        {
            "slot": index,
            "character_id": name,
            "current_awakening": 3,
            "minimum_awakening": 0,
            "recommended_awakening": 2,
            "full_weapon_id": f"满配{index}",
            "low_weapon_id": f"低配{index}",
            "assigned_weapon_id": f"满配{index}",
        }
        for index, name in enumerate(("甲", "乙", "丙", "丁", "戊"), 1)
    ]
    page.apply_team_recommendation_result(
        {
            "status": "success",
            "message": "找到 1 套固定配队。",
            "counts": {
                "character_complete": 1,
                "character_basic": 0,
                "weapon_full": 1,
                "weapon_low": 0,
                "weapon_unmet": 0,
            },
            "recommendations": [
                {
                    "team_id": "测试队",
                    "title": "测试队",
                    "categories": ["攻坚"],
                    "character_status": "complete",
                    "weapon_status": "full",
                    "members": members,
                }
            ],
        }
    )
    root = page.team_recommendation_panel.result_tree.topLevelItem(0)
    assert root.text(2) == "角色完全满足"
    assert root.text(3) == "满配武器都有"
    assert root.childCount() == 5


def test_main_window_opens_small_tasks_without_losing_global_controls(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = ResonanceMainWindow(
        settings=ResonanceConfigRepository(settings),
        initialize_on_startup=False,
        update_checker=lambda: "",
    )
    try:
        refresh_requests: list[bool] = []
        window.requestRefreshTarget.connect(lambda: refresh_requests.append(True))
        window.primary_nav_buttons[window.SMALL_TASKS_PAGE_INDEX].click()

        assert window.page_stack.currentWidget() is window.small_tasks_page
        assert window.refresh_target_button.text() == "刷新目标"
        assert "窗口" not in window.version_badge.text()
        assert window.version_badge.text().startswith("v")
        assert window.global_target_label.text() == "● 未连接窗口"
        assert not window.back_to_workflow_button.isVisible()
        assert window.workflow_page.workflow_steps() == [
            "startup",
            "commerce",
            "battle",
            "close",
        ]
        assert "player_data" not in window.workflow_page._task_rows
        assert not hasattr(window.workflow_page, "player_data_panel")

        window.requestRunPcTask.disconnect()
        dispatches: list[tuple[str, dict, str, float]] = []
        window.requestRunPcTask.connect(
            lambda task_ref, inputs, label, timeout: dispatches.append(
                (str(task_ref), dict(inputs), str(label), float(timeout))
            )
        )
        window.small_tasks_page.run_button.click()
        assert dispatches == [
            (
                "tasks:player_data_pc.yaml:player_data_refresh",
                {
                    "stages": ["location", "profile", "inventory", "characters"],
                    "inventory_categories": ["items"],
                },
                "刷新用户数据",
                0.0,
            )
        ]
        assert window.small_tasks_page.run_status.text() == "正在刷新用户数据……"

        window._active_game_name = "resonance_pc"
        window._on_task_finished(
            {
                "status": "success",
                "gui_item": {
                    "kind": "workflow_task",
                    "task_ref": "tasks:player_data_pc.yaml:player_data_refresh",
                },
                "final_result": {
                    "player_data": {
                        "metadata": {
                            "persisted": True,
                            "refreshed_at": "2026-08-25T12:00:00+08:00",
                        }
                    }
                },
            }
        )
        assert window.small_tasks_page.run_status.text() == "刷新完成"
        assert window.small_tasks_page.player_data_panel.tabs.currentIndex() == 1
        assert window._small_task_active_ref == ""

        team_category = window.small_tasks_page.category_list.findItems(
            "配队推荐", Qt.MatchFlag.MatchExactly
        )[0]
        window.small_tasks_page.category_list.setCurrentItem(team_category)
        window.small_tasks_page.team_recommendation_panel.run_button.click()
        assert dispatches[-1] == (
            "tasks:team_recommendation_pc.yaml:team_recommendation_pc",
            {},
            "配队推荐",
            0.0,
        )
        window._on_task_finished(
            {
                "status": "success",
                "gui_item": {
                    "kind": "workflow_task",
                    "task_ref": "tasks:team_recommendation_pc.yaml:team_recommendation_pc",
                },
                "final_result": {
                    "team_recommendations": {
                        "status": "blocked",
                        "message": "请先更新武器数据。",
                        "recommendations": [],
                    }
                },
            }
        )
        assert (
            window.small_tasks_page.team_recommendation_panel.status_label.text()
            == "请先更新武器数据。"
        )
        assert window._small_task_active_ref == ""

        window.refresh_target_button.click()
        assert refresh_requests == [True]

        window.primary_nav_buttons[window.SETTINGS_PAGE_INDEX].click()
        assert window.page_stack.currentWidget() is window.settings_page
        window.primary_nav_buttons[window.WORKFLOW_PAGE_INDEX].click()
        assert window.page_stack.currentWidget() is window.workflow_page
    finally:
        window.close()
        _application().processEvents()


def test_workflow_rejects_removed_player_data_configuration(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue(
        "workflow/task_order",
        "startup,player_data,commerce,battle,close",
    )
    repository = ResonanceConfigRepository(settings)

    with pytest.raises(ValueError, match="workflow/task_order"):
        WorkflowPage(repository)
