"""Team sync uses explicit business identities independently of portrait layout."""
import json
from pathlib import Path

import pytest

from tools import sync_team_recommendations as sync


ROOT = Path(__file__).resolve().parents[2]


def test_build_uses_catalog_identities_with_relocated_shared_portraits(monkeypatch):
    known = sync._load_character_ids()
    assert {"弗兰", "空", "爱弥儿"} <= known
    team = json.loads((ROOT / "plans/resonance_pc/data/meta/team_recommendations.json").read_text(encoding="utf8"))["teams"][0]
    card = {"title": team["title"], "members": [m["character_id"] for m in team["members"]], "sections": team["categories"]}
    lines = ["{{娃娃鱼报社配队攻略"]
    for member in team["members"]:
        lines.extend([f"|角色{member['slot']}={member['character_id']}", f"|角色{member['slot']}最低觉醒={member['minimum_awakening']}"])
    lines.append("}}")
    monkeypatch.setattr(sync, "_load_team_cards", lambda: ([card], 1))
    monkeypatch.setattr(sync, "_load_page_sources", lambda titles: {team["title"]: {"wikitext": "\n".join(lines), "revision_id": 1}})
    result = sync.build_catalog(expected_count=1)
    assert [m["character_id"] for m in result["teams"][0]["members"]] == [m["character_id"] for m in team["members"]]


@pytest.mark.parametrize("payload", [[], {}, {"characters": []}, {"characters": [{}]}, {"characters": [{"character_id": " "}]}, {"characters": [{"character_id": "空"}, {"character_id": "空"}]}])
def test_invalid_identity_catalog_fails_before_fetching(tmp_path, monkeypatch, payload):
    path = tmp_path / "characters.json"
    path.write_text(json.dumps(payload), encoding="utf8")
    monkeypatch.setattr(sync, "CHARACTER_CATALOG", path)
    monkeypatch.setattr(sync, "_load_team_cards", lambda: pytest.fail("invalid catalog must fail before network fetch"))
    with pytest.raises(ValueError):
        sync.build_catalog()


def test_identity_values_are_not_derived_from_shared_image_names(tmp_path, monkeypatch):
    path = tmp_path / "characters.json"
    path.write_text(json.dumps({"characters": [{"character_id": identity, "templates": ["templates/characters/portraits/shared.png"]} for identity in ("弗兰", "空")]}), encoding="utf8")
    monkeypatch.setattr(sync, "CHARACTER_CATALOG", path)
    assert sync._load_character_ids() == {"弗兰", "空"}
