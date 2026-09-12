"""Offline policy and source-base catalog regression evidence.

Read-only inspection on 2026-09-12, using the existing devtools readers:
  D:/python_project/Aura_script_lsns_devtools/src/aura_resonance_devtools/
    binary_config.py:PooledBinaryConfig
    role_catalog.py:IndexedConfigReader
Game source (the installation has exactly one *_Data directory):
  D:/game/Resonance/*_Data/Patch/BinaryConfig/FoodFactory.bin
  SHA256 476f2e35f64a34cc7b2d16f944c0fbba86dafb30c1c930595c346d5de591ab39
  Per-record energy: field type 9, payload tag 0, signed int32 at field+9.
  All 11 IDs/names matched the existing catalog; reader reported no issues.
  Work meal 83300003 has base energy 30; love bases are pinned below.
  D:/game/Resonance/*_Data/Patch/Script/UIHomeFood/UIHomeFoodViewFunction.lua
  SHA256 a62c59f55b4e0eeae6fbc19b16748eb9e64304f2f3f37669d2b53675208ce9fe
  Static Lua bytecode inspection, function source lines 14-54, line 20:
    energyShow = info.ca.energy
        + PlayerData:GetHomeSkillIncrease(EnumDefine.HomeSkillEnum.RiseBentoEnergy)
  GETTABLE energy, CALL GetHomeSkillIncrease, ADD R4 R4 R5 precede the
  free/love branch. No game code was executed.

The user fixes work meal recovery at 36. No current-account skill bonus was
independently verified: DO NOT extrapolate a +6 bonus from this instruction or
the sample screenshots to love-bento catalog values. Those remain source bases.
The user subsequently requested direct base values: runtime must not OCR card
recovery or calibrate the account bonus. The current explicit-list child task
also does not read fatigue; all fatigue planning belongs to its caller.
Ratings are user policy (two one-star foods, all remaining known foods five-star),
not a claim about values extracted from the game.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections import UserDict
from pathlib import Path

import pytest

from plans.resonance_pc.src.actions import _bento_consumption_policy as policy
from plans.resonance_pc.src.actions._bento_consumption_policy import (
    WORK_MEAL_RECOVERY,
    meal_identity,
    resolve_meal,
    validate_inventory,
    validate_meals,
)


CATALOG_PATH = Path(__file__).resolve().parents[2] / "plans/resonance_pc/data/meta/love_bento.json"
SOURCE_BASES = {
    83300002: 20, 83300011: 22, 83300012: 35, 83300013: 35,
    83300014: 38, 83300015: 40, 83300016: 40, 83300017: 45,
    83300018: 42, 83300019: 50, 83300055: 42,
}


@pytest.fixture
def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def work(*available):
    return {"available_count": len(available), "slots": [
        {"issue_time": time, "available": time in available}
        for time in ("05:00", "12:00", "18:00")
    ]}


def love_row(catalog, food_id=83300014, role_id=10000025, days=2):
    food = next(food for food in catalog["items"] if food["id"] == food_id)
    return {"role_id": role_id, "role_name": "Observed Role", "food_id": food_id,
            "food_name": food["name"], "remaining_days": days}


def inventory(*rows, available=()):
    return {"work_meals": work(*available), "love_bentos": {"count": len(rows), "items": list(rows)}}


def work_request(time="05:00"):
    return {"kind": "work_meals", "issue_time": time}


def love_request(food_id=83300014, role_id=10000025, days=2):
    return {"kind": "love_bentos", "role_id": role_id, "food_id": food_id, "remaining_days": days}


def test_source_effects_and_ratings_are_complete(catalog):
    assert WORK_MEAL_RECOVERY == 36
    assert len(catalog["items"]) == len(SOURCE_BASES) == 11
    assert {food["id"] for food in catalog["items"]} == set(SOURCE_BASES)
    for food in catalog["items"]:
        assert type(food["fatigue_recovery"]) is int
        assert food["fatigue_recovery"] == SOURCE_BASES[food["id"]]
        assert type(food["rating_stars"]) is int
        assert food["rating_stars"] == (1 if food["id"] in (83300002, 83300011) else 5)


def test_planning_api_is_removed_without_compatibility():
    assert not hasattr(policy, "validate_inputs")
    assert not hasattr(policy, "select_next_meal")


def test_empty_requests_are_a_fresh_noop():
    meals = []
    validated = validate_meals(meals)
    assert validated == []
    assert validated is not meals
    assert validate_inventory([], None, None) == []


@pytest.mark.parametrize("bad", [None, True, False, 1, "work_meals", (), {}, iter(())])
def test_meals_must_be_a_list(bad):
    with pytest.raises(ValueError, match="list"):
        validate_meals(bad)


@pytest.mark.parametrize("bad", [None, True, 1, "work_meals", [], (), UserDict(work_request())])
def test_requests_must_be_dicts(bad):
    with pytest.raises(ValueError, match="dict"):
        validate_meals([bad])


@pytest.mark.parametrize("meal", [
    *(work_request(time) for time in ("05:00", "12:00", "18:00")),
    love_request(days=0), love_request(role_id=1, food_id=1, days=100),
])
def test_exact_requests_are_copied(meal):
    meals = [meal]
    validated = validate_meals(meals)
    assert validated == meals
    assert validated is not meals
    assert validated[0] is not meal
    validated[0]["kind"] = "changed"
    assert meals == [meal]
    assert meal["kind"] != "changed"


@pytest.mark.parametrize("factory", [work_request, love_request])
@pytest.mark.parametrize("extra", [
    "fatigue", "fatigue_recovery", "score", "rating_stars", "count", "max_count",
    "base_fatigue_reserve", "meal_priority", "priority", "floor", "budget",
    "role_name", "food_name",
])
def test_request_extra_keys_are_rejected(factory, extra):
    meal = factory()
    meal[extra] = 1
    with pytest.raises(ValueError, match="exactly"):
        validate_meals([meal])


@pytest.mark.parametrize("meal,key", [
    (work_request(), "kind"), (work_request(), "issue_time"),
    *((love_request(), key) for key in ("kind", "role_id", "food_id", "remaining_days")),
])
def test_missing_request_keys_are_rejected(meal, key):
    del meal[key]
    with pytest.raises(ValueError):
        validate_meals([meal])


@pytest.mark.parametrize("kind", [None, True, 1, [], {}, "", "unknown", "work_meal"])
def test_invalid_request_kind(kind):
    with pytest.raises(ValueError, match="kind"):
        validate_meals([{"kind": kind}])


@pytest.mark.parametrize("time", [None, True, 5, 5.0, [], {}, "5:00", "06:00", "05:00 "])
def test_invalid_work_request_time(time):
    with pytest.raises(ValueError, match="issue_time"):
        validate_meals([work_request(time)])


@pytest.mark.parametrize("field", ["role_id", "food_id", "remaining_days"])
@pytest.mark.parametrize("bad", [None, True, False, -1, 1.0, "1", [], {}])
def test_request_numbers_are_strict(field, bad):
    meal = love_request()
    meal[field] = bad
    with pytest.raises(ValueError, match=field):
        validate_meals([meal])


@pytest.mark.parametrize("field", ["role_id", "food_id"])
def test_request_ids_are_positive(field):
    meal = love_request()
    meal[field] = 0
    with pytest.raises(ValueError, match=field):
        validate_meals([meal])


@pytest.mark.parametrize("meal", [work_request(), love_request()])
def test_duplicate_request_identities_are_rejected(meal):
    with pytest.raises(ValueError, match="duplicate identity"):
        validate_meals([meal, dict(meal)])


def test_all_request_entries_are_validated_without_mutation():
    requests = [work_request(), love_request(), {"kind": "unknown"}]
    original = copy.deepcopy(requests)
    with pytest.raises(ValueError):
        validate_meals(requests)
    assert requests == original


@pytest.mark.parametrize("bad", [None, True, "work_meals", ("work_meals",), {}, [None],
                                  [[]], [1], ["work_meals", "work_meals"], ["unknown"]])
def test_invalid_inventory_kinds(bad):
    with pytest.raises(ValueError, match="kinds"):
        validate_inventory(bad, None, None)


@pytest.mark.parametrize("bad", [None, [], 1, True])
def test_inventory_must_be_an_object(bad):
    with pytest.raises(ValueError, match="inventory"):
        validate_inventory(["work_meals"], bad, None)


def test_known_empty_inventory(catalog):
    assert validate_inventory(["work_meals", "love_bentos"], inventory(), catalog) == []


def test_work_candidates_keep_observed_slot_order():
    data = inventory(available=("05:00", "18:00"))
    data["work_meals"]["slots"].reverse()
    assert validate_inventory(["work_meals"], data, None) == [
        {"kind": "work_meals", "issue_time": time, "fatigue_recovery": 36, "rating_stars": None}
        for time in ("18:00", "05:00")
    ]


@pytest.mark.parametrize("kinds", [["work_meals", "love_bentos"], ["love_bentos", "work_meals"]])
def test_candidates_keep_kind_and_observation_order(catalog, kinds):
    rows = [love_row(catalog, 83300019, 3, 9), love_row(catalog, 83300002, 1, 0),
            love_row(catalog, 83300014, 2, 1)]
    for permutation in itertools.permutations(rows):
        data = inventory(*permutation, available=("18:00",))
        by_kind = {
            "work_meals": [{**work_request("18:00"), "fatigue_recovery": 36, "rating_stars": None}],
            "love_bentos": [
                {**row, "kind": "love_bentos", "fatigue_recovery": SOURCE_BASES[row["food_id"]],
                 "rating_stars": 1 if row["food_id"] == 83300002 else 5}
                for row in permutation
            ],
        }
        assert validate_inventory(kinds, data, catalog) == [
            candidate for kind in kinds for candidate in by_kind[kind]
        ]


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
@pytest.mark.parametrize("bad", [None, [], {}, {"degraded": True}])
def test_unknown_or_degraded_requested_inventory_cannot_fall_through(catalog, kind, bad):
    data = inventory(love_row(catalog), available=("05:00",))
    data[kind] = bad
    with pytest.raises(ValueError):
        validate_inventory(["work_meals", "love_bentos"], data, catalog)
    del data[kind]
    with pytest.raises(ValueError):
        validate_inventory(["work_meals", "love_bentos"], data, catalog)


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
@pytest.mark.parametrize("bad", [1, "false", None])
def test_degraded_flag_type_is_strict(catalog, kind, bad):
    data = inventory()
    data[kind]["degraded"] = bad
    with pytest.raises(ValueError, match="degraded"):
        validate_inventory([kind], data, catalog)


@pytest.mark.parametrize("field,bad", [
    ("available_count", True), ("available_count", "1"), ("available_count", -1),
    ("available_count", 2), ("slots", None), ("slots", []), ("slots", [None]),
])
def test_invalid_work_inventory(field, bad):
    data = inventory(available=("05:00",))
    data["work_meals"][field] = bad
    with pytest.raises(ValueError):
        validate_inventory(["work_meals"], data, None)


@pytest.mark.parametrize("field,bad", [
    ("issue_time", "06:00"), ("issue_time", "12:00"), ("issue_time", None),
    ("available", 1), ("available", "false"), ("available", None),
])
def test_invalid_work_slot(field, bad):
    data = inventory(available=("05:00",))
    data["work_meals"]["slots"][0][field] = bad
    with pytest.raises(ValueError):
        validate_inventory(["work_meals"], data, None)


@pytest.mark.parametrize("field,bad", [
    ("count", True), ("count", "1"), ("count", -1), ("count", 0),
    ("items", None), ("items", []), ("items", [None]),
])
def test_invalid_love_inventory(catalog, field, bad):
    data = inventory(love_row(catalog))
    data["love_bentos"][field] = bad
    with pytest.raises(ValueError):
        validate_inventory(["love_bentos"], data, catalog)


@pytest.mark.parametrize("field,bad", [
    ("role_id", "1"), ("food_id", "83300014"), ("food_id", 99999999),
    ("role_id", True), ("role_id", 0), ("food_name", "wrong"), ("food_name", " padded "),
    ("role_name", ""), ("role_name", " padded "), ("role_name", 1),
    ("remaining_days", "1"), ("remaining_days", True), ("remaining_days", -1),
    ("remaining_days", 1.0),
])
def test_invalid_love_row(catalog, field, bad):
    row = love_row(catalog)
    row[field] = bad
    with pytest.raises(ValueError):
        validate_inventory(["love_bentos"], inventory(row), catalog)
    del row[field]
    with pytest.raises(ValueError):
        validate_inventory(["love_bentos"], inventory(row), catalog)


def test_duplicate_love_identity_is_rejected(catalog):
    row = love_row(catalog)
    with pytest.raises(ValueError, match="duplicate identity"):
        validate_inventory(["love_bentos"], inventory(row, dict(row)), catalog)


@pytest.mark.parametrize("field,bad", [
    ("fatigue_recovery", None), ("fatigue_recovery", "44"), ("fatigue_recovery", True),
    ("fatigue_recovery", 44.0), ("fatigue_recovery", 0), ("fatigue_recovery", -1),
    ("rating_stars", None), ("rating_stars", True), ("rating_stars", "5"), ("rating_stars", 2),
    ("id", "83300002"), ("id", True), ("name", ""), ("name", " padded "),
])
def test_catalog_values_are_strict_even_for_nonrequested_food(catalog, field, bad):
    data = inventory(love_row(catalog))
    catalog["items"][0][field] = bad
    with pytest.raises(ValueError):
        validate_inventory(["love_bentos"], data, catalog)
    del catalog["items"][0][field]
    with pytest.raises(ValueError):
        validate_inventory(["love_bentos"], data, catalog)


@pytest.mark.parametrize("bad", [None, [], {}, {"items": []}, {"items": [None]}])
def test_invalid_catalog(bad):
    with pytest.raises(ValueError):
        validate_inventory(["love_bentos"], inventory(), bad)


def test_duplicate_catalog_id(catalog):
    catalog["items"].append(copy.deepcopy(catalog["items"][0]))
    with pytest.raises(ValueError, match="duplicate food_id"):
        validate_inventory(["love_bentos"], inventory(), catalog)


@pytest.mark.parametrize("candidate", [
    None, {}, {"kind": "unknown"}, {"kind": "work_meals"}, {"kind": "love_bentos"},
])
def test_invalid_identity(candidate):
    with pytest.raises(ValueError):
        meal_identity(candidate)


def test_identity_ignores_names_effects_and_ui_state():
    assert meal_identity({**work_request(), "fatigue_recovery": 1, "position": [1, 2]}) == (
        "work_meals", "05:00",
    )
    assert meal_identity({**love_request(), "role_name": "Other", "rating_stars": 1}) == (
        "love_bentos", 10000025, 83300014, 2,
    )


@pytest.mark.parametrize("food_id", SOURCE_BASES)
def test_resolve_each_food_with_source_base_and_rating(catalog, food_id):
    row = love_row(catalog, food_id)
    candidate = resolve_meal(love_request(food_id), inventory(row), catalog)
    assert candidate == {
        **row, "kind": "love_bentos", "fatigue_recovery": SOURCE_BASES[food_id],
        "rating_stars": 1 if food_id in (83300002, 83300011) else 5,
    }


@pytest.mark.parametrize("time", ["05:00", "12:00", "18:00"])
def test_resolve_exact_work_slot(time):
    candidate = resolve_meal(work_request(time), inventory(available=("05:00", "12:00", "18:00")), None)
    assert candidate == {**work_request(time), "fatigue_recovery": 36, "rating_stars": None}


def test_explicit_request_order_is_independent_of_expiry_rating_recovery_and_inventory(catalog):
    requests = [love_request(83300019, 3, 9), work_request("18:00"),
                love_request(83300002, 1, 0), work_request("05:00"),
                love_request(83300019, 3, 0)]
    data = inventory(love_row(catalog, 83300002, 1, 0),
                     love_row(catalog, 83300019, 3, 0),
                     love_row(catalog, 83300019, 3, 9), available=("05:00", "18:00"))
    validated = validate_meals(requests)
    resolved = [resolve_meal(meal, data, catalog) for meal in validated]
    assert [meal_identity(candidate) for candidate in resolved] == [
        meal_identity(meal) for meal in requests
    ]


@pytest.mark.parametrize("meal", [
    work_request("12:00"), love_request(83300019), love_request(role_id=1),
    love_request(days=0), love_request(food_id=99999999),
])
def test_missing_identity_never_chooses_alternative(catalog, meal):
    data = inventory(love_row(catalog), available=("05:00", "18:00"))
    with pytest.raises(ValueError, match="must exist exactly once"):
        resolve_meal(meal, data, catalog)


@pytest.mark.parametrize("meal", [work_request(), love_request()])
def test_resolve_empty_live_inventory_is_an_explicit_error(catalog, meal):
    with pytest.raises(ValueError, match="must exist exactly once"):
        resolve_meal(meal, inventory(), catalog)


@pytest.mark.parametrize("meal", [
    {}, [], None, {**work_request(), "count": 1}, {**love_request(), "score": 5},
])
def test_resolve_validates_request_before_inventory(meal):
    with pytest.raises(ValueError) as error:
        resolve_meal(meal, None, None)
    assert "inventory" not in str(error.value)


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
def test_resolve_rejects_degraded_inventory(catalog, kind):
    data = inventory(love_row(catalog), available=("05:00",))
    data[kind]["degraded"] = True
    meal = work_request() if kind == "work_meals" else love_request()
    with pytest.raises(ValueError, match="degraded"):
        resolve_meal(meal, data, catalog)


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
def test_resolve_rejects_duplicate_identity(catalog, kind):
    row = love_row(catalog)
    data = inventory(row, available=("05:00",))
    if kind == "work_meals":
        data[kind]["slots"].append(dict(data[kind]["slots"][0]))
        meal = work_request()
    else:
        data[kind]["items"].append({**row, "role_name": "Different name"})
        data[kind]["count"] = 2
        meal = love_request()
    with pytest.raises(ValueError, match="duplicate"):
        resolve_meal(meal, data, catalog)


def test_resolve_rejects_food_name_mismatch(catalog):
    row = love_row(catalog)
    row["food_name"] = "wrong"
    with pytest.raises(ValueError, match="does not match catalog"):
        resolve_meal(love_request(), inventory(row), catalog)


def test_unrequested_kinds_and_catalog_are_not_required(catalog):
    data = {"work_meals": work("05:00"), "love_bentos": {"degraded": True}}
    candidate = resolve_meal(work_request(), data, None)
    assert validate_inventory(["work_meals"], data, None) == [candidate]
    data = {"love_bentos": {"count": 1, "items": [love_row(catalog)]}}
    candidate = resolve_meal(love_request(), data, catalog)
    assert validate_inventory(["love_bentos"], data, catalog) == [candidate]


def test_no_mutation_or_inventory_effect_override(catalog):
    row = love_row(catalog)
    row.update(fatigue_recovery=1, rating_stars=1)
    data = inventory(row, available=("05:00",))
    requests = [love_request(), work_request()]
    kinds = ["love_bentos", "work_meals"]
    original = copy.deepcopy((data, catalog, requests, kinds))
    validated = validate_meals(requests)
    candidates = validate_inventory(kinds, data, catalog)
    candidate = resolve_meal(validated[0], data, catalog)
    assert candidate["fatigue_recovery"] == 38
    assert candidate["rating_stars"] == 5
    candidate["role_name"] = "Changed return value"
    candidates[0]["food_name"] = "Changed candidate"
    candidates[1]["issue_time"] = "18:00"
    validated[0]["remaining_days"] = 99
    assert (data, catalog, requests, kinds) == original
