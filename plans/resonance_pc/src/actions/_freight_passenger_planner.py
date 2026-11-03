"""Pure interval planning over an immutable, already selected freight route.

The dynamic program is exact for the *approximate* passenger model below.
It does not reconstruct the game's server settlement or alter freight legs.
Each selected interval represents one maximum-fill recruitment, not a chosen
number of leaflets. The spending target is only a stopping condition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Any

from packages.resonance_shared.passenger_parameters import (
    SCORE_NAMES, normalize_passenger_options, resolve_flyer_target,
)


@lru_cache(maxsize=1)
def _load_model() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "data/meta/passenger_commerce_model.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _prestige(city_id: str, values: Any) -> int:
    if values is None:
        return 20
    if not isinstance(values, Mapping):
        raise ValueError("city_prestige must be an object")
    overrides = values.get("overrides", {})
    if not isinstance(overrides, Mapping):
        raise ValueError("city_prestige.overrides must be an object")
    value = overrides.get(city_id, values.get(city_id, values.get("default", 20)))
    return _nonnegative_int(value, "city prestige")


def _visits(route: Any, model: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(route, Sequence) or isinstance(route, (str, bytes)):
        raise ValueError("route must be a list of freight legs")
    visits = []
    for i, leg in enumerate(route):
        if not isinstance(leg, Mapping):
            raise ValueError("route legs must be objects")
        source, destination = str(leg.get("from_city_id") or ""), str(leg.get("to_city_id") or "")
        if not source or not destination:
            raise ValueError("route legs require from_city_id and to_city_id")
        if i and visits[-1]["city_id"] != source:
            raise ValueError("freight route must contain consecutive city visits")
        if not i:
            visits.append({"city_id": source, "city_name": str(leg.get("from_city") or model["cities"].get(source, {}).get("name") or source)})
        visits.append({"city_id": destination, "city_name": str(leg.get("to_city") or model["cities"].get(destination, {}).get("name") or destination)})
    return visits


def _grade_bonus(scores: Mapping[str, int], model: dict[str, Any]) -> float:
    aliases = {"comfort": "comfort", "foodScores": "delicious", "playScores": "play",
               "petScores": "pet", "fishScores": "fish", "plantScores": "plant", "medicalScores": "medic"}
    return max((row["upwin"] for row in model["grades"]
                if all(scores[key] >= row.get(alias, 0) for key, alias in aliases.items())), default=0.0)


def _demand_value(score: int, rows: list[dict], tag_rows: list[dict], model: dict) -> float:
    """Continuous surrogate through common/most, with the configured out tier.

    Thresholds/pay are native data. Interpolation and tag aggregation are
    explicit modelling assumptions; final server arithmetic is unavailable.
    """
    demands = rows + tag_rows
    if not demands:
        return 0.0
    common = sum(row["common"] for row in demands)
    most = sum(row["most"] for row in demands)
    out = sum(max(0, row["out"]) for row in demands)
    pay = sum(row["pay"] for row in demands)
    basic = model["demand_basics"]
    if common > 0 and score < common:
        multiplier = basic * score / common
    elif most > common and score < most:
        multiplier = basic + (1 - basic) * (score - common) / (most - common)
    else:
        multiplier = model["demand_out"] if out > most and score > out else 1.0
    return pay * multiplier


def _expected_extra(spot: dict, options: dict, model: dict) -> float:
    price_id = model["seat_ids"][options["seat_level"] - 1]
    pool = [(model["passengers"][str(pid)], weight) for pid, weight in spot["pool"]
            if str(pid) in model["passengers"] and weight > 0]
    pool = [(person, weight) for person, weight in pool
            if not person["seat_required"] or price_id in person["seat_demand"]]
    total_weight = sum(weight for _, weight in pool)
    if not total_weight:
        return 0.0
    spot_tags = dict(spot.get("tags", []))
    extra = 0.0
    for person, weight in pool:
        tags = [(model["tags"].get(str(tid), {}), tw * spot_tags.get(tid, 1))
                for tid, tw in person.get("tags", []) if tw > 0]
        total_tag_weight = sum(tw for _, tw in tags)
        value = 0.0
        for tag, tw in tags if total_tag_weight else [({}, 1)]:
            consumption = sum(_demand_value(options["scores"][name], person["demand"].get(name, []), tag.get(name, []), model)
                              for name in SCORE_NAMES)
            value += consumption * tw / (total_tag_weight or 1)
        # First version deliberately excludes crowding and empty-seat bonuses.
        extra += value * weight / total_weight
    return extra


def _trip_candidates(visits: list[dict], options: dict, prestige: Any, start: int, model: dict) -> dict[int, list[dict]]:
    candidates: dict[int, list[dict]] = {}
    for i in range(start, len(visits) - 1):
        source = visits[i]
        city = model["cities"].get(source["city_id"], {})
        if (not city.get("leaflet_enabled")
                or _prestige(source["city_id"], prestige) < city.get("leaflet_unlock", 0)):
            continue
        spots = [spot for spot in city.get("spots", [])
                 if spot["unlock_prestige"] <= _prestige(source["city_id"], prestige)]
        if not spots:
            continue
        # Match the UI action's highest/rightmost unlocked recruitment place.
        spot = max(spots, key=lambda row: (row["order"], row["id"]))
        seen = {source["city_id"]}
        for j in range(i + 1, len(visits)):
            destination = visits[j]
            city_id = destination["city_id"]
            if city_id in seen:
                continue
            seen.add(city_id)  # first future arrival, even if this pair is ineligible
            destination_city = model["cities"].get(city_id, {})
            if (not destination_city.get("leaflet_enabled")
                    or _prestige(city_id, prestige) < destination_city.get("leaflet_unlock", 0)):
                continue
            distance = model["raw_distances"].get(source["city_id"], {}).get(city_id)
            if city_id not in model["cities"] or distance is None or distance <= 0:
                continue
            # Native table is discrete. Nearest 1000 raw units is an explicit
            # preview assumption; use lower/upper clamps outside table coverage.
            bucket_distance = math.floor(distance / 1000 + 0.5) * 1000
            bucket = min(model["distance_buckets"], key=lambda row: (abs(row["distance"] - bucket_distance), row["distance"]))
            mean = (bucket["adPassageMin"] + bucket["adPassageMax"]) / 2
            candidates.setdefault(i, []).append({
                "from_index": i, "to_index": j, "from_city_id": source["city_id"], "to_city_id": city_id,
                "from_city": source["city_name"], "to_city": destination["city_name"],
                "raw_distance": distance, "distance_km": distance * model["distance_ratio"],
                "recruitment_place_id": spot["id"], "passengers_per_batch": mean,
                "earning_multiplier": bucket["earning"], "_spot": spot,
            })
    return candidates


def _full_trip(candidate: dict, inventory: int, options: dict, model: dict, *, extra_cache: dict | None = None) -> dict:
    mean = candidate["passengers_per_batch"]
    batches = min(inventory // model["flyer_batch"], math.ceil(options["seat_capacity"] / mean))
    count = min(options["seat_capacity"], batches * mean)
    ticket = model["seat_prices"][options["seat_level"] - 1] * max(1.0, candidate["raw_distance"] / model["ticket_distance"])
    cache_key = candidate["_spot"]["id"]
    if extra_cache is not None and cache_key in extra_cache:
        per_person_extra = extra_cache[cache_key]
    else:
        per_person_extra = _expected_extra(candidate["_spot"], options, model)
        if extra_cache is not None:
            extra_cache[cache_key] = per_person_extra
    extra = per_person_extra * candidate["earning_multiplier"]
    extra *= 1 + _grade_bonus(options["scores"], model) + options["furniture_bonus_bps"] / 10000
    clean = 1.0 if options["cleanliness_bps"] >= model["clean_good_bps"] else options["cleanliness_bps"] / 10000
    result = {key: value for key, value in candidate.items() if not key.startswith("_")}
    result.update(flyer_batches=batches, expected_flyers_used=batches * model["flyer_batch"],
                  expected_passengers=round(count, 6), expected_ticket_revenue=round(count * ticket * clean, 6),
                  expected_extra_revenue=round(count * extra * clean, 6),
                  expected_revenue=round(count * (ticket + extra) * clean, 6))
    return result


def build_passenger_plan(route: Any, options: Any, *, city_prestige: Any = None,
                         inventory: int | None = None, target_remaining: int | None = None,
                         start_index: int = 0) -> dict[str, Any]:
    values = normalize_passenger_options(options)
    model = _load_model()
    visits = _visits(route, model)
    start = _nonnegative_int(start_index, "start_index")
    if start > len(route):
        raise ValueError("start_index cannot exceed the freight route length")
    target = resolve_flyer_target(values) if target_remaining is None else _nonnegative_int(target_remaining, "target_remaining")
    available = target if inventory is None else _nonnegative_int(inventory, "inventory")
    assumptions = [
        "Expected values use a CN native numeric catalog, not the server settlement implementation.",
        "Every recruitment uses maximum-fill; the target stops future recruitment and does not cap the last trip.",
        "Recruitment range midpoint and deterministic seat clipping approximate random recruitment and early full-seat stopping.",
        "Passenger/tag weights, score interpolation and modifier ordering are approximations; no crowding or empty-seat bonuses are modelled.",
        "Uniform seat level and furniture bonus; no character, livery, special-city income or weekly news bonuses.",
        "Cleanliness is held constant and the intermediate cleanliness penalty is approximated on total income.",
        "Revenue uses shortest endpoint rail distance, not accumulated freight distance; bucket selection uses nearest 1000 raw units.",
    ]
    if inventory is None:
        assumptions.append("Preview inventory is assumed equal to the spending target; runtime must read actual inventory.")
    result = {"trips": [], "target": target, "inventory": available, "expected_flyers_used": 0,
              "expected_revenue": 0.0, "stop_reason": "route_exhausted", "assumptions": assumptions,
              "model_version": model["model_version"]}
    if not target:
        result["stop_reason"] = "target_completed"
        return result
    if values["cleanliness_bps"] < model["clean_block_bps"]:
        result["stop_reason"] = "cleanliness_too_low"
        return result
    if available < model["flyer_batch"]:
        result["stop_reason"] = "inventory_below_batch"
        return result
    candidates = _trip_candidates(visits, values, city_prestige, start, model)
    # State = (visit, spent). Retain highest revenue, then fewest intervals,
    # then lexicographically earliest indices. Reaching target terminates
    # recruitment; later freight visits are still implicitly traversed.
    states: list[dict[int, tuple[float, tuple]]] = [{} for _ in range(len(route) + 1)]
    states[start][0] = (0.0, ())
    extra_cache: dict[int, float] = {}
    trip_cache: dict[tuple, dict] = {}

    def tie_key(state):
        revenue, trips = state
        return (-revenue, len(trips), tuple((trip["from_index"], trip["to_index"]) for trip in trips))

    def retain(index, spent, state):
        old = states[index].get(spent)
        if old is None or tie_key(state) < tie_key(old):
            states[index][spent] = state

    for i in range(start, len(route)):
        for spent, state in tuple(states[i].items()):
            retain(i + 1, spent, state)
            if spent >= target or available - spent < model["flyer_batch"]:
                continue
            for candidate in candidates.get(i, []):
                batches = min((available - spent) // model["flyer_batch"],
                              math.ceil(values["seat_capacity"] / candidate["passengers_per_batch"]))
                cache_key = (candidate["from_index"], candidate["to_index"], batches)
                trip = trip_cache.get(cache_key)
                if trip is None:
                    trip = _full_trip(candidate, available - spent, values, model, extra_cache=extra_cache)
                    trip_cache[cache_key] = trip
                retain(trip["to_index"], spent + trip["expected_flyers_used"],
                       (round(state[0] + trip["expected_revenue"], 6), state[1] + (trip,)))
    spent, state = min(states[len(route)].items(), key=lambda item:
                       (max(0, target - item[0]), *tie_key(item[1])))
    result.update(trips=list(state[1]), expected_flyers_used=spent, expected_revenue=state[0])
    if spent >= target:
        result["stop_reason"] = "target_completed"
    elif available - spent < model["flyer_batch"]:
        result["stop_reason"] = "inventory_exhausted"
    elif not candidates:
        result["stop_reason"] = "no_eligible_trips"
    return result
