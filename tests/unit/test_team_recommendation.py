"""Smoke checks for strict fixed-team matching."""

from __future__ import annotations

import json
from pathlib import Path

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions.team_recommendation_pc_actions import (
    load_team_recommendation_catalog,
    recommend_fixed_teams,
    resonance_pc_team_recommendations,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _member(
    slot: int,
    character_id: str,
    *,
    minimum: int = 1,
    recommended: int | None = 3,
    full: str | None = None,
    low: str | None = None,
) -> dict:
    return {
        "slot": slot,
        "character_id": character_id,
        "minimum_awakening": minimum,
        "recommended_awakening": recommended,
        "full_weapon_id": full or f"满配{slot}",
        "low_weapon_id": low or f"低配{slot}",
    }


def _catalog(*teams: dict) -> dict:
    return {"schema_version": 1, "teams": list(teams)}


def _team(team_id: str = "测试队") -> dict:
    return {
        "team_id": team_id,
        "title": team_id,
        "guide_name": team_id,
        "categories": ["攻坚"],
        "source_url": "https://example.invalid/team",
        "members": [
            _member(1, "甲", full="共享满配", low="共享低配"),
            _member(2, "乙", full="共享满配", low="共享低配"),
            _member(3, "丙"),
            _member(4, "丁"),
            _member(5, "戊"),
        ],
    }


def _player_data(
    *,
    stars: int = 3,
    characters: tuple[str, ...] = ("甲", "乙", "丙", "丁", "戊"),
    weapons: dict[str, int] | None = None,
) -> dict:
    weapon_counts = weapons or {
        "共享满配": 2,
        "满配3": 1,
        "满配4": 1,
        "满配5": 1,
    }
    return {
        "characters": {
            "schema_version": 1,
            "scan_complete": True,
            "entries": [
                {"character_id": name, "name": name, "stars": stars}
                for name in characters
            ],
        },
        "weapons": {
            "schema_version": 1,
            "scan_complete": True,
            "entries": [
                {"weapon_id": name, "quantity": quantity}
                for name, quantity in weapon_counts.items()
            ],
        },
    }


def test_missing_player_sections_block_recommendation() -> None:
    result = recommend_fixed_teams({}, _catalog(_team()))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "player_data_incomplete"
    assert result["missing_sections"] == ["characters", "weapons"]
    assert result["recommendations"] == []


def test_missing_character_or_low_awakening_excludes_team() -> None:
    missing = recommend_fixed_teams(
        _player_data(characters=("甲", "乙", "丙", "丁")),
        _catalog(_team()),
    )
    low = recommend_fixed_teams(_player_data(stars=0), _catalog(_team()))

    assert missing["status"] == "success"
    assert missing["recommendation_count"] == 0
    assert low["recommendation_count"] == 0


def test_character_status_uses_minimum_and_recommended_boundaries() -> None:
    basic = recommend_fixed_teams(_player_data(stars=1), _catalog(_team()))
    complete = recommend_fixed_teams(_player_data(stars=3), _catalog(_team()))
    unknown_recommended_team = _team("推荐值缺失")
    unknown_recommended_team["members"][0]["recommended_awakening"] = None
    unknown = recommend_fixed_teams(
        _player_data(stars=5),
        _catalog(unknown_recommended_team),
    )

    assert basic["recommendations"][0]["character_status"] == "basic"
    assert complete["recommendations"][0]["character_status"] == "complete"
    assert unknown["recommendations"][0]["character_status"] == "basic"


def test_weapon_status_reuses_owned_weapons_and_accepts_mixed_low_configuration() -> None:
    full = recommend_fixed_teams(_player_data(), _catalog(_team()))
    mixed_low = recommend_fixed_teams(
        _player_data(
            weapons={
                "共享满配": 1,
                "共享低配": 1,
                "低配3": 1,
                "满配4": 1,
                "低配5": 1,
            }
        ),
        _catalog(_team()),
    )
    shared_low = recommend_fixed_teams(
        _player_data(
            weapons={
                "共享低配": 1,
                "低配3": 1,
                "低配4": 1,
                "低配5": 1,
            }
        ),
        _catalog(_team()),
    )

    assert full["recommendations"][0]["weapon_status"] == "full"
    assert mixed_low["recommendations"][0]["weapon_status"] == "low"
    assert shared_low["recommendations"][0]["weapon_status"] == "low"
    missing_weapon = _player_data(weapons={"共享低配": 1, "低配3": 1, "低配4": 1})
    insufficient = recommend_fixed_teams(missing_weapon, _catalog(_team()))
    assert insufficient["recommendations"][0]["weapon_status"] == "unmet"


def test_invalid_weapon_schema_is_blocked_without_name_fallback() -> None:
    player_data = _player_data()
    player_data["weapons"]["entries"] = [{"name": "共享满配", "quantity": 2}]

    result = recommend_fixed_teams(player_data, _catalog(_team()))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "player_data_invalid"
    assert result["invalid_sections"] == ["weapons"]


def test_actual_catalog_contains_all_fixed_wiki_teams() -> None:
    catalog = load_team_recommendation_catalog(
        REPO_ROOT
        / "plans"
        / "resonance_pc"
        / "data"
        / "meta"
        / "team_recommendations.json"
    )

    assert catalog["source"]["revision_id"] == 30014
    assert catalog["team_count"] == 123
    assert len(catalog["teams"]) == 123
    assert {team["title"] for team in catalog["teams"]} >= {
        "爱弥儿标准队",
        "索玛红卡队2.0",
        "单金负核：那由他",
    }


def test_task_action_reads_cache_and_returns_stable_envelope(tmp_path) -> None:
    persistent_data = PersistentDataService(tmp_path)
    missing = resonance_pc_team_recommendations(persistent_data=persistent_data)
    assert missing["status"] == "blocked"

    catalog_file = tmp_path / "catalog.json"
    persistent_data.set("user-info.json", [], _player_data())
    catalog_payload = _catalog(_team())
    catalog_payload["team_count"] = 1
    catalog_file.write_text(
        json.dumps(catalog_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    result = resonance_pc_team_recommendations(
        catalog_file=str(catalog_file),
        persistent_data=persistent_data,
    )

    assert result["status"] == "success"
    assert result["recommendation_count"] == 1
    assert result["recommendations"][0]["team_id"] == "测试队"
