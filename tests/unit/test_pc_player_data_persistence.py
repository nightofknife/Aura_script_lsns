from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import _player_data_persistence as persistence
from plans.resonance_pc.src.actions.player_data_pc_actions import _merge_latest


def test_imports_legacy_player_data_once_and_keeps_source(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "legacy" / "latest.json"
    legacy.parent.mkdir()
    legacy_payload = {
        "profile": {"name": "测试用户"},
        "metadata": {"updated_at": "old"},
    }
    legacy.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(persistence, "LEGACY_PLAYER_DATA_FILE", legacy)
    service = PersistentDataService(tmp_path / "install")

    assert persistence.ensure_pc_user_info_migrated(service) is True
    imported = service.read("user-info.json")
    assert imported["profile"] == legacy_payload["profile"]
    assert imported["schema_version"] == 1
    assert imported["metadata"]["migration"]["source"].endswith("player/latest.json")
    assert legacy.is_file()

    legacy.write_text(json.dumps({"profile": {"name": "new legacy"}}), encoding="utf-8")
    assert persistence.ensure_pc_user_info_migrated(service) is False
    assert service.read("user-info.json", ["profile", "name"]) == "测试用户"


def test_invalid_legacy_data_is_not_replaced(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(persistence, "LEGACY_PLAYER_DATA_FILE", legacy)
    service = PersistentDataService(tmp_path / "install")

    with pytest.raises(persistence.PlayerDataPersistenceError) as exc_info:
        persistence.ensure_pc_user_info_migrated(service)
    assert exc_info.value.code == "player_data_invalid"
    assert service.inspect("user-info.json")["exists"] is False
    assert legacy.read_text(encoding="utf-8") == "not-json"


def test_existing_user_info_takes_precedence_over_legacy(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"profile": {"name": "legacy"}}), encoding="utf-8")
    monkeypatch.setattr(persistence, "LEGACY_PLAYER_DATA_FILE", legacy)
    service = PersistentDataService(tmp_path / "install")
    service.set("user-info.json", [], {"profile": {"name": "current"}})

    assert persistence.load_pc_user_info(service)["profile"]["name"] == "current"


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
