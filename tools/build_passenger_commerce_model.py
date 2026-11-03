"""Export the small numeric passenger catalog from installed CN client files.

This is an offline development tool. Runtime never loads game files or the
decoder. The revenue approximation is implemented separately from these data.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import heapq
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCORES = ("comfort", "foodScores", "playScores", "petScores", "fishScores", "plantScores", "medicalScores")


def _normalize(value):
    if isinstance(value, list):
        if value and all(isinstance(x, dict) and "name" in x and "value" in x for x in value):
            return {x["name"]: _normalize(x["value"]) for x in value}
        return [_normalize(x) for x in value]
    return value


def build(game_data: Path, devtools_src: Path) -> dict:
    sys.path.insert(0, str(devtools_src))
    from aura_resonance_devtools.full_config_export import decode_binary_config

    source_hashes = {}

    def records(name):
        path = game_data / "Patch/BinaryConfig" / (name + ".bin")
        source_hashes[path.relative_to(game_data).as_posix()] = sha256(path.read_bytes()).hexdigest()
        return {row["id"]: row for record in decode_binary_config(path)["records"]
                if not (row := _normalize(record["fields"])).get("isInformalData")}

    config, stations, lines, lists, passengers = [records(name) for name in
        ("ConfigFactory", "HomeStationFactory", "HomeLineFactory", "ListFactory", "PassageFactory")]
    pc = config[99900061]
    graph = {}
    for ref in config[99900014]["lineList"]:
        row = lines[ref["id"]]
        a, b, length = row["station01"], row["station02"], row["distance"]
        graph.setdefault(a, {})[b] = length
        graph.setdefault(b, {})[a] = length
    mapping = json.loads((ROOT / "plans/resonance_pc/data/meta/trade_goods_investment_cities.json").read_text(encoding="utf-8"))["cities"]
    raw_distances = {}
    cities = {}
    relevant_passengers = set()
    relevant_tags = set()
    for city_id, entry in mapping.items():
        native_id = entry["native_station_id"]
        distances, pending = {native_id: 0.0}, [(0.0, native_id)]
        while pending:
            distance, node = heapq.heappop(pending)
            if distance != distances[node]:
                continue
            for neighbor, length in graph.get(node, {}).items():
                candidate = distance + length
                if candidate < distances.get(neighbor, float("inf")):
                    distances[neighbor] = candidate
                    heapq.heappush(pending, (candidate, neighbor))
        raw_distances[city_id] = {other: distances[e["native_station_id"]] for other, e in mapping.items()
                                  if e["native_station_id"] in distances}
        spots = []
        for ref in stations[native_id].get("cityLeafletLest", []):
            spot = lists[ref["id"]]
            pool = [[x["id"], x["weight"]] for x in spot.get("passengerTypeList", []) if x["weight"] > 0]
            relevant_passengers.update(x[0] for x in pool)
            spots.append({"id": spot["id"], "unlock_prestige": spot["unlockPlace"],
                          "order": spot["placeWeight"], "pool": pool,
                          "tags": [[x["id"], x["weight"]] for x in spot.get("passengerTagList", [])]})
        cities[city_id] = {"name": entry["name"], "native_id": native_id, "spots": spots,
                           "leaflet_enabled": bool(stations[native_id].get("isLeaflet")) and bool(spots),
                           "leaflet_unlock": stations[native_id].get("leafletUnlock", 0)}

    def demand(row):
        return {name: [{key: x[key] for key in ("common", "most", "out", "pay")} for x in row.get(name, [])]
                for name in SCORES if row.get(name)}

    passenger_rows = {}
    for pid in sorted(relevant_passengers):
        row = passengers[pid]
        tags = [[x["id"], x["weight"]] for x in row.get("tag", []) if x["weight"] > 0]
        relevant_tags.update(x[0] for x in tags)
        passenger_rows[str(pid)] = {"seat_required": row["isSeat"],
                                   "seat_demand": [x["id"] for x in row.get("seatDemand", [])],
                                   "tags": tags, "demand": demand(row)}
    return {
        "schema_version": 1, "model_version": "cn-passenger-approx-v1",
        "source": {"kind": "native_cn_client_numeric_config", "sha256": source_hashes,
                   "graph": "ConfigFactory[99900014].lineList; all intermediate stations retained",
                   "scope": "Static inputs only; does not reproduce server settlement or sample distribution"},
        "distance_ratio": config[99900014]["disRatio"], "raw_distances": raw_distances,
        "flyer_batch": pc["leafletDozen"], "ticket_distance": pc["tickerDistance"],
        "seat_prices": [row["price"] for row in pc["hardSeat"]],
        "seat_ids": [81300021, 81300081, 81300082, 81300083, 81300084],
        "clean_block_bps": round(config[99900014]["cleantwo"] * 10000),
        "clean_good_bps": round(config[99900014]["cleanone"] * 10000),
        "demand_basics": pc["demandBasics"], "demand_out": pc["demandOut"],
        "distance_buckets": [{key: row[key] for key in ("distance", "adPassageMin", "adPassageMax", "earning")}
                             for row in pc["leafletIncome"]],
        "grades": config[99900081]["scoreGradeList"], "cities": cities,
        "passengers": passenger_rows, "tags": {str(tid): demand(lists[tid]) for tid in sorted(relevant_tags)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, default=ROOT.parent / "Aura_script_lsns_devtools/src")
    parser.add_argument("--output", type=Path, default=ROOT / "plans/resonance_pc/data/meta/passenger_commerce_model.json")
    args = parser.parse_args()
    result = build(args.game_data.resolve(), args.devtools_src.resolve())
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cities": len(result["cities"]), "passengers": len(result["passengers"]),
                      "tags": len(result["tags"]), "model_version": result["model_version"]}))
