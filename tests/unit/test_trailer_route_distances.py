"""Offline checks for native rail distances, separate from freight fatigue."""

from hashlib import sha256
import json
import math
from pathlib import Path

import pytest

from tools.build_trailer_route_distances import (
    PLAN_TEXT_HASH_FORMAT, display_distance, field_records, plan_text_sha256,
    rail_graph, reference_ids, shortest_distances,
)


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
CATALOG = json.loads((PLAN / "data/meta/trailer_route_distances.json").read_text(encoding="utf-8"))


def test_catalog_covers_canonical_market_cities_with_symmetric_integer_km():
    expected = json.loads((PLAN / "data/meta/cities.json").read_text(encoding="utf-8"))
    assert CATALOG["schema_version"] == 1
    assert CATALOG["cities"] == expected
    assert set(expected) == {str(index) for index in range(1, 22)}
    distances = CATALOG["distances_km"]
    assert set(distances) == set(expected)
    for start in expected:
        assert set(distances[start]) == set(expected)
        for end in expected:
            km = distances[start][end]
            assert type(km) is int
            assert km >= 0
            assert (km == 0) is (start == end)
            assert km == distances[end][start]


def test_every_city_distance_matches_independent_floyd_warshall_over_native_edges():
    nodes = list(map(int, CATALOG["native_stations"]))
    raw = {start: {end: 0. if start == end else math.inf for end in nodes} for start in nodes}
    for edge in CATALOG["native_edges"]:
        start, end = edge["station01"], edge["station02"]
        raw[start][end] = raw[end][start] = edge["distance"]
    for via in nodes:
        for start in nodes:
            for end in nodes:
                raw[start][end] = min(raw[start][end], raw[start][via] + raw[via][end])
    ratio = CATALOG["source"]["dis_ratio"]
    for city_id, station_id in CATALOG["native_city_ids"].items():
        for other_id, other_station in CATALOG["native_city_ids"].items():
            assert math.isfinite(raw[station_id][other_station])
            assert CATALOG["distances_km"][city_id][other_id] == math.ceil(raw[station_id][other_station] * ratio)


def test_non_trading_transit_stations_remain_in_rail_path():
    graph = rail_graph(CATALOG["native_edges"])
    mapped_stations = set(CATALOG["native_city_ids"].values())
    assert 83000027 not in mapped_stations
    assert CATALOG["native_stations"]["83000027"] == "隐岩站"
    assert 83000018 not in graph[83000017]
    assert graph[83000017][83000027] == pytest.approx(5178.221)
    assert graph[83000027][83000018] == pytest.approx(9547.952)
    assert CATALOG["distances_km"]["12"]["16"] == 295


def test_native_distance_matches_current_static_rail_data_without_faking_old_screenshot():
    distances = CATALOG["distances_km"]
    assert distances["1"]["2"] == 65
    assert distances["2"]["7"] == 75
    assert distances["1"]["7"] == 140
    evidence = CATALOG["source"]["screenshot_cross_check"]
    assert evidence["screenshot_displayed_km"] == 136
    assert evidence["current_static_km"] == 140
    assert evidence["difference_km"] == 4
    assert evidence["status"] == "mismatch_not_calibrated"
    assert evidence["attachment"].startswith("codex-clipboard-")
    assert len(evidence["attachment_sha256"]) == 64


def test_catalog_records_reproducible_native_and_mapping_provenance():
    source = CATALOG["source"]
    assert source["kind"] == "native_game_config"
    assert source["distance_kind"] == "static_city_to_city_displayed_rail_km"
    assert source["dis_ratio"] == .02
    assert set(source["sha256"]) == {"Patch/BinaryConfig/ConfigFactory.bin",
                                     "Patch/BinaryConfig/HomeLineFactory.bin",
                                     "Patch/BinaryConfig/HomeStationFactory.bin"}
    assert set(source["scripts"]) == {"Patch/Script/UIHome/UIHomeMapDataModel.lua",
                                      "Patch/Script/UIHome/UIHomeMapController.lua"}
    assert all(len(value) == 64 for value in source["sha256"].values())
    assert all(len(value["sha256"]) == 64 for value in source["scripts"].values())
    assert source["plan_sha256_format"] == PLAN_TEXT_HASH_FORMAT == "utf8-crlf-to-lf-v1"
    for relative, digest in source["plan_sha256"].items():
        assert plan_text_sha256(PLAN / relative) == digest
    investment = json.loads((PLAN / "data/meta/trade_goods_investment_cities.json").read_text(encoding="utf-8"))
    assert CATALOG["native_city_ids"] == {city_id: row["native_station_id"]
                                            for city_id, row in investment["cities"].items()}


def test_display_rounding_keeps_equal_displayed_km_equal():
    assert display_distance(5000, .02) == 100
    assert display_distance(5000.001, .02) == 101
    assert display_distance(5049.999, .02) == 101
    assert display_distance(0, .02) == 0


def test_shortest_path_uses_rail_edges_not_direct_map_geometry():
    graph = rail_graph([{"station01": 1, "station02": 2, "distance": 100},
                        {"station01": 2, "station02": 3, "distance": 50},
                        {"station01": 1, "station02": 3, "distance": 200}])
    assert shortest_distances(graph, 1) == {1: 0., 2: 100., 3: 150.}
    assert shortest_distances(graph, 3)[1] == 150
    with pytest.raises(ValueError, match="absent"):
        shortest_distances(graph, 99)


@pytest.mark.parametrize(("raw", "ratio"), [(-1, .02), (math.inf, .02), (0, 0),
                                            (True, .02), (1, math.nan)])
def test_invalid_distances_and_scale_are_rejected(raw, ratio):
    with pytest.raises(ValueError, match="finite"):
        display_distance(raw, ratio)


@pytest.mark.parametrize("edge", [
    {"station01": 1, "station02": 1, "distance": 20},
    {"station01": True, "station02": 2, "distance": 20},
    {"station01": 1, "station02": 2, "distance": 0},
    {"station01": 1, "station02": 2, "distance": math.nan},
])
def test_invalid_native_edges_are_rejected(edge):
    with pytest.raises(ValueError, match="rail edges"):
        rail_graph([edge])


def test_native_reference_order_and_informal_filter_are_preserved():
    references = [[{"name": "id", "value": 7}], [{"name": "id", "value": 8}],
                  [{"name": "id", "value": 7}]]
    assert reference_ids(references) == [7, 8, 7]
    records = {"records": [
        {"fields": [{"name": "id", "value": 7}, {"name": "isInformalData", "value": False}]},
        {"fields": [{"name": "id", "value": 8}, {"name": "isInformalData", "value": True}]},
    ]}
    assert list(field_records(records)) == [7]
    with pytest.raises(ValueError, match="invalid reference"):
        reference_ids([[{"name": "id", "value": "7"}]])
    with pytest.raises(ValueError, match="unique"):
        field_records({"records": [records["records"][0], records["records"][0]]})


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_plan_source_provenance_is_independent_of_checkout_newlines(tmp_path, newline):
    for relative, digest in CATALOG["source"]["plan_sha256"].items():
        lf = (PLAN / relative).read_bytes().replace(b"\r\n", b"\n")
        checkout = tmp_path / Path(relative).name
        checkout.write_bytes(lf.replace(b"\n", newline))
        assert plan_text_sha256(checkout) == digest == sha256(lf).hexdigest()


def test_plan_source_hash_preserves_content_and_formatting_bytes(tmp_path):
    path = tmp_path / "source.json"
    raw = '{"城市": "修格里城"}\n'.encode("utf-8")
    path.write_bytes(raw)
    expected = sha256(raw).hexdigest()
    assert plan_text_sha256(path) == expected
    for changed in (raw.rstrip(b"\n"), b"\xef\xbb\xbf" + raw,
                    raw.replace(b" ", b"  "), raw.replace("修格里城".encode(), "铁盟哨站".encode())):
        path.write_bytes(changed)
        assert plan_text_sha256(path) != expected
