"""Buy-lot coercion, domain errors, and loader ownership remain distinct."""

import copy
import json
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.services._buy_lot_normalization import (
    normalize_city_product_buy_lot,
)
from plans.resonance_pc.src.services.resonance_pc_market_data_service import (
    ResonancePcMarketDataError,
    ResonancePcMarketDataService,
)
from plans.resonance_pc.src.services.resonance_pc_trade_planner_service import (
    ResonancePcTradePlannerError,
    ResonancePcTradePlannerService,
)


ERROR_TYPES = [ResonancePcMarketDataError, ResonancePcTradePlannerError]


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_normalization_keeps_coercions_skips_and_independent_rows(error_type):
    raw = {
        " not-a-row ": [1],
        " ": {"bad": "invalid"},
        7: {" ": "invalid", " bool ": True, "false": False,
            "float": 3.9, "negative_fraction": -0.9, "string": " +12 "},
        "empty": {},
        None: {False: 0, 2: 5},
    }
    before = copy.deepcopy(raw)
    result = normalize_city_product_buy_lot(raw, error_factory=error_type)
    assert result == {
        "7": {"bool": 1, "false": 0, "float": 3, "negative_fraction": 0, "string": 12},
        "empty": {}, "None": {"False": 0, "2": 5},
    }
    assert raw == before
    result["7"]["float"] = 99
    assert raw[7]["float"] == 3.9


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_trim_collisions_replace_whole_city_rows_without_reordering(error_type):
    raw = {
        " 1 ": {"old": 9},
        "2": {" p ": 1, "q": 2, "p": 3},
        "1": {"new": 4},
        " 2 ": "skip this row rather than remove the existing row",
    }
    result = normalize_city_product_buy_lot(raw, error_factory=error_type)
    assert list(result) == ["1", "2"]
    assert result == {"1": {"new": 4}, "2": {"p": 3, "q": 2}}
    assert list(result["2"]) == ["p", "q"]


@pytest.mark.parametrize("error_type", ERROR_TYPES)
@pytest.mark.parametrize("raw", [None])
def test_invalid_table_keeps_domain_error_and_message(error_type, raw):
    with pytest.raises(error_type) as caught:
        normalize_city_product_buy_lot(raw, error_factory=error_type)
    assert caught.value.to_dict() == {
        "code": "buy_lot_invalid",
        "message": "buy_lot payload must include object field city_product_buy_lot.",
        "detail": {},
    }
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("error_type", ERROR_TYPES)
@pytest.mark.parametrize("value,cause_type", [
    (None, TypeError), ("1.5", ValueError),
])
def test_conversion_failures_keep_trimmed_identity_and_cause(error_type, value, cause_type):
    with pytest.raises(error_type) as caught:
        normalize_city_product_buy_lot({" city ": {" product ": value}}, error_factory=error_type)
    assert caught.value.code == "buy_lot_invalid"
    assert caught.value.message == "buy_lot 'city->product' must be integer."
    assert caught.value.detail == {}
    assert type(caught.value.__cause__) is cause_type
    with pytest.raises(cause_type) as original:
        int(value)
    assert str(caught.value.__cause__) == str(original.value)


@pytest.mark.parametrize("error_type", ERROR_TYPES)
@pytest.mark.parametrize("value", [" -2 "])
def test_negative_result_keeps_unchained_domain_error(error_type, value):
    with pytest.raises(error_type) as caught:
        normalize_city_product_buy_lot({" c ": {" p ": value}}, error_factory=error_type)
    assert caught.value.to_dict() == {
        "code": "buy_lot_invalid", "message": "buy_lot 'c->p' must be >= 0.", "detail": {},
    }
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_overflow_remains_unwrapped(error_type):
    with pytest.raises(OverflowError) as caught:
        normalize_city_product_buy_lot({"c": {"p": float("inf")}}, error_factory=error_type)
    assert caught.value.__cause__ is None


def _loaders(tmp_path):
    path = tmp_path / "buy_lot.json"
    planner = ResonancePcTradePlannerService(SimpleNamespace(), plan_root=tmp_path)
    planner.buy_lot_file = path
    # Loading metadata needs no market service initialization or cache directories.
    market = ResonancePcMarketDataService.__new__(ResonancePcMarketDataService)
    market.buy_lot_file = path
    return path, planner, market


def _write(path, **updates):
    payload = {"schema_version": " 1.0.0 ", "city_product_buy_lot": {" 1 ": {" p ": 2.9}}}
    payload.update(updates)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_planner_owns_identity_cache_while_market_rereads_and_keeps_metadata(tmp_path):
    path, planner, market = _loaders(tmp_path)
    _write(path, source={"route": "native"}, default_lot=" 4 ", ignored="metadata")
    planned = planner._load_buy_lot_payload()
    assert planned == {"schema_version": "1.0.0", "city_product_buy_lot": {"1": {"p": 2}}}
    loaded = market.load_buy_lot_payload()
    assert loaded == {**planned, "source": {"route": "native"}, "default_lot": 4}
    _write(path, city_product_buy_lot={"2": {"q": 5}}, source="changed", default_lot=-3)
    assert planner._load_buy_lot_payload() is planned
    assert market.load_buy_lot_payload() == {
        "schema_version": "1.0.0", "city_product_buy_lot": {"2": {"q": 5}},
        "source": "changed", "default_lot": -3,
    }
    path.unlink()
    assert planner._load_buy_lot_payload() is planned
    with pytest.raises(ResonancePcMarketDataError) as caught:
        market.load_buy_lot_payload()
    assert caught.value.code == "buy_lot_missing"


@pytest.mark.parametrize("kind,error_type", [("planner", ResonancePcTradePlannerError),
                                             ("market", ResonancePcMarketDataError)])
@pytest.mark.parametrize("payload,message", [
    ([], "buy_lot payload must be an object."),
    ({"schema_version": "unsupported"}, "Unsupported buy_lot schema_version 'unsupported', expected '1.0.0'."),
    ({"schema_version": "1.0.0"}, "buy_lot payload must include object field city_product_buy_lot."),
    ({"schema_version": "1.0.0", "city_product_buy_lot": {"1": {"p": -2}}},
     "buy_lot '1->p' must be >= 0."),
])
def test_loader_validation_order_and_no_cache_on_failure(tmp_path, kind, error_type, payload, message):
    path, planner, market = _loaders(tmp_path)
    path.write_text(json.dumps(payload), encoding="utf-8")
    load = planner._load_buy_lot_payload if kind == "planner" else market.load_buy_lot_payload
    with pytest.raises(error_type) as caught:
        load()
    assert caught.value.code == "buy_lot_invalid"
    assert caught.value.message == message
    assert planner._buy_lot_payload is None
    _write(path)
    assert load()["city_product_buy_lot"] == {"1": {"p": 2}}


def test_missing_file_messages_remain_loader_specific(tmp_path):
    path, planner, market = _loaders(tmp_path)
    for load, error_type, message in [
        (planner._load_buy_lot_payload, ResonancePcTradePlannerError, f"buy_lot metadata file not found: {path}"),
        (market.load_buy_lot_payload, ResonancePcMarketDataError, f"buy_lot file not found: {path}"),
    ]:
        with pytest.raises(error_type) as caught:
            load()
        assert caught.value.code == "buy_lot_missing"
        assert caught.value.message == message


def test_malformed_json_remains_a_json_error_in_both_loaders(tmp_path):
    path, planner, market = _loaders(tmp_path)
    path.write_text("{", encoding="utf-8")
    for load in [planner._load_buy_lot_payload, market.load_buy_lot_payload]:
        with pytest.raises(json.JSONDecodeError):
            load()
    assert planner._buy_lot_payload is None


@pytest.mark.parametrize("metadata,expected", [({}, {"source": None, "default_lot": 0}),
    ({"source": ["route"], "default_lot": 2.9}, {"source": ["route"], "default_lot": 2}),
    ({"default_lot": None}, {"source": None, "default_lot": 0})])
def test_market_source_and_default_metadata_remain_outside_normalizer(tmp_path, metadata, expected):
    path, planner, market = _loaders(tmp_path)
    _write(path, **metadata)
    assert market.load_buy_lot_payload() == {
        "schema_version": "1.0.0", "city_product_buy_lot": {"1": {"p": 2}}, **expected,
    }
    assert set(planner._load_buy_lot_payload()) == {"schema_version", "city_product_buy_lot"}


def test_market_extractor_uses_its_own_error_and_does_not_mutate_input(tmp_path):
    _, _, market = _loaders(tmp_path)
    payload = {"city_product_buy_lot": {" 1 ": {" p ": "3"}}}
    assert market._extract_city_product_buy_lot(payload) == {"1": {"p": 3}}
    assert payload == {"city_product_buy_lot": {" 1 ": {" p ": "3"}}}
    with pytest.raises(ResonancePcMarketDataError) as caught:
        market._extract_city_product_buy_lot({"city_product_buy_lot": {"1": {"p": []}}})
    assert type(caught.value.__cause__) is TypeError
