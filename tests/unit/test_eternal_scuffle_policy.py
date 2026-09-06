"""Pure selection contracts and the promoted asset bundle integrity."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
spec = importlib.util.spec_from_file_location("scuffle_policy", PLAN / "src/actions/_eternal_scuffle_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


@pytest.mark.parametrize("coins,runs", [(1, 1), (5, 9999), (3, 20)])
def test_valid_inputs(coins, runs):
    assert policy.validate_inputs(coins, runs) == {"coins_per_run": coins, "run_count": runs}


@pytest.mark.parametrize("coins,runs", [(0, 1), (6, 1), (1, 0), (1, 10000), (True, 1), (1, False), (1.0, 2), ("2", 1)])
def test_invalid_inputs_are_not_coerced(coins, runs):
    with pytest.raises(ValueError):
        policy.validate_inputs(coins, runs)


def test_rank_dates_only_reorder_known_slots_and_preserve_inputs():
    rows = [
        {"id": 8, "rarity": "SR", "release_date": "2026-01-01"},
        {"id": 7, "rarity": "SSR", "release_date": "2024-01-01"},
        {"id": 6, "rarity": "SSR", "release_date": None},
        {"id": 5, "rarity": "SSR", "release_date": "2025-01-01"},
        {"id": 4, "rarity": "SSR", "release_date": "2025-01-01"},
        {"id": 3, "rarity": "N"}, {"id": 2, "rarity": "R"},
    ]
    original = deepcopy(rows)
    ordered = policy.rank_entries(rows, policy.CHARACTER_RARITIES)
    assert [r["id"] for r in ordered] == [5, 6, 4, 7, 8, 2, 3]
    assert [r["rank"] for r in ordered] == list(range(1, 8))
    assert rows == original
    assert policy.rank_entries(list(reversed(rows)), policy.CHARACTER_RARITIES) == ordered


@pytest.mark.parametrize("rows", [
    [{"id": 1, "rarity": "SSR"}, {"id": 1, "rarity": "SSR"}],
    [{"id": 1, "rarity": "UR"}],
    [{"id": 1, "rarity": "SSR", "release_date": "yesterday"}],
])
def test_invalid_rank_metadata_rejected(rows):
    with pytest.raises(ValueError):
        policy.rank_entries(rows, policy.CHARACTER_RARITIES)


@pytest.fixture
def catalog():
    return {"characters": [{"id": 1, "rank": 1}, {"id": 2, "rank": 2}, {"id": 3, "rank": 3}],
            "equipment": [{"id": 11, "rank": 1, "slot_type": "attack"},
                          {"id": 12, "rank": 2, "slot_type": "attack"},
                          {"id": 13, "rank": 3, "slot_type": "support"},
                          {"id": 14, "rank": 4, "slot_type": "defense"},
                          {"id": 15, "rank": 5, "slot_type": "attack"}]}


def member(rid, index, attack=15, defense=14, support=13):
    return {"id": rid, "screen_index": index, "equipment": {"attack": attack, "defense": defense, "support": support}}


def test_choose_preserves_candidate_ui_metadata(catalog):
    rows = [{"id": 2, "center": [100, 200]}, {"id": 1, "center": [300, 200]}]
    assert policy.choose_character(rows, catalog) is rows[1]
    assert policy.choose_equipment([15, 11, 12], catalog) == 11
    with pytest.raises(ValueError):
        policy.choose_character([1, 404], catalog)
    with pytest.raises(ValueError):
        policy.choose_equipment([], catalog)


def test_empty_category_takes_priority_over_higher_rank_upgrade(catalog):
    team = [member(1, 1), member(2, 0, support=None)]
    before = deepcopy(team)
    assert policy.choose_loot([11, 13], team, catalog) == {
        "equipment_id": 13, "character_id": 2, "reason": "fill_empty", "replaced_equipment_id": None}
    assert team == before


def test_empty_targets_use_character_priority_then_screen(catalog):
    team = [member(2, 0, attack=None), member(1, 1, attack=None)]
    assert policy.choose_loot([12, 11], team, catalog)["character_id"] == 1
    catalog["characters"][1]["rank"] = 1
    assert policy.choose_loot([12, 11], team, catalog)["character_id"] == 2


def test_upgrade_selects_best_candidate_and_worst_old_gear(catalog):
    team = [member(1, 1, attack=12), member(2, 0, attack=15)]
    assert policy.choose_loot([12, 11], team, catalog) == {
        "equipment_id": 11, "character_id": 2, "reason": "upgrade", "replaced_equipment_id": 15}


def test_equal_old_gear_breaks_tie_by_character_then_position(catalog):
    team = [member(2, 0), member(1, 1)]
    assert policy.choose_loot([11], team, catalog)["character_id"] == 1
    catalog["characters"][1]["rank"] = 1
    assert policy.choose_loot([11], team, catalog)["character_id"] == 2


def test_no_improvement_uses_current_top_left_not_draft_order(catalog):
    team = [member(1, 2, attack=11), member(2, 0, attack=11), member(3, 1, attack=11)]
    assert policy.choose_loot([15, 12, 13], team, catalog) == {
        "equipment_id": 12, "character_id": 2, "reason": "fallback_first", "replaced_equipment_id": 11}


@pytest.mark.parametrize("change", ["missing", "unknown", "wrong_slot", "duplicate_position", "duplicate_character"])
def test_uncertain_or_invalid_team_does_not_become_an_empty_slot(catalog, change):
    team = [member(1, 0), member(2, 1)]
    if change == "missing": del team[0]["equipment"]["attack"]
    if change == "unknown": team[0]["equipment"]["attack"] = 404
    if change == "wrong_slot": team[0]["equipment"]["attack"] = 13
    if change == "duplicate_position": team[1]["screen_index"] = 0
    if change == "duplicate_character": team[1]["id"] = 1
    with pytest.raises(ValueError):
        policy.choose_loot([11], team, catalog)


def test_production_catalog_counts_dates_and_internal_rarities():
    cat = policy.load_catalog(PLAN)
    assert len(cat["characters"]) == 99
    assert len(cat["equipment"]) == 176
    assert {row["rarity"] for row in cat["characters"]} == {"SSR", "SR", "R", "N"}
    for kind, order in (("characters", policy.CHARACTER_RARITIES), ("equipment", policy.EQUIPMENT_RARITIES)):
        assert policy.rank_entries(cat[kind], order) == cat[kind]
        assert len({row["id"] for row in cat[kind]}) == len(cat[kind])
    special = next(row for row in cat["equipment"] if row["id"] == 11800086)
    assert (special["name"], special["rarity"], special["slot_type"]) == ("乱斗变量模块", "UR", "attack")
    assert sum(bool(row["release_date"]) for row in cat["equipment"]) == 19
    dates = {row["name"]: row["release_date"] for row in cat["characters"] if row["release_date"]}
    assert dates == {"伊卡菈": "2025-07-15", "静流·逐夏": "2024-09-09"}


def test_every_promoted_asset_is_local_hash_valid_and_masks_match():
    cat = policy.load_catalog()
    for relative, metadata in cat["assets"].items():
        path = (PLAN / relative).resolve()
        assert path.is_relative_to((PLAN / "templates/eternal_scuffle").resolve())
        assert ".pytest_tmp" not in relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
        with Image.open(path) as image:
            assert list(image.size) == metadata["size"]
        if path.name.endswith("_mask.png"):
            with Image.open(path) as mask, Image.open(path.with_name(path.name.replace("_mask.png", ".png"))) as template:
                assert mask.mode == "L" and mask.size == template.size
                assert mask.getbbox() is not None
    for row in cat["characters"]:
        assert len(row["stand_templates"]) == len(row["stand_masks"]) == 8
        assert cat["assets"][row["name_template"]]["size"] == [138, 23]
    for row in cat["equipment"]:
        assert cat["assets"][row["template"]]["size"] == [200, 200]
        name_asset = cat["assets"][row["name_template"]]
        assert name_asset["mode"] == "L"
        assert name_asset["size"] in ([138, 23], [164, 23])
        assert name_asset["source_font_sha256"]
        for profile in ("small_icon", "small_tips"):
            assert cat["assets"][row[profile + "_template"]]["size"] == [88, 88]
    assert "choose_role_phase" in cat["controls"] and "unopened_box" in cat["controls"]


def test_identical_equipment_images_have_explicit_name_disambiguation():
    cat = policy.load_catalog()
    rows = {r["id"]: r for r in cat["equipment"]}
    assert {tuple(group["ids"]) for group in cat["image_alias_groups"]} == {
        (11800053, 11800278), (11800009, 11800010), (11800057, 11800101)}
    for group in cat["image_alias_groups"]:
        names = set()
        images = set()
        for rid in group["ids"]:
            row = rows[rid]
            assert row["image_alias_ids"] == group["ids"]
            images.add(cat["assets"][row["template"]]["sha256"])
            names.add(cat["assets"][row["name_template"]]["sha256"])
        assert len(images) == 1 and len(names) == len(group["ids"])


def test_all_equipment_names_are_distinct_and_longest_keeps_full_font_size():
    cat = policy.load_catalog()
    hashes = {cat["assets"][row["name_template"]]["sha256"] for row in cat["equipment"]}
    assert len(hashes) == len(cat["equipment"]) == 176
    long_name = next(row for row in cat["equipment"] if row["id"] == 11800242)
    with Image.open(PLAN / long_name["name_template"]) as image:
        bounds = image.point(lambda value: 255 if value > 20 else 0).getbbox()
        assert image.size == (164, 23)
        assert bounds[2] - bounds[0] > 138  # No shrink-to-fit or truncated template.
        assert bounds[0] > 0 and bounds[2] < image.width
    assert cat["profiles"]["equipment_name"]["best_fit"] is False
