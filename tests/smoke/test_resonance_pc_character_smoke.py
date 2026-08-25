"""Fast regression checks for PC character player-data recognition."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton

from packages.aura_core.utils.exceptions import StopTaskException
from packages.resonance_gui.config_repository import (
    PLAYER_DATA_STAGE_ORDER,
    ResonanceConfigRepository,
)
from packages.resonance_gui.widgets.player_data_panel import PlayerDataPanel
from plans.resonance_pc.src.actions import character_pc_actions as characters
from plans.resonance_pc.src.actions import player_data_pc_actions as player_data


REPO_ROOT = Path(__file__).resolve().parents[2]


class _Vision:
    def __init__(self, *, star_scores: list[float], identity_matches: list[object] | None = None):
        self.star_scores = list(star_scores)
        self.identity_matches = list(identity_matches or [])

    def find_all_templates_batch(self, *, template_images, **_kwargs):
        results = [SimpleNamespace(matches=[]) for _ in template_images]
        if results and self.identity_matches:
            results[0] = SimpleNamespace(matches=list(self.identity_matches))
        return results

    def find_template(self, *, threshold: float, **_kwargs):
        score = self.star_scores.pop(0)
        return SimpleNamespace(found=score >= threshold, confidence=score)


class _App:
    def __init__(self) -> None:
        self.drags: list[tuple[tuple, dict]] = []
        self.clicks: list[dict] = []

    def drag(self, *args, **kwargs) -> None:
        self.drags.append((args, kwargs))

    def click(self, **kwargs) -> None:
        self.clicks.append(dict(kwargs))


def _minimal_scan_catalog() -> dict:
    return {
        "layout": {
            "no_new_scan_limit": 3,
            "max_scrolls": 30,
            "scroll_start": (1000, 620),
            "scroll_end": (1000, 310),
            "scroll_duration_sec": 0.5,
            "scroll_hold_before_release_sec": 0.5,
        },
        "characters": [{"character_id": "甲"}, {"character_id": "乙"}],
        "templates": [{"template_ref": "甲/01.png"}, {"template_ref": "乙/01.png"}],
    }


def _observation(name: str, stars: int, confidence: float = 0.9) -> dict:
    return {
        "character_id": name,
        "name": name,
        "stars": stars,
        "confidence": confidence,
        "card_top_left": (20, 10),
    }


def _write_png(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.full((height, width, 3), 127, dtype=np.uint8)
    success, encoded = cv2.imencode(".png", image)
    assert success
    encoded.tofile(str(path))


def _temporary_character_config(tmp_path: Path) -> tuple[Path, Path]:
    config = json.loads(
        (
            REPO_ROOT / "plans/resonance_pc/data/meta/player_characters.json"
        ).read_text(encoding="utf-8")
    )
    template_root = tmp_path / "templates"
    character_root = template_root / "characters"
    template_root.mkdir(parents=True, exist_ok=True)
    config["templates"] = {
        "character_root": "templates/characters",
        "entry": "templates/entry.png",
        "lit_star": "templates/star.png",
        "lit_star_mask": "templates/star_mask.png",
    }
    shutil.copyfile(
        REPO_ROOT / "plans/resonance_pc/templates/player_data_character_entry.png",
        template_root / "entry.png",
    )
    shutil.copyfile(
        REPO_ROOT / "plans/resonance_pc/templates/character_stars/lit/01.png",
        template_root / "star.png",
    )
    shutil.copyfile(
        REPO_ROOT / "plans/resonance_pc/templates/character_stars/lit/01_mask.png",
        template_root / "star_mask.png",
    )
    config_path = tmp_path / "player_characters.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return config_path, character_root


def test_character_assets_are_discovered_from_named_unicode_folders() -> None:
    catalog = characters.load_character_catalog()
    character_root = REPO_ROOT / "plans" / "resonance_pc" / "templates" / "characters"
    named_folders = [item for item in character_root.iterdir() if item.is_dir()]

    assert len(named_folders) == 100
    assert len(catalog["characters"]) == 77
    assert len(catalog["templates"]) == 95
    assert {item["name"] for item in catalog["characters"]} >= {
        "夏娜",
        "冯·里奈",
        "圣剑波克士",
    }
    assert all(item["template_image"].shape == (140, 140, 3) for item in catalog["templates"])
    assert catalog["layout"]["star_slots_from_card"] == (
        (38, 242),
        (62, 242),
        (86, 242),
        (110, 242),
        (134, 242),
    )


def test_character_catalog_groups_multiple_skins_skips_placeholders_and_rejects_bad_sizes(
    tmp_path,
) -> None:
    config_path, character_root = _temporary_character_config(tmp_path)
    _write_png(character_root / "测试角色" / "默认.png", 140, 140)
    _write_png(character_root / "测试角色" / "新皮肤.png", 140, 140)

    catalog = characters.load_character_catalog(
        config_path,
        plan_root=tmp_path,
    )

    assert len(catalog["characters"]) == 1
    assert len(catalog["templates"]) == 2
    assert catalog["characters"][0]["name"] == "测试角色"

    (character_root / "空角色").mkdir()
    catalog = characters.load_character_catalog(config_path, plan_root=tmp_path)
    assert [item["name"] for item in catalog["characters"]] == ["测试角色"]
    assert len(catalog["templates"]) == 2

    _write_png(character_root / "坏尺寸" / "01.png", 139, 140)
    with pytest.raises(ValueError, match="140x140"):
        characters.load_character_catalog(config_path, plan_root=tmp_path)


def test_cross_skin_matches_at_the_same_card_are_suppressed() -> None:
    kept = characters._suppress_overlapping_cards(
        [
            {"confidence": 0.85, "card_top_left": (20, 10), "character_id": "甲"},
            {"confidence": 0.95, "card_top_left": (21, 11), "character_id": "甲"},
            {"confidence": 0.9, "card_top_left": (200, 10), "character_id": "乙"},
        ]
    )

    assert [(item["character_id"], item["confidence"]) for item in kept] == [
        ("甲", 0.95),
        ("乙", 0.9),
    ]


def test_single_page_matches_identity_and_classifies_five_star_slots() -> None:
    catalog = characters.load_character_catalog()
    identity_match = SimpleNamespace(top_left=(34, 62), confidence=0.95)
    vision = _Vision(
        star_scores=[0.95, 0.9, 0.85, 0.2, 0.1],
        identity_matches=[identity_match],
    )
    page = np.zeros((650, 1280, 3), dtype=np.uint8)

    observations = characters.scan_character_page(page, catalog, vision)

    assert len(observations) == 1
    assert observations[0]["stars"] == 3
    assert observations[0]["character_id"] == catalog["templates"][0]["character_id"]


def test_ambiguous_star_score_fails_the_character_stage() -> None:
    catalog = characters.load_character_catalog()
    vision = _Vision(star_scores=[0.95, 0.6, 0.2, 0.2, 0.2])
    page = np.zeros((650, 1280, 3), dtype=np.uint8)

    with pytest.raises(StopTaskException, match="ambiguous"):
        characters.read_character_stars(
            page,
            (20, 10),
            catalog,
            vision,
            character_name="测试角色",
        )


def test_character_scan_stops_after_three_pages_without_new_ids() -> None:
    app = _App()
    page = np.zeros((10, 10, 3), dtype=np.uint8)
    scans = [
        [_observation("甲", 2)],
        [_observation("甲", 2), _observation("乙", 4)],
        [_observation("甲", 2), _observation("乙", 4)],
        [_observation("甲", 2), _observation("乙", 4)],
        [_observation("甲", 2), _observation("乙", 4)],
    ]
    with (
        patch.object(characters, "scan_character_page", side_effect=scans),
        patch.object(characters, "_capture_stable_grid", return_value=page),
    ):
        result = characters.read_player_characters(
            app,
            object(),
            _minimal_scan_catalog(),
            first_page_image=page,
        )

    assert result["matched_character_count"] == 2
    assert result["pages_scanned"] == 5
    assert result["completion_reason"] == "three_consecutive_scans_without_new_characters"
    assert [entry["character_id"] for entry in result["entries"]] == ["乙", "甲"]
    assert len(app.drags) == 4
    assert all(item[1]["hold_before_release_sec"] == 0.5 for item in app.drags)


def test_repeated_character_with_conflicting_stars_fails() -> None:
    app = _App()
    page = np.zeros((10, 10, 3), dtype=np.uint8)
    with (
        patch.object(
            characters,
            "scan_character_page",
            side_effect=[[_observation("甲", 2)], [_observation("甲", 3)]],
        ),
        patch.object(characters, "_capture_stable_grid", return_value=page),
    ):
        with pytest.raises(StopTaskException, match="disagree"):
            characters.read_player_characters(
                app,
                object(),
                _minimal_scan_catalog(),
                first_page_image=page,
            )


def test_character_scan_enforces_maximum_scroll_count() -> None:
    app = _App()
    page = np.zeros((10, 10, 3), dtype=np.uint8)
    catalog = _minimal_scan_catalog()
    catalog["layout"]["max_scrolls"] = 1
    with (
        patch.object(
            characters,
            "scan_character_page",
            side_effect=[[_observation("甲", 2)], [_observation("乙", 4)]],
        ),
        patch.object(characters, "_capture_stable_grid", return_value=page),
    ):
        with pytest.raises(StopTaskException, match="maximum scroll"):
            characters.read_player_characters(
                app,
                object(),
                catalog,
                first_page_image=page,
            )


def test_player_data_cache_replaces_character_section_and_defaults_to_four_stages() -> None:
    assert player_data._normalize_stages() == (
        "location",
        "profile",
        "inventory",
        "characters",
    )
    existing = {
        "characters": {"entries": [{"character_id": "旧", "stars": 1}]},
        "location": {"current_city": "旧城市"},
        "metadata": {"section_updated_at": {"characters": "old"}},
    }
    fresh = {
        "characters": {"entries": [{"character_id": "新", "stars": 5}]},
    }

    merged = player_data._merge_latest(
        existing,
        fresh,
        section_updated_at={"characters": "new"},
        updated_at="new",
    )

    assert merged["characters"] == fresh["characters"]
    assert merged["location"] == existing["location"]
    assert merged["metadata"]["section_updated_at"]["characters"] == "new"


def test_character_failure_does_not_reach_cache_persistence() -> None:
    app = _App()
    with (
        patch.object(player_data, "_wait_for_any_marker"),
        patch.object(player_data, "load_character_catalog", return_value={}),
        patch.object(
            player_data,
            "enter_character_page",
            return_value=np.zeros((10, 10, 3), dtype=np.uint8),
        ),
        patch.object(
            player_data,
            "read_player_characters",
            side_effect=StopTaskException("character failed", success=False),
        ),
        patch.object(player_data, "_persist_latest") as persist,
    ):
        with pytest.raises(StopTaskException, match="character failed"):
            player_data.resonance_pc_player_data_refresh(
                stages=["characters"],
                app=app,
                ocr=object(),
                vision=object(),
            )
    persist.assert_not_called()


def test_gui_migrates_old_inputs_once_and_renders_character_snapshot(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "player-data.ini"), QSettings.Format.IniFormat)
    settings.setValue(
        "player_data/inputs_json",
        json.dumps({"stages": ["location"], "inventory_categories": ["items"]}),
    )
    repository = ResonanceConfigRepository(settings=settings)

    migrated = repository.load_player_data_inputs()
    assert migrated["stages"] == ["location", "characters"]
    repository.save_player_data_inputs(
        {"stages": ["profile"], "inventory_categories": ["items"]}
    )
    assert repository.load_player_data_inputs()["stages"] == ["profile"]
    assert PLAYER_DATA_STAGE_ORDER[-1] == "characters"

    panel = PlayerDataPanel(repository)
    panel.show()
    app.processEvents()
    try:
        buttons = {button.text(): button for button in panel.findChildren(QPushButton)}
        buttons["仅角色"].click()
        assert panel.selected_data_stages() == ["characters"]
        panel.set_snapshot(
            {
                "characters": {
                    "pages_scanned": 4,
                    "entries": [{"character_id": "夏娜", "name": "夏娜", "stars": 3}],
                },
                "metadata": {"section_updated_at": {"characters": "2026-08-24T00:00:00+00:00"}},
            }
        )
        assert panel.character_table.rowCount() == 1
        assert panel.character_table.item(0, 0).text() == "夏娜"
        assert panel.character_table.item(0, 1).text() == "★★★☆☆（3/5）"
        assert "4 页" in panel.character_summary.text()
    finally:
        panel.close()


def test_player_data_task_schema_exports_characters_stage() -> None:
    payload = yaml.safe_load(
        (REPO_ROOT / "plans/resonance_pc/tasks/player_data_pc.yaml").read_text(
            encoding="utf-8"
        )
    )
    inputs = payload["player_data_refresh"]["meta"]["inputs"]
    stages = next(item for item in inputs if item["name"] == "stages")

    assert stages["default"] == ["location", "profile", "inventory", "characters"]
    assert stages["item"]["enum"] == ["location", "profile", "inventory", "characters"]
