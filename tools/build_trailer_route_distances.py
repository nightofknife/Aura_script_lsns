"""Build static city-to-city displayed rail distances from native game data.

The native map constructs an undirected HomeLineFactory graph from
ConfigFactory[99900014].lineList, runs Dijkstra on the raw line distance,
then displays ceil(shortest_distance * disRatio). Non-trading intermediate
stations must remain in that graph. This is not fatigue or map geometry.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import heapq
import json
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
NATIVE_CONFIGS = ("ConfigFactory.bin", "HomeLineFactory.bin", "HomeStationFactory.bin")
NATIVE_SCRIPTS = {
    "UIHome/UIHomeMapDataModel.lua": {
        "InitLineInfo": [46, 73], "CalcShortestDisPath": [267, 345],
        "GetTargetStationDistance": [348, 400],
    },
    "UIHome/UIHomeMapController.lua": {"display_distance": [322, 323]},
}


PLAN_TEXT_HASH_FORMAT = "utf8-crlf-to-lf-v1"


def plan_text_sha256(path: Path) -> str:
    """Hash UTF-8 plan source bytes with CRLF converted to LF only.

    Git checkouts can change line endings; JSON order, whitespace, trailing
    newline, BOM and all other bytes remain significant. Native binary/config
    and Lua evidence hashes deliberately keep their exact original bytes.
    """
    return sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def field_records(decoded: dict) -> dict[int, dict]:
    records = {}
    for record in decoded["records"]:
        row = {field["name"]: field.get("value") for field in record["fields"]}
        if row.get("isInformalData"):
            continue
        if type(row.get("id")) is not int or row["id"] in records:
            raise ValueError("Native record IDs must be unique integers")
        records[row["id"]] = row
    return records


def reference_ids(values: list) -> list[int]:
    result = []
    for fields in values:
        row = {field["name"]: field.get("value") for field in fields}
        if type(row.get("id")) is not int:
            raise ValueError("Native line list contains an invalid reference")
        result.append(row["id"])
    return result


def rail_graph(edges: list[dict]) -> dict[int, dict[int, float]]:
    graph: dict[int, dict[int, float]] = {}
    for edge in edges:
        start, end, distance = edge["station01"], edge["station02"], edge["distance"]
        if (type(start) is not int or type(end) is not int or start == end
                or type(distance) not in (int, float) or not math.isfinite(distance) or distance <= 0):
            raise ValueError("Native rail edges require distinct integer stations and positive distance")
        # Native InitLineInfo assigns both directions in configured lineList order.
        graph.setdefault(start, {})[end] = float(distance)
        graph.setdefault(end, {})[start] = float(distance)
    return graph


def shortest_distances(graph: dict[int, dict[int, float]], start: int) -> dict[int, float]:
    if start not in graph:
        raise ValueError(f"Station {start} is absent from the native rail graph")
    distances = {start: 0.}
    pending = [(0., start)]
    while pending:
        cost, current = heapq.heappop(pending)
        if cost != distances[current]:
            continue
        for neighbor, length in graph[current].items():
            candidate = cost + length
            if candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                heapq.heappush(pending, (candidate, neighbor))
    return distances


def display_distance(raw: float, ratio: float) -> int:
    if (type(raw) not in (int, float) or not math.isfinite(raw) or raw < 0
            or type(ratio) not in (int, float) or not math.isfinite(ratio) or ratio <= 0):
        raise ValueError("Distance and disRatio must be finite and nonnegative/positive")
    return math.ceil(raw * ratio)


def build(game_data: Path, devtools_src: Path, plan: Path = PLAN) -> dict:
    sys.path.insert(0, str(devtools_src.resolve()))
    from aura_resonance_devtools.full_config_export import decode_binary_config

    binary_root = game_data / "Patch/BinaryConfig"
    configs, lines, stations = [field_records(decode_binary_config(binary_root / name))
                               for name in NATIVE_CONFIGS]
    config = configs[99900014]
    ratio = config["disRatio"]
    display_distance(0, ratio)
    edges = []
    for line_id in reference_ids(config["lineList"]):
        line = lines[line_id]
        edges.append({key: line[key] for key in ("id", "station01", "station02", "distance")})
    graph = rail_graph(edges)
    if not set(graph) <= set(stations):
        raise ValueError("Configured rail graph references an unknown native station")

    cities_path = plan / "data/meta/cities.json"
    mapping_path = plan / "data/meta/trade_goods_investment_cities.json"
    cities = json.loads(cities_path.read_text(encoding="utf-8"))
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))["cities"]
    if set(cities) != {str(index) for index in range(1, 22)} or set(mapping) != set(cities):
        raise ValueError("Expected the existing canonical 21-city market mapping")
    native_city_ids = {}
    for city_id, name in cities.items():
        entry = mapping[city_id]
        station_id = entry["native_station_id"]
        if (entry["name"] != name or type(station_id) is not int
                or stations[station_id]["name"] not in entry["aliases"]):
            raise ValueError(f"City {city_id} does not match its existing native station mapping")
        native_city_ids[city_id] = station_id

    distances = {}
    for city_id, native_id in native_city_ids.items():
        raw = shortest_distances(graph, native_id)
        if not set(native_city_ids.values()) <= set(raw):
            raise ValueError(f"City {city_id} cannot reach all 21 trading cities")
        distances[city_id] = {other: display_distance(raw[station_id], ratio)
                              for other, station_id in native_city_ids.items()}
    if any(distances[a][b] != distances[b][a] for a in cities for b in cities):
        raise ValueError("Native displayed city-to-city distances are not symmetric")

    screenshot = "codex-clipboard-ba7e5029-a0b0-4f2e-b479-95601e0e3a7a.png"
    result = {
        "schema_version": 1,
        "cities": cities,
        "distances_km": distances,
        "source": {
            "kind": "native_game_config",
            "distance_kind": "static_city_to_city_displayed_rail_km",
            "graph": "ConfigFactory[99900014].lineList -> HomeLineFactory.station01/station02/distance",
            "algorithm": "undirected graph; minimum sum of native distance; ceil(sum * disRatio)",
            "dis_ratio": ratio,
            "intermediate_stations": "Keep every configured rail station, including non-trading and transit stations; no trade-only graph or straight-line geometry.",
            "runtime_scope": "Stationary city-to-city planning only; does not model remaining distance while already traveling or account-specific route locks.",
            "tie_rule": "Use displayed integer km for ranking; equal displayed km remain equal even if raw path lengths differ.",
            "sha256": {f"Patch/BinaryConfig/{name}": sha256((binary_root / name).read_bytes()).hexdigest()
                       for name in NATIVE_CONFIGS},
            "scripts": {
                f"Patch/Script/{name}": {"sha256": sha256((game_data / "Patch/Script" / name).read_bytes()).hexdigest(),
                                        "function_lines": functions,
                                        "evidence": "decoded Lua 5.3 bytecode constants and instructions; not recovered source"}
                for name, functions in NATIVE_SCRIPTS.items()
            },
            "plan_sha256_format": PLAN_TEXT_HASH_FORMAT,
            "plan_sha256": {path.relative_to(plan).as_posix(): plan_text_sha256(path)
                            for path in (cities_path, mapping_path)},
            "screenshot_cross_check": {
                "attachment": screenshot,
                "attachment_sha256": "f3dd41ab77503085370122dfe3e38665ec76f1fccb45c885b8de68738eb94c99",
                "from_city_id": "1", "to_city_id": "7", "screenshot_displayed_km": 136,
                "current_static_km": distances["1"]["7"],
                "difference_km": distances["1"]["7"] - 136,
                "status": "mismatch_not_calibrated",
                "note": "Current native stationary shortest path is 3250 + 3750 raw units = 140 km. Screenshot shows 136 km; no fabricated correction is applied. A traveling-position offset or a different game data version has not been established.",
            },
        },
        "native_city_ids": native_city_ids,
        "native_stations": {str(station_id): stations[station_id]["name"] for station_id in sorted(graph)},
        "native_edges": edges,
    }
    output = plan / "data/meta/trailer_route_distances.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, default=ROOT.parent / "Aura_script_lsns_devtools/src")
    parser.add_argument("--plan", type=Path, default=PLAN)
    args = parser.parse_args()
    result = build(args.game_data.resolve(), args.devtools_src, args.plan.resolve())
    print(json.dumps({"cities": len(result["cities"]), "pairs": sum(map(len, result["distances_km"].values())),
                      "graph_stations": len(result["native_stations"]), "configured_edges": len(result["native_edges"]),
                      "shoggolith_to_wilderness_km": result["distances_km"]["1"]["7"]}))


if __name__ == "__main__":
    main()
