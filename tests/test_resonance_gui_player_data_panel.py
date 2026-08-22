from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets.player_data_panel import PlayerDataPanel


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _panel(tmp_path) -> PlayerDataPanel:
    _app()
    settings = QSettings(str(tmp_path / "player-data.ini"), QSettings.Format.IniFormat)
    return PlayerDataPanel(ResonanceConfigRepository(settings=settings))


def test_player_data_panel_defaults_to_all_stages_and_always_persists_selection(tmp_path):
    panel = _panel(tmp_path)
    try:
        assert panel.collect_inputs() == {
            "stages": [
                "location",
                "profile",
                "inventory",
            ],
            "inventory_categories": ["items"],
        }
        assert not hasattr(panel, "persist_check")
        assert "期限识别暂时关闭" in panel.inventory_expiry_notice.text()
        assert "按道具类型合并数量" in panel.inventory_expiry_notice.text()
        panel._select_stages(("profile", "inventory"))
        assert panel.collect_inputs() == {
            "stages": ["profile", "inventory"],
            "inventory_categories": ["items"],
        }
        assert "自动合并保存" in panel.selection_summary.text()
    finally:
        panel.close()

    reopened = _panel(tmp_path)
    try:
        assert reopened.collect_inputs() == {
            "stages": ["profile", "inventory"],
            "inventory_categories": ["items"],
        }
    finally:
        reopened.close()


def test_player_data_repository_migrates_old_nonpersisting_gui_selection(tmp_path):
    settings = QSettings(str(tmp_path / "old-player-data.ini"), QSettings.Format.IniFormat)
    settings.setValue("player_data/inputs_json", '{"stages":["profile","currencies"]}')
    repository = ResonanceConfigRepository(settings=settings)

    assert repository.load_player_data_inputs() == {
        "stages": ["profile", "inventory"],
        "inventory_categories": ["items"],
    }


def test_player_data_repository_migrates_old_status_and_currency_stages(tmp_path):
    settings = QSettings(str(tmp_path / "legacy-player-data.ini"), QSettings.Format.IniFormat)
    settings.setValue(
        "player_data/inputs_json",
        '{"stages":["clarity","fatigue","currencies"],"inventory_categories":["materials"]}',
    )
    repository = ResonanceConfigRepository(settings=settings)

    assert repository.load_player_data_inputs() == {
        "stages": ["profile", "inventory"],
        "inventory_categories": ["items", "materials"],
    }


def test_player_data_panel_rejects_empty_stage_selection(tmp_path):
    panel = _panel(tmp_path)
    try:
        panel._select_stages(())
        with pytest.raises(ValueError, match="至少需要选择一个读取阶段"):
            panel.collect_inputs()
    finally:
        panel.close()


def test_player_data_panel_summarizes_missing_cache_without_traceback(tmp_path):
    panel = _panel(tmp_path)
    try:
        panel.show_error(
            "{'message': 'No cached Resonance PC player data is available.', "
            "'traceback': 'Traceback (most recent call last): internal details'}"
        )

        assert panel.snapshot_message.text() == "没有可读缓存。"
        assert "Traceback" not in panel.snapshot_message.text()
    finally:
        panel.close()


def test_player_data_panel_requires_category_for_inventory_and_saves_both(tmp_path):
    panel = _panel(tmp_path)
    try:
        panel._select_stages(("inventory",))
        for check in panel._inventory_category_checks.values():
            check.setChecked(False)
        with pytest.raises(ValueError, match="至少需要选择道具或材料"):
            panel.collect_inputs()

        panel._inventory_category_checks["items"].setChecked(True)
        panel._inventory_category_checks["materials"].setChecked(True)
        assert panel.collect_inputs() == {
            "stages": ["inventory"],
            "inventory_categories": ["items", "materials"],
        }
    finally:
        panel.close()


def test_player_data_panel_renders_snapshot_times_inventory_and_filter(tmp_path):
    panel = _panel(tmp_path)
    try:
        panel.set_snapshot({
            "location": {"current_city": "岚心城"},
            "profile": {"uid": "123", "nickname": "测试账号", "level": 74},
            "status": {
                "cargo": {"current": 3, "max": 748},
                "clarity": {"current": 256, "max": 282},
                "fatigue": {"current": 161, "max": 848},
            },
            "currencies": {"iron_coins": 42396236, "birch_stone": 5970},
            "inventory": {
                "matched_stack_count": 2,
                "pages_scanned": 3,
                "items": [
                    {"item_id": "a", "name": "仙人掌能量棒棒糖", "count": 3,
                     "expiry": {"kind": "days_remaining", "value": 6}},
                    {"item_id": "b", "name": "履历情报", "count": 1114},
                ],
            },
            "metadata": {
                "updated_at": "2026-08-22T01:02:03+00:00",
                "section_updated_at": {
                    "profile": "2026-08-22T01:01:01+00:00",
                    "inventory": "2026-08-22T01:02:02+00:00",
                },
            },
        })

        assert "测试账号" in panel.identity_label.text()
        assert "42,396,236" in panel.currency_label.text()
        assert panel.inventory_table.rowCount() == 2
        assert panel.inventory_table.item(0, 2).text() == "6 天"
        assert "2 个物品堆" in panel.inventory_summary.text()
        assert panel._stage_times["profile"].text() != "从未更新"
        panel.inventory_search.setText("履历")
        assert panel.inventory_table.isRowHidden(0)
        assert not panel.inventory_table.isRowHidden(1)
    finally:
        panel.close()


def test_player_data_panel_renders_material_category_and_keeps_item_cache(tmp_path):
    panel = _panel(tmp_path)
    try:
        panel.set_snapshot({
            "inventory": {
                "schema_version": 2,
                "categories": {
                    "items": {
                        "items": [{"item_id": "item", "name": "测试道具", "count": 3}],
                    },
                    "materials": {
                        "materials": [
                            {
                                "material_id": "material",
                                "name": "测试材料",
                                "count": 2197,
                            }
                        ],
                        "matched_stack_count": 1,
                        "pages_scanned": 2,
                    },
                },
            },
            "metadata": {
                "inventory_category_updated_at": {
                    "items": "2026-08-22T01:00:00+00:00",
                    "materials": "2026-08-22T02:00:00+00:00",
                }
            },
        })
        panel.inventory_category_combo.setCurrentIndex(1)

        assert panel.inventory_table.rowCount() == 1
        assert panel.inventory_table.item(0, 0).text() == "测试材料"
        assert panel.inventory_table.item(0, 1).text() == "2197"
        assert panel.inventory_table.isColumnHidden(2)
        assert "1 个物品堆" in panel.inventory_summary.text()

        panel.inventory_category_combo.setCurrentIndex(0)
        assert panel.inventory_table.item(0, 0).text() == "测试道具"
        assert not panel.inventory_table.isColumnHidden(2)
    finally:
        panel.close()


def test_player_data_panel_hides_disabled_expiry_column_and_explains_merge(tmp_path):
    panel = _panel(tmp_path)
    try:
        panel.set_snapshot({
            "inventory": {
                "schema_version": 2,
                "categories": {
                    "items": {
                        "expiry_recognition_enabled": False,
                        "items": [
                            {
                                "item_id": "cactus_energy_lollipop",
                                "name": "仙人掌能量棒棒糖",
                                "count": 10,
                            }
                        ],
                        "matched_stack_count": 4,
                        "pages_scanned": 10,
                    }
                },
            }
        })

        assert panel.inventory_table.rowCount() == 1
        assert panel.inventory_table.isColumnHidden(2)
        assert "期限识别已暂停" in panel.inventory_summary.text()
        assert "按类型合并" in panel.inventory_summary.text()
    finally:
        panel.close()
