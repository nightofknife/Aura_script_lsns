from __future__ import annotations

import csv
import copy
import json
from pathlib import Path

import pytest

from plans.resonance.src.services.resonance_market_data_service import (
    ResonanceMarketDataError,
    ResonanceMarketDataService,
)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_default_fatigue_payload(cities: dict[str, str]) -> dict:
    city_ids = sorted(cities.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))
    costs = {}
    for from_id in city_ids:
        row = {}
        for to_id in city_ids:
            row[to_id] = 0 if from_id == to_id else abs(int(from_id) - int(to_id)) + 10
        costs[from_id] = row
    return {
        "schema_version": "1.0.0",
        "cities": {str(cid): cities[str(cid)] for cid in city_ids},
        "costs": costs,
    }


def _build_service(
    tmp_path: Path,
    cities: dict[str, str] | None = None,
    products: dict[str, str] | None = None,
    fatigue_payload: dict | None = None,
    city_aliases: dict[str, str] | None = None,
):
    plan_root = tmp_path / "resonance"
    cities_payload = cities or {}
    products_payload = products or {}
    _write_json(plan_root / "data" / "meta" / "cities.json", cities_payload)
    _write_json(plan_root / "data" / "meta" / "products.json", products_payload)
    _write_json(plan_root / "data" / "meta" / "city_aliases.json", city_aliases or {})
    default_buy_lot = {
        "schema_version": "1.0.0",
        "default_lot": 0,
        "city_product_buy_lot": {str(cid): {} for cid in cities_payload.keys()},
    }
    _write_json(plan_root / "data" / "meta" / "buy_lot.json", default_buy_lot)
    if fatigue_payload is None:
        fatigue_payload = _build_default_fatigue_payload(cities_payload)
    _write_json(plan_root / "data" / "meta" / "city_travel_fatigue.json", fatigue_payload)
    return ResonanceMarketDataService(plan_root=plan_root)


def test_normalize_trend_scope_and_indexes(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "City A", "2": "City B", "3": "City C"},
        products={"1": "Item A", "2": "Item B", "3": "Item C"},
    )
    raw = {
        "data": {
            "1": {
                "s": {"1": {"t": 1, "v": 12, "ti": 1700000000, "p": 100}},
                "b": {"1": {"t": 0, "v": 3, "ti": 1700000001, "p": 95}},
            },
            "2": {
                "s": {"2": {"t": 0, "v": 8, "ti": 1700000002, "p": 250}},
                "b": {
                    "1": {"t": 1, "v": 2, "ti": 1700000003, "p": 200},
                    "2": {"t": 0, "v": 5, "ti": 1700000004, "p": 210},
                },
            },
            "3": {
                "s": {"3": {"t": 0, "v": 1, "ti": 1700000005, "p": 300}},
            },
        }
    }

    snapshot = service.normalize(raw)
    assert len(snapshot["products"]) == 3

    product_1 = snapshot["products"]["1"]
    assert product_1["market"]["sell"]["1"]["trend"] == "up"
    assert product_1["market"]["buy"]["1"]["trend"] == "down"
    assert product_1["classification"]["scope"] == "unique"

    product_2 = snapshot["products"]["2"]
    assert product_2["classification"]["scope"] == "shared"
    assert product_2["classification"]["buy_city_count"] == 2

    product_3 = snapshot["products"]["3"]
    assert product_3["classification"]["scope"] == "non_buy"

    indexes = snapshot["indexes"]
    assert indexes["unique_products"] == ["1"]
    assert indexes["shared_products"] == ["2"]
    assert indexes["non_buy_products"] == ["3"]
    assert "1" in indexes["city_to_buy_products"]
    assert "3" in indexes["city_to_sell_products"]


def test_extract_buy_lot_pairs_from_chunk_text():
    chunk = (
        'abc {name:"发动机",buyPrices:{修格里城:3363},buyLot:{修格里城:9},sellPrices:{汇流塔:4170}} '
        '{name:"弹丸加速装置",buyLot:{修格里城:18,铁盟哨站:18},sellPrices:{汇流塔:2920}} '
        '{name:"手工编织地毯",buyLot:{},sellPrices:{汇流塔:2074}} xyz'
    )
    pairs = ResonanceMarketDataService._extract_product_buy_lot_pairs(chunk)
    assert pairs["发动机"] == {"修格里城": 9}
    assert pairs["弹丸加速装置"] == {"修格里城": 18, "铁盟哨站": 18}
    assert pairs["手工编织地毯"] == {}


def test_refresh_includes_buy_lot_from_web_source(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "修格里城", "2": "铁盟哨站"},
        products={"3": "发动机"},
    )
    raw = {
        "data": {
            "3": {
                "s": {"1": {"t": 1, "v": 0, "ti": 1700000000, "p": 100}},
                "b": {"1": {"t": 0, "v": 0, "ti": 1700000000, "p": 90}},
            }
        }
    }

    service.fetch_raw = lambda: copy.deepcopy(raw)  # type: ignore[method-assign]
    service.fetch_buy_lot_payload = lambda: {  # type: ignore[method-assign]
        "schema_version": "1.0.0",
        "default_lot": 0,
        "city_product_buy_lot": {"1": {"3": 9}, "2": {}},
    }
    snapshot = service.refresh(force=True)

    assert snapshot["products"]["3"]["buy_lot"] == {"1": 9}
    persisted = json.loads((service.buy_lot_file).read_text(encoding="utf-8"))
    assert persisted["city_product_buy_lot"]["1"]["3"] == 9


def test_extract_route_constants_from_chunk_text(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "Old A", "2": "Old B"},
        products={"1": "Item A"},
    )
    chunk = (
        'x={CITIES:()=>h,CITY_FATIGUES:()=>f};'
        'var h=["城市甲","城市乙","城市丙"],'
        'f=[{cities:["城市甲","城市乙"],fatigue:11},{cities:["城市乙","城市丙"],fatigue:13},{cities:["城市甲","城市丙"],fatigue:17}];'
    )
    parsed = service._extract_route_constants_from_chunk(chunk)
    assert parsed is not None
    assert parsed["cities"] == ["城市甲", "城市乙", "城市丙"]
    assert len(parsed["fatigue_edges"]) == 3
    assert parsed["fatigue_edges"][0]["fatigue"] == 11


def test_sync_web_constants_updates_local_meta_files(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "Old A", "2": "Old B"},
        products={"1": "Item A"},
    )
    constants_payload = {
        "route_chunk": "https://example.com/chunk.js",
        "cities": {"1": "城A", "2": "城B", "3": "城C"},
        "travel_fatigue": {
            "schema_version": "1.0.0",
            "cities": {"1": "城A", "2": "城B", "3": "城C"},
            "costs": {
                "1": {"1": 0, "2": 11, "3": 13},
                "2": {"1": 11, "2": 0, "3": 17},
                "3": {"1": 13, "2": 17, "3": 0},
            },
        },
    }
    service.fetch_route_constants_payload = lambda: copy.deepcopy(constants_payload)  # type: ignore[method-assign]

    result = service.sync_web_constants(sync_buy_lot=False)

    assert result["status"] == "ok"
    assert result["cities_count"] == 3
    assert result["fatigue_edge_count"] == 3
    cities_file = json.loads((service.meta_dir / "cities.json").read_text(encoding="utf-8"))
    assert cities_file == constants_payload["cities"]
    fatigue_csv = (service.meta_dir / "city_travel_fatigue.csv").read_text(encoding="utf-8")
    assert "from_city_id,from_city_name,to_city_id,to_city_name,fatigue" in fatigue_csv
    assert "1,城A,2,城B,11" in fatigue_csv


def test_sync_web_constants_with_buy_lot_summary(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "Old A", "2": "Old B"},
        products={"3": "发动机"},
    )
    constants_payload = {
        "route_chunk": "https://example.com/chunk.js",
        "cities": {"1": "城A", "2": "城B"},
        "travel_fatigue": {
            "schema_version": "1.0.0",
            "cities": {"1": "城A", "2": "城B"},
            "costs": {"1": {"1": 0, "2": 11}, "2": {"1": 11, "2": 0}},
        },
    }
    buy_lot_payload = {
        "schema_version": "1.0.0",
        "city_product_buy_lot": {"1": {"3": 9}, "2": {}},
        "unknown_city_names": [],
        "remapped_city_names": [{"from_city_name": "沃德镇", "to_city_id": "1", "to_city_name": "城A"}],
    }
    service.fetch_route_constants_payload = lambda: copy.deepcopy(constants_payload)  # type: ignore[method-assign]
    service.resolve_buy_lot_payload = lambda: copy.deepcopy(buy_lot_payload)  # type: ignore[method-assign]

    result = service.sync_web_constants(sync_buy_lot=True)

    assert result["buy_lot_sync"]["mapped_pairs"] == 1
    assert result["buy_lot_sync"]["unknown_city_names"] == []
    assert result["buy_lot_sync"]["remapped_city_names"][0]["from_city_name"] == "沃德镇"


def test_fetch_buy_lot_payload_applies_city_alias(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"12": "云岫桥基地"},
        products={"119": "羽绒服"},
        city_aliases={"沃德镇": "云岫桥基地"},
    )

    route_html = '<html><script src="/_next/static/chunks/test-route.js"></script></html>'
    chunk_text = '{name:"羽绒服",buyLot:{沃德镇:17},buyPrices:{云岫桥基地:891}}'

    def _mock_http_get_text(url: str) -> str:
        if url.endswith("/route"):
            return route_html
        if url.endswith("/_next/static/chunks/test-route.js"):
            return chunk_text
        raise AssertionError(f"unexpected url: {url}")

    service._http_get_text = _mock_http_get_text  # type: ignore[method-assign]
    payload = service.fetch_buy_lot_payload()

    assert payload["city_product_buy_lot"]["12"]["119"] == 17
    assert payload["remapped_city_names"][0]["from_city_name"] == "沃德镇"
    assert payload["remapped_city_names"][0]["to_city_name"] == "云岫桥基地"


def test_refresh_cache_and_dedupe(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "City A"},
        products={"1": "Item A"},
    )
    raw = {
        "data": {
            "1": {
                "s": {"1": {"t": 1, "v": 0, "ti": 1700000000, "p": 100}},
                "b": {"1": {"t": 0, "v": 0, "ti": 1700000000, "p": 90}},
            }
        }
    }

    service.fetch_raw = lambda: copy.deepcopy(raw)  # type: ignore[method-assign]
    first = service.refresh()
    second = service.refresh()

    assert first["deduped"] is False
    assert second["deduped"] is True
    assert first["snapshot_id"] == second["snapshot_id"]
    assert len(service.list_snapshots(limit=50)) == 1


def test_refresh_failure_fallback_and_no_cache_error(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "City A"},
        products={"1": "Item A"},
    )
    raw = {
        "data": {
            "1": {
                "s": {"1": {"t": 1, "v": 0, "ti": 1700000000, "p": 100}},
                "b": {"1": {"t": 0, "v": 0, "ti": 1700000000, "p": 90}},
            }
        }
    }
    service.fetch_raw = lambda: copy.deepcopy(raw)  # type: ignore[method-assign]
    first = service.refresh()

    def _raise_fetch():
        raise RuntimeError("network down")

    service.fetch_raw = _raise_fetch  # type: ignore[method-assign]
    fallback = service.refresh()
    assert fallback["stale"] is True
    assert fallback["snapshot_id"] == first["snapshot_id"]

    empty_service = _build_service(tmp_path / "empty", cities={}, products={})
    empty_service.fetch_raw = _raise_fetch  # type: ignore[method-assign]
    with pytest.raises(ResonanceMarketDataError) as exc:
        empty_service.refresh()
    assert exc.value.code == "market_refresh_failed_no_cache"


def test_unknown_mapping_and_query_filters(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "City A", "2": "City B"},
        products={"1": "Item A", "2": "Item B"},
    )
    raw = {
        "data": {
            "1": {
                "s": {"1": {"t": 1, "v": 1, "ti": 1700000000, "p": 100}},
                "b": {"1": {"t": 0, "v": 1, "ti": 1700000001, "p": 90}},
            },
            "2": {
                "s": {"1": {"t": 0, "v": 2, "ti": 1700000002, "p": 200}},
                "b": {
                    "1": {"t": 1, "v": 2, "ti": 1700000003, "p": 180},
                    "2": {"t": 0, "v": 2, "ti": 1700000004, "p": 170},
                },
            },
            "9": {
                "s": {"9": {"t": 1, "v": 3, "ti": 1700000005, "p": 999}},
            },
        }
    }
    service.fetch_raw = lambda: copy.deepcopy(raw)  # type: ignore[method-assign]
    snapshot = service.refresh()
    assert snapshot["products"]["9"]["name"] == "unknown_9"
    assert snapshot["cities"]["9"]["name"] == "unknown_9"

    non_buy_rows = service.query_products(scope="non_buy")
    assert [row["product_id"] for row in non_buy_rows] == ["9"]

    buy_city_2_rows = service.query_products(side="buy", city_id="2")
    assert [row["product_id"] for row in buy_city_2_rows] == ["2"]

    city_9_rows = service.query_products(city_id="9")
    assert [row["product_id"] for row in city_9_rows] == ["9"]


def test_travel_fatigue_dataset_consistency():
    service = ResonanceMarketDataService()
    payload = service.get_all_travel_fatigue()
    cities = payload["cities"]
    costs = payload["costs"]

    city_ids = sorted(cities.keys(), key=lambda x: int(x))
    assert len(city_ids) == 20

    edge_count = 0
    for i, from_id in enumerate(city_ids):
        for to_id in city_ids[i + 1 :]:
            assert costs[from_id][to_id] == costs[to_id][from_id]
            edge_count += 1
    assert edge_count == 190


def test_get_travel_fatigue_sample_pairs():
    service = ResonanceMarketDataService()
    assert service.get_travel_fatigue("1", "2") == 24
    assert service.get_travel_fatigue("1", "3") == 27
    assert service.get_travel_fatigue("1", "4") == 24
    assert service.get_travel_fatigue("2", "1") == 24


def test_travel_fatigue_csv_matches_json():
    meta_dir = Path("plans/resonance/data/meta")
    json_payload = json.loads((meta_dir / "city_travel_fatigue.json").read_text(encoding="utf-8"))
    costs = json_payload["costs"]
    city_ids = sorted(json_payload["cities"].keys(), key=lambda x: int(x))

    csv_edges = set()
    with (meta_dir / "city_travel_fatigue.csv").open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            a = row["from_city_id"].strip()
            b = row["to_city_id"].strip()
            v = int(row["fatigue"])
            pair = tuple(sorted((a, b)))
            csv_edges.add((pair[0], pair[1], v))

    json_edges = set()
    for i, a in enumerate(city_ids):
        for b in city_ids[i + 1 :]:
            pair = tuple(sorted((a, b)))
            json_edges.add((pair[0], pair[1], int(costs[a][b])))

    assert csv_edges == json_edges
    assert len(csv_edges) == 190


def test_travel_fatigue_invalid_payload_raises(tmp_path: Path):
    cities = {"1": "City A", "2": "City B"}
    products = {"1": "Item A"}
    invalid_payload = {
        "schema_version": "1.0.0",
        "cities": cities,
        "costs": {
            "1": {"1": 0, "2": 11},
            "2": {"1": 9, "2": 0},
        },
    }
    service = _build_service(tmp_path, cities=cities, products=products, fatigue_payload=invalid_payload)

    with pytest.raises(ResonanceMarketDataError) as exc:
        service.get_all_travel_fatigue()
    assert exc.value.code == "travel_fatigue_invalid"


def test_travel_fatigue_missing_edge_raises(tmp_path: Path):
    cities = {"1": "City A", "2": "City B", "3": "City C"}
    products = {"1": "Item A"}
    invalid_payload = {
        "schema_version": "1.0.0",
        "cities": cities,
        "costs": {
            "1": {"1": 0, "2": 11},
            "2": {"1": 11, "2": 0, "3": 14},
            "3": {"1": 12, "2": 14, "3": 0},
        },
    }
    service = _build_service(tmp_path, cities=cities, products=products, fatigue_payload=invalid_payload)

    with pytest.raises(ResonanceMarketDataError) as exc:
        service.get_all_travel_fatigue()
    assert exc.value.code == "travel_fatigue_invalid"


def test_get_travel_fatigue_unknown_city_id_raises(tmp_path: Path):
    service = _build_service(
        tmp_path,
        cities={"1": "City A", "2": "City B"},
        products={"1": "Item A"},
    )
    with pytest.raises(ResonanceMarketDataError) as exc:
        service.get_travel_fatigue("1", "99")
    assert exc.value.code == "unknown_city_id"
