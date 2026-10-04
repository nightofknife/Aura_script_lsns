"""Shared freight request validation and GUI-facing plan projections."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


PLANNING_KEYS = (
    "trade_mode", "fatigue_budget", "cargo_capacity", "book_budget",
    "book_profit_threshold", "book_policy", "negotiation_policy",
    "fixed_route_city_ids", "reposition_to_route", "target_profit",
    "available_city_ids", "required_end_city_ids", "city_prestige",
    "product_unlocks", "bargain_success_rates_bps", "bargain_step_bps",
    "raise_success_rates_bps", "raise_step_bps",
)


def integer(name: str, value: Any, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be an integer >= {minimum}" +
                         (f" and <= {maximum}" if maximum is not None else ""))
    return value


def _city_ids(name: str, value: Any, *, minimum: int, ordered: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{name} must contain at least {minimum} city IDs")
    if any(not isinstance(city, str) or not city.strip() for city in value):
        raise ValueError(f"{name} must contain nonempty string city IDs")
    cities = [city.strip() for city in value]
    if ordered:
        if any(a == b for a, b in zip(cities, cities[1:])):
            raise ValueError("fixed_route_city_ids cannot contain adjacent identical cities")
        return cities
    cities = list(dict.fromkeys(cities))
    if len(cities) < minimum:
        raise ValueError(f"{name} must contain at least {minimum} distinct city IDs")
    return cities


def normalize_planning_inputs(source: dict[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(source[key]) for key in PLANNING_KEYS if key in source}
    mode = result.get("trade_mode", "profit")
    if mode not in {"profit", "quick", "fixed", "target"}:
        raise ValueError("trade_mode must be profit, quick, fixed, or target")
    result["trade_mode"] = mode
    for key, default, minimum in (("fatigue_budget", 700, 0), ("cargo_capacity", 750, 1),
                                   ("book_profit_threshold", 500000, 0)):
        result[key] = integer(key, result.get(key, default), minimum=minimum)
    books = result.get("book_budget", 0)
    result["book_budget"] = None if books is None else integer("book_budget", books)
    policy = result.get("book_policy", "profit")
    negotiation = result.get("negotiation_policy", "auto")
    if policy not in {"profit", "fill"} or negotiation not in {"auto", "required", "disabled"}:
        raise ValueError("Invalid book_policy or negotiation_policy")
    result["book_policy"] = "fill" if mode == "quick" else policy
    result["negotiation_policy"] = "required" if mode == "quick" else negotiation
    for side in ("bargain", "raise"):
        key = f"{side}_success_rates_bps"
        rates = result.get(key)
        rates = [5000] if rates is None else rates
        if not isinstance(rates, list) or not rates:
            raise ValueError(f"{key} must be a nonempty list")
        result[key] = [integer(key, rate, maximum=10000) for rate in rates]
        key = f"{side}_step_bps"
        result[key] = integer(key, result.get(key, 1000), minimum=1, maximum=2000)
    if mode == "fixed":
        result["fixed_route_city_ids"] = _city_ids(
            "fixed_route_city_ids", result.get("fixed_route_city_ids"), minimum=2, ordered=True,
        )
        if type(result.get("reposition_to_route", False)) is not bool:
            raise ValueError("reposition_to_route must be a boolean")
        result.setdefault("reposition_to_route", False)
        result.pop("available_city_ids", None)
        result.pop("required_end_city_ids", None)
    else:
        result.pop("fixed_route_city_ids", None)
        result.pop("reposition_to_route", None)
        if result.get("available_city_ids") is not None:
            result["available_city_ids"] = _city_ids(
                "available_city_ids", result["available_city_ids"], minimum=2,
            )
        if result.get("required_end_city_ids") is not None:
            ends = _city_ids("required_end_city_ids", result["required_end_city_ids"], minimum=1)
            cities = result.get("available_city_ids")
            if cities is not None and any(city not in cities for city in ends):
                raise ValueError("required_end_city_ids must be selected planning cities")
            result["required_end_city_ids"] = ends
    if mode == "target":
        result["target_profit"] = integer("target_profit", result.get("target_profit"), minimum=1)
    else:
        result.pop("target_profit", None)
    prestige = result.get("city_prestige")
    prestige = {"default": 20, "overrides": {}} if prestige is None else prestige
    if not isinstance(prestige, dict) or not isinstance(prestige.get("overrides", {}), dict):
        raise ValueError("city_prestige must contain default and overrides")
    integer("city_prestige.default", prestige.get("default", 20), minimum=1, maximum=20)
    for value in prestige.get("overrides", {}).values():
        integer("city_prestige.overrides", value, minimum=1, maximum=20)
    result["city_prestige"] = prestige
    unlocks = result.get("product_unlocks")
    unlocks = {"mode": "all", "product_ids": []} if unlocks is None else unlocks
    if not isinstance(unlocks, dict) or unlocks.get("mode") not in {"all", "only"}:
        raise ValueError("product_unlocks.mode must be all or only")
    if not isinstance(unlocks.get("product_ids", []), list):
        raise ValueError("product_unlocks.product_ids must be a list")
    result["product_unlocks"] = unlocks
    return result


def start_mismatch(request: dict[str, Any], city_id: str) -> bool:
    return (request["trade_mode"] == "fixed" and not request["reposition_to_route"]
            and request["fixed_route_city_ids"][0] != city_id)


def fixed_start_stop(request: dict[str, Any]) -> dict[str, Any]:
    return {"status": "fixed_route_infeasible", "reason": "fixed_start_mismatch",
            "route": [], "expected_profit": 0, "expected_profit_exact": "0",
            "fatigue_budget": request["fatigue_budget"], "expected_fatigue_used": 0,
            "expected_fatigue_used_exact": "0", "remaining_expected_fatigue": request["fatigue_budget"],
            "remaining_expected_fatigue_exact": str(request["fatigue_budget"]), "books_used": 0,
            "remaining_books": request["book_budget"],
            "diagnostics": {"code": "fixed_start_mismatch", "execution_allowed": False}}


def plan_view(plan: dict[str, Any], request: dict[str, Any], *, kind: str,
              auto_bento: bool = False, water_plan: dict[str, Any] | None = None,
              investment: bool = False, rubbish: bool = False) -> dict[str, Any]:
    result = deepcopy(plan)
    planning_status = str(plan.get("status") or "no_plan")
    route = [dict(leg) for leg in plan.get("route", [])]
    result.update(trade_mode=request["trade_mode"], request_kind=kind,
                  input_snapshot=deepcopy(request), planning_status=planning_status,
                  book_budget=request["book_budget"],
                  book_budget_unlimited=request["book_budget"] is None,
                  book_policy=request["book_policy"], negotiation_policy=request["negotiation_policy"])
    if planning_status == "no_plan":
        result["reason"] = "no_profitable_route"
    if request["book_budget"] is None:
        result["remaining_books"] = None
    result["reposition"] = {
        "required": bool(plan.get("reposition_route")),
        "route": deepcopy(plan.get("reposition_route") or []),
        "expected_fatigue": plan.get("reposition_expected_fatigue", 0),
        "expected_fatigue_exact": plan.get("reposition_expected_fatigue_exact", "0"),
    }
    for old in ("auto_book", "all_plan", "book_budget_ignored", "books_budget",
                "negotiation_budget", "negotiation_budget_ignored", "remaining_negotiation"):
        result.pop(old, None)
    visits = []
    original = plan.get("city_visits") or []
    rubbish_selected = False
    for index in range(len(route) + 1 if route else 0):
        incoming = route[index - 1] if index else None
        following = route[index] if index < len(route) else None
        city_id = str((incoming or following).get("to_city_id" if incoming else "from_city_id", ""))
        city_name = str((incoming or following).get("to_city" if incoming else "from_city", ""))
        visit = deepcopy(original[index]) if index < len(original) else {}
        phases = []
        def phase(key: str, enabled: bool, reason: str = "not_applicable") -> None:
            phases.append({"key": key, "status": "waiting" if enabled else "skipped",
                           "reason": None if enabled else reason})
        if incoming:
            phase("arrival", True)
            phase("investment", investment and city_id == "11")
            eligible_rubbish = rubbish and city_id in {"7", "14"} and not rubbish_selected
            phase("rubbish_recycling", eligible_rubbish)
            rubbish_selected = rubbish_selected or eligible_rubbish
            phase("sparkling_water", bool(water_plan and water_plan.get("planned")
                                         and water_plan.get("city_index") == index))
        phase("sell" if following else "final_sale", True)
        if following:
            phase("books", bool(following.get("books_used")), "no_books_planned")
            phase("buy", bool(following.get("buy_products")), "no_goods_planned")
            phase("travel", True)
        else:
            phase("bento", auto_bento, "disabled")
        visit.update(city_index=index, city_count=len(route) + 1, city_id=city_id,
                     city_name=city_name, role="initial" if index == 0 else
                     "terminal" if following is None else "intermediate", phases=phases)
        visits.append(visit)
    result["city_visits"] = visits
    return result


def planning_event_data(plan: dict[str, Any]) -> dict[str, Any]:
    excluded = {"route", "city_visits", "execution", "input_snapshot"}
    return {"route": deepcopy(plan.get("route") or []),
            "city_visits": deepcopy(plan.get("city_visits") or []),
            "reposition": deepcopy(plan.get("reposition") or {}),
            "summary": {key: deepcopy(value) for key, value in plan.items() if key not in excluded}}
