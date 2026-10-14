"""Confirmed city arrays share the existing atomic player cache."""
import copy
import json

import pytest

from packages.aura_core.context.persistence import persistent_data_service as persistence_module
from packages.aura_core.context.persistence.persistent_data_errors import PersistentDataError
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions._trade_goods_investment_records import InvestmentLevelRecords
from plans.resonance_pc.src.actions.player_data_pc_actions import _merge_latest


FIELD = "trade_goods_investment_levels"


@pytest.fixture
def persistence(tmp_path):
    return PersistentDataService(tmp_path)


def store(persistence, city="9", count=3):
    return InvestmentLevelRecords(persistence, city, count)


def test_missing_file_and_constructor_are_read_only(persistence):
    reader = store(persistence)
    assert reader.confirmed_level(1) is None
    assert not persistence.root.exists()


def test_actual_observations_restore_and_keep_unknown_positions(persistence):
    first = store(persistence)
    assert first.record(2, 13)
    document = persistence.read(file="user-info.json")
    assert document == {FIELD: {"9": [None, 13, None]}}
    second = store(persistence)
    assert second.confirmed_level(1) is None
    assert second.confirmed_level(2) == 13
    assert second.confirmed_level(2) >= 10
    assert second.confirmed_level(2) < 15
    second.record(1, 0)
    first.record(3, 20)
    assert second.confirmed_level(1) == 0
    assert second.confirmed_level(3) == 20
    assert list(persistence.root.rglob("*.json")) == [persistence.root / "user-info.json"]
    assert not list(persistence.root.rglob("*.tmp"))


def test_update_preserves_every_unrelated_field_and_other_city(persistence):
    original = {
        "status": {"cargo": 750, "fatigue": 10},
        "metadata": {"source": "ocr", "updated_at": "old"},
        "location": {"city": "淘金乐园"},
        "custom": {"anything": [True, "text", None]},
        FIELD: {"19": [13, 10], "2": [None, 20], "9": [10, None, 5]},
    }
    persistence.set(file="user-info.json", path=None, value=original)
    store(persistence).record(2, 13)
    updated = persistence.read(file="user-info.json")
    expected = copy.deepcopy(original)
    expected[FIELD]["9"] = [10, 13, 5]
    assert updated == expected
    assert list(updated[FIELD]) == ["2", "9", "19"]
    assert store(persistence, city="19", count=2).confirmed_level(1) == 13


def test_new_lower_actual_level_replaces_stale_higher_level(persistence):
    reader = store(persistence)
    reader.record(1, 13)
    reader.record(1, 10)
    assert store(persistence).confirmed_level(1) == 10


def test_no_service_keeps_only_task_local_observations():
    reader = store(None)
    assert reader.confirmed_level(1) is None
    assert reader.record(1, 13)
    assert reader.confirmed_level(1) == 13
    assert store(None).confirmed_level(1) is None


@pytest.mark.parametrize("level", [True, False, -1, 21, "10", 10.0, None, float("nan"), float("inf")])
def test_invalid_actual_level_never_writes(persistence, level):
    with pytest.raises(ValueError, match="Confirmed actual"):
        store(persistence).record(1, level)
    assert not persistence.root.exists()


@pytest.mark.parametrize("index", [True, False, 0, -1, 4, 1.0, "1", None])
def test_invalid_position_never_reads_or_writes(persistence, index):
    reader = store(persistence)
    with pytest.raises(ValueError, match="one-based"):
        reader.confirmed_level(index)
    with pytest.raises(ValueError, match="one-based"):
        reader.record(index, 10)
    assert not persistence.root.exists()


@pytest.mark.parametrize("city", [True, False, "", "city", "../9", -1, 0, 9.0, None])
def test_invalid_city_is_rejected(persistence, city):
    with pytest.raises(ValueError, match="city_id"):
        store(persistence, city=city)


@pytest.mark.parametrize("count", [True, False, 0, -1, 3.0, "3", None])
def test_invalid_count_is_rejected(persistence, count):
    with pytest.raises(ValueError, match="product_count"):
        store(persistence, count=count)


@pytest.mark.parametrize("value", [True, False, -1, 21, "10", 10.0])
def test_bad_slot_is_unknown_and_valid_other_slots_remain(persistence, value):
    path = persistence.root / "user-info.json"
    path.parent.mkdir()
    path.write_text(json.dumps({FIELD: {"9": [10, value, None]}}), encoding="utf-8")
    reader = store(persistence)
    assert reader.confirmed_level(1) == 10
    assert reader.confirmed_level(2) is None
    assert reader.confirmed_level(3) is None


@pytest.mark.parametrize("value", [True, 10, "text", {}, [10], [10, 13, 15, 20]])
def test_invalid_city_array_or_length_is_never_reused(persistence, value):
    persistence.set(file="user-info.json", path=None, value={FIELD: {"9": value}})
    assert store(persistence).confirmed_level(1) is None
    store(persistence).record(2, 13)
    assert persistence.read(file="user-info.json")[FIELD]["9"] == [None, 13, None]


@pytest.mark.parametrize("value", [True, "text", [10], 20])
def test_invalid_top_mapping_is_not_used(persistence, value):
    persistence.set(file="user-info.json", path=None, value={FIELD: value, "status": {"cargo": 750}})
    assert store(persistence).confirmed_level(1) is None
    store(persistence).record(1, 10)
    assert persistence.read(file="user-info.json") == {FIELD: {"9": [10, None, None]}, "status": {"cargo": 750}}


@pytest.mark.parametrize("raw", ["{bad", "[]", '"text"', '{"trade_goods_investment_levels":{"9":[10,NaN,null]}}', '{"trade_goods_investment_levels":{"9":[10,Infinity,null]}}'])
def test_whole_file_corruption_fails_explicitly_and_is_not_overwritten(persistence, raw):
    path = persistence.root / "user-info.json"
    path.parent.mkdir()
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(PersistentDataError):
        store(persistence).confirmed_level(1)
    with pytest.raises(PersistentDataError):
        store(persistence).record(1, 10)
    assert path.read_text(encoding="utf-8") == raw


def test_player_refresh_merge_retains_confirmed_city_levels(persistence):
    store(persistence).record(1, 10)
    existing = persistence.read(file="user-info.json")
    fresh = {"location": {"city": "修格里城"}, "status": {"cargo": 800}}
    persistence.update(file="user-info.json", updater=lambda document: _merge_latest(
        document, fresh, section_updated_at={"location": "now", "profile": "now"}, updated_at="now"))
    updated = persistence.read(file="user-info.json")
    assert updated[FIELD] == existing[FIELD]
    assert updated["status"]["cargo"] == 800
    assert updated["location"] == fresh["location"]


def test_failed_atomic_replace_keeps_original_cache(persistence, monkeypatch):
    reader = store(persistence)
    reader.record(1, 10)
    path = persistence.root / "user-info.json"
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(persistence_module.os, "replace", fail_replace)
    with pytest.raises(PersistentDataError):
        reader.record(2, 13)
    assert path.read_bytes() == original
    assert not list(persistence.root.rglob("*.tmp"))


@pytest.mark.parametrize("invalid,actual", [(False, 0), (True, 1), (10.0, 10)])
def test_equal_but_invalid_slot_is_never_claimed_persisted(persistence, invalid, actual):
    path = persistence.root / "user-info.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"status": {"cargo": 750}, FIELD: {"9": [invalid, None, None]}}), encoding="utf-8")
    original = path.read_bytes()
    reader = store(persistence)
    assert reader.confirmed_level(1) is None
    assert reader.record(1, actual) is False
    assert reader.confirmed_level(1) == actual  # Confirmed for this task only.
    assert store(persistence).confirmed_level(1) is None
    assert path.read_bytes() == original
