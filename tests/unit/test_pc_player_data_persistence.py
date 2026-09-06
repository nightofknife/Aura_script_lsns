from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import _player_data_persistence as persistence
from plans.resonance_pc.src.actions.player_data_pc_actions import _merge_latest
from plans.resonance_pc.src.actions import player_data_pc_actions as player


@pytest.mark.parametrize("legacy_text", ['{"location":{"current_city":"旧城"}}', 'not-json', '[]'])
def test_legacy_cache_is_ignored_and_missing_new_cache_does_not_write(tmp_path: Path, legacy_text) -> None:
    legacy = tmp_path / "install/plans/resonance_pc/data/cache/player/latest.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(legacy_text, encoding="utf-8")
    service = PersistentDataService(tmp_path / "install")
    with pytest.raises(persistence.PlayerDataPersistenceError) as exc_info:
        player.resonance_pc_player_data_get_latest(persistent_data=service)
    assert exc_info.value.code == "player_data_incomplete"
    assert service.inspect("user-info.json")["exists"] is False
    assert legacy.read_text(encoding="utf-8") == legacy_text
    assert not hasattr(persistence,"ensure_pc_user_info_migrated")
    assert not hasattr(persistence,"LEGACY_PLAYER_DATA_FILE")


def test_latest_reads_only_new_file_without_mutating_it(tmp_path: Path) -> None:
    service = PersistentDataService(tmp_path / "install")
    payload = {"status":{"cargo":{"current":1,"max":2}},"metadata":{"updated_at":"old"}}
    service.set("user-info.json", [], payload)
    path=service.root / "user-info.json"
    before=path.read_bytes()
    result=player.resonance_pc_player_data_get_latest(persistent_data=service)
    assert result == payload
    result["status"]["cargo"]["current"] = 99
    assert path.read_bytes()==before


@pytest.mark.parametrize("invalid", ["not-json", "[]", "null"])
def test_invalid_new_file_is_rejected_not_overwritten(tmp_path: Path, invalid) -> None:
    service=PersistentDataService(tmp_path / "install")
    service.root.mkdir(parents=True)
    path=service.root / "user-info.json"
    path.write_text(invalid,encoding="utf-8")
    with pytest.raises(persistence.PlayerDataPersistenceError) as exc_info:
        persistence.load_pc_user_info(service)
    assert exc_info.value.code=="player_data_invalid"
    assert path.read_text(encoding="utf-8")==invalid


def test_first_partial_refresh_creates_new_file_without_importing_legacy(tmp_path: Path, monkeypatch) -> None:
    service=PersistentDataService(tmp_path / "install")
    legacy=tmp_path / "install/plans/resonance_pc/data/cache/player/latest.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"inventory":{"legacy_only":True}}),encoding="utf-8")
    before=legacy.read_bytes()
    monkeypatch.setattr(player,"_wait_for_any_marker",lambda *args,**kwargs:[])
    monkeypatch.setattr(player,"_capture_ocr_items",lambda *args,**kwargs:[{"text":"修格里城"}])
    result=player.resonance_pc_player_data_refresh(stages=["location"],app=object(),ocr=object(),persistent_data=service)
    saved=persistence.load_pc_user_info(service)
    assert result["metadata"]["persisted"]
    assert saved["location"]=={"current_city":"修格里城"}
    assert "inventory" not in saved
    assert "migration" not in saved["metadata"]
    assert legacy.read_bytes()==before


def test_player_refresh_merge_preserves_future_user_sections() -> None:
    existing = {
        "daily": {"tavern_drink": {"used": 2}},
        "metadata": {
            "section_updated_at": {"daily.tavern_drink": "daily-time"},
            "custom": {"kept": True},
        },
    }
    merged = _merge_latest(
        existing,
        {"location": {"current_city": "海角城"}},
        section_updated_at={"location": "location-time"},
        updated_at="now",
    )

    assert merged["daily"]["tavern_drink"]["used"] == 2
    assert merged["metadata"]["section_updated_at"] == {
        "daily.tavern_drink": "daily-time",
        "location": "location-time",
    }
    assert merged["metadata"]["custom"] == {"kept": True}
    assert merged["schema_version"] == 1
