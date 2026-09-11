"""Independent profile choices, migration and partial snapshot display."""
from __future__ import annotations

import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QPushButton

from packages.resonance_gui.config_repository import (
    DEFAULT_PROFILE_SECTIONS,
    PLAYER_DATA_INPUTS_SCHEMA_VERSION,
    PLAYER_DATA_PROFILE_SECTION_ORDER,
    ResonanceConfigRepository,
)
from packages.resonance_gui.widgets.player_data_panel import PlayerDataPanel, _format_timestamp


@pytest.fixture
def panel(tmp_path):
    app = QApplication.instance() or QApplication([])
    repository = ResonanceConfigRepository(
        QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    widget = PlayerDataPanel(repository)
    yield widget
    widget.close()
    app.processEvents()


def _click_shortcut(panel, text):
    next(button for button in panel.findChildren(QPushButton) if button.text() == text).click()


def test_profile_defaults_shortcuts_and_disabled_selection_retention(panel):
    assert tuple(panel._profile_section_checks) == PLAYER_DATA_PROFILE_SECTION_ORDER
    assert panel.selected_profile_sections() == list(DEFAULT_PROFILE_SECTIONS)
    assert "用户信息 3 项" in panel.selection_summary.text()
    _click_shortcut(panel, "全部")
    assert panel.selected_profile_sections() == list(PLAYER_DATA_PROFILE_SECTION_ORDER)
    assert "用户信息 6 项" in panel.selection_summary.text()
    _click_shortcut(panel, "仅仓库")
    assert panel.selected_data_stages() == ["inventory"]
    assert all(not check.isEnabled() for check in panel._profile_section_checks.values())
    assert panel.selected_profile_sections() == list(PLAYER_DATA_PROFILE_SECTION_ORDER)
    _click_shortcut(panel, "仅角色")
    assert panel.selected_data_stages() == ["characters"]
    assert panel.selected_profile_sections() == list(PLAYER_DATA_PROFILE_SECTION_ORDER)
    _click_shortcut(panel, "基础信息")
    assert panel.selected_data_stages() == ["location", "profile"]
    assert panel.selected_profile_sections() == list(DEFAULT_PROFILE_SECTIONS)
    assert all(check.isEnabled() for check in panel._profile_section_checks.values())


def test_profile_selection_is_persisted_and_empty_only_rejected_when_enabled(panel):
    panel._select_stages(("profile",), profile_sections=("love_bentos", "work_meals", "sparkling_water"))
    inputs = panel.collect_inputs()
    assert inputs["profile_sections"] == ["sparkling_water", "work_meals", "love_bentos"]
    assert panel._settings.load_player_data_inputs() == inputs
    for check in panel._profile_section_checks.values():
        check.setChecked(False)
    with pytest.raises(ValueError, match="至少需要选择一个子项"):
        panel.collect_inputs()
    assert panel.selection_summary.property("status") == "error"
    panel._select_stages(("location",))
    assert panel.collect_inputs()["profile_sections"] == []
    panel._load_inputs()
    assert panel.selected_profile_sections() == []


@pytest.mark.parametrize("stages,categories", [(["profile"], ["items"]), (["inventory"], ["materials", "equipment"])])
def test_version_three_migration_adds_only_original_three_and_runs_once(tmp_path, stages, categories):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("player_data/inputs_schema_version", 3)
    settings.setValue("player_data/inputs_json", json.dumps({"stages": stages, "inventory_categories": categories}))
    repository = ResonanceConfigRepository(settings)
    migrated = repository.load_player_data_inputs()
    assert migrated == {"stages": stages, "inventory_categories": categories, "profile_sections": list(DEFAULT_PROFILE_SECTIONS)}
    assert int(settings.value("player_data/inputs_schema_version")) == PLAYER_DATA_INPUTS_SCHEMA_VERSION
    repository.save_player_data_inputs({**migrated, "profile_sections": ["work_meals", "love_bentos"]})
    assert repository.load_player_data_inputs()["profile_sections"] == ["work_meals", "love_bentos"]


def test_profile_snapshot_partial_values_and_times_preserve_unselected(panel):
    old = {
        "status": {"cargo": {"current": 1, "max": 200}, "clarity": {"current": 250, "max": 200}},
        "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6}},
        "metadata": {
            "section_updated_at": {"profile": "2026-09-01T00:00:00Z"},
            "profile_section_updated_at": {"cargo": "cargo-old", "clarity": "clarity-old", "sparkling_water": "water-old"},
        },
    }
    original = copy.deepcopy(old)
    panel.set_snapshot(old)
    fresh = {
        "recovery": {"work_meals": {"available_count": 0, "slots": [
            {"issue_time": issue_time, "available": False} for issue_time in ("05:00", "12:00", "18:00")
        ]}},
        "metadata": {"persisted": True, "refreshed_at": "2026-09-05T01:00:00Z", "section_updated_at": {"profile": "new"}, "profile_section_updated_at": {"work_meals": "work-meals-new"}},
    }
    panel.apply_refresh_result(fresh)
    assert old == original
    assert panel._snapshot["status"] == old["status"]
    assert panel.profile_value_labels["cargo"].text() == "1 / 200"
    assert panel.profile_value_labels["clarity"].text() == "250 / 200"
    assert panel.profile_value_labels["fatigue"].text() == "未读取"
    assert panel.profile_value_labels["sparkling_water"].text() == "剩余免费次数 6 / 6"
    assert panel.profile_value_labels["work_meals"].text() == "0 / 3 · 05:00 无 · 12:00 无 · 18:00 无"
    assert panel.profile_value_labels["love_bentos"].text() == "未读取"
    assert panel.profile_time_labels["cargo"].text() == "cargo-old"
    assert panel.profile_time_labels["work_meals"].text() == "work-meals-new"
    assert panel.profile_time_labels["love_bentos"].text() == "从未更新"
    assert panel.profile_time_labels["fatigue"].text() == "从未更新"
    panel.apply_refresh_result({"recovery": {"sparkling_water": {"remaining_free_uses": 0, "daily_free_limit": 6}}, "metadata": {"persisted": True, "profile_section_updated_at": {"sparkling_water": "water-new"}}})
    assert panel.profile_value_labels["sparkling_water"].text() == "剩余免费次数 0 / 6"
    assert panel._snapshot["recovery"]["work_meals"] == fresh["recovery"]["work_meals"]


def test_love_bentos_partial_refresh_preserves_other_resources_and_clears_rows(panel):
    old = {
        "recovery": {
            "sparkling_water": {"remaining_free_uses": 4, "daily_free_limit": 6},
            "work_meals": {"available_count": 2},
        },
        "metadata": {"profile_section_updated_at": {
            "sparkling_water": "water-old", "work_meals": "work-meals-old",
        }},
    }
    original = copy.deepcopy(old)
    panel.set_snapshot(old)
    fresh = {
        "recovery": {"love_bentos": {"count": 2, "items": [
            {"role_name": "测试角色甲", "food_name": "测试菜品甲", "remaining_days": 3},
            {"role_name": "测试角色乙", "food_name": "测试菜品乙", "remaining_days": 0},
        ]}},
        "metadata": {"persisted": True, "profile_section_updated_at": {"love_bentos": "love-new"}},
    }
    expected_love = copy.deepcopy(fresh["recovery"]["love_bentos"])
    panel.apply_refresh_result(fresh)
    assert old == original
    assert panel.profile_value_labels["love_bentos"].text() == "2 份"
    assert panel.profile_time_labels["love_bentos"].text() == "love-new"
    assert panel.love_bento_table.rowCount() == 2
    assert panel.love_bento_table.columnCount() == 3
    for row, item in enumerate(expected_love["items"]):
        assert [panel.love_bento_table.item(row, column).text() for column in range(3)] == [
            item["role_name"], item["food_name"], str(item["remaining_days"]),
        ]
    for key in ("sparkling_water", "work_meals"):
        assert panel._snapshot["recovery"][key] == old["recovery"][key]
        assert panel.profile_time_labels[key].text() == old["metadata"]["profile_section_updated_at"][key]
    fresh["recovery"]["love_bentos"]["items"][0]["role_name"] = "changed"
    assert panel._snapshot["recovery"]["love_bentos"] == expected_love

    for key, value in (
        ("work_meals", {"available_count": 0}),
        ("sparkling_water", {"remaining_free_uses": 0, "daily_free_limit": 6}),
    ):
        panel.apply_refresh_result({
            "recovery": {key: value},
            "metadata": {"persisted": True, "profile_section_updated_at": {key: "new"}},
        })
        assert panel._snapshot["recovery"]["love_bentos"] == expected_love
        assert panel.profile_time_labels["love_bentos"].text() == "love-new"
        assert panel.love_bento_table.rowCount() == 2

    panel.apply_refresh_result({
        "recovery": {"love_bentos": {"count": 0, "items": []}},
        "metadata": {"persisted": True, "profile_section_updated_at": {"love_bentos": "love-empty"}},
    })
    assert panel.profile_value_labels["love_bentos"].text() == "0 份"
    assert panel.profile_time_labels["love_bentos"].text() == "love-empty"
    assert panel.love_bento_table.rowCount() == 0
    assert panel.profile_value_labels["work_meals"].text() == "0 / 3"
    assert panel.profile_value_labels["sparkling_water"].text() == "剩余免费次数 0 / 6"


def test_legacy_profile_time_is_frozen_before_first_partial_refresh(panel):
    old_time = "2026-09-01T00:00:00Z"
    panel.set_snapshot({"status": {"cargo": {"current": 1, "max": 2}}, "metadata": {"section_updated_at": {"profile": old_time}}})
    expected = _format_timestamp(old_time) + "（旧记录）"
    assert panel.profile_time_labels["cargo"].text() == expected
    panel.apply_refresh_result({"recovery": {"work_meals": {"available_count": 2}}, "metadata": {"persisted": True, "section_updated_at": {"profile": "new"}, "profile_section_updated_at": {"work_meals": "new"}}})
    assert panel._snapshot["metadata"]["profile_legacy_updated_at"] == old_time
    assert panel.profile_time_labels["cargo"].text() == expected
    assert panel.profile_time_labels["work_meals"].text() == "new"
    assert panel.profile_time_labels["sparkling_water"].text() == "从未更新"


def test_headless_profile_layout_preview(panel, tmp_path):
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    if font_id >= 0:
        panel.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    panel.resize(960, 740)
    panel.show()
    QApplication.processEvents()
    assert panel.grab().save(str(tmp_path / "profile_selection.png"))
    panel.set_snapshot({"status": {"cargo": {"current": 126, "max": 748}}, "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6}, "work_meals": {"available_count": 2, "slots": [{"issue_time": time, "available": time != "05:00"} for time in ("05:00", "12:00", "18:00")]}, "love_bentos": {"count": 1, "items": [{"role_name": "测试角色", "food_name": "测试菜品", "remaining_days": 3}]}}, "metadata": {"profile_section_updated_at": {key: "2026-09-05T01:00:00Z" for key in PLAYER_DATA_PROFILE_SECTION_ORDER}}})
    QApplication.processEvents()
    assert panel.grab().save(str(tmp_path / "profile_snapshot.png"))
    for label in panel.profile_value_labels.values():
        assert label.height() >= label.fontMetrics().height()
