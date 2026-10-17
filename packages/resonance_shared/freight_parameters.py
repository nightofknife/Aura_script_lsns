"""Pure parameter dependencies shared by freight UI and task boundaries."""
from __future__ import annotations

from typing import Any, Mapping

FREIGHT_MODES = ("free", "fixed")
FREIGHT_PLANNING_KEYS = (
    "trade_mode", "fatigue_budget", "cargo_capacity", "book_budget",
    "book_profit_threshold", "book_waste_limit", "book_policy", "negotiation_policy",
    "fixed_route_city_ids", "reposition_to_route", "target_profit",
    "available_city_ids", "required_end_city_ids", "city_prestige", "city_tax_rates_bps",
    "product_unlocks", "bargain_success_rates_bps", "bargain_step_bps",
    "raise_success_rates_bps", "raise_step_bps",
)


def effective_freight_inputs(source: Mapping[str, Any]) -> dict[str, Any]:
    """Project a saved form onto active task fields without changing the form."""
    result = dict(source)
    mode = result.get("trade_mode", "free")
    if mode not in FREIGHT_MODES:
        raise ValueError("trade_mode must be free or fixed")
    result["trade_mode"] = mode
    if mode == "fixed":
        for key in ("available_city_ids", "required_end_city_ids", "target_profit"):
            result.pop(key, None)
    else:
        for key in ("fixed_route_city_ids", "reposition_to_route"):
            result.pop(key, None)
        if result.get("target_profit_enabled") is False or result.get("target_profit") is None:
            result.pop("target_profit", None)
    result.pop("target_profit_enabled", None)
    if type(result.get("book_budget")) is int and result["book_budget"] == 0:
        result.pop("book_policy", None)
        result.pop("book_profit_threshold", None)
        result.pop("book_waste_limit", None)
    elif result.get("book_policy", "profit") == "fill":
        result.pop("book_profit_threshold", None)
    else:
        result.pop("book_waste_limit", None)
    if result.get("negotiation_policy") == "disabled":
        result.pop("negotiation_max_attempts", None)
    if result.get("auto_bento") is False:
        result.pop("bento_priority", None)
    if result.get("auto_bento") is False and result.get("auto_sparkling_water") is False:
        result.pop("base_fatigue_reserve", None)
    if result.get("use_fatigue_medicine") is False:
        result.pop("allowed_fatigue_medicines", None)
        result.pop("fatigue_medicine_max_uses", None)
    return result


def freight_control_state(*, mode: str, book_usage: str, target_enabled: bool,
                          negotiation: str, water: bool, bento: bool, medicine: bool,
                          book_policy: str = "profit") -> dict[str, bool]:
    """All enable/visibility dependencies; a busy form applies one outer lock."""
    return {"fixed": mode == "fixed", "free": mode == "free",
            "book_count": book_usage == "finite", "book_rules": book_usage != "none",
            "book_threshold": book_usage != "none" and book_policy == "profit",
            "book_profit_threshold": book_usage != "none" and book_policy == "profit",
            "book_waste_limit": book_usage != "none" and book_policy == "fill",
            "target": mode == "free" and target_enabled,
            "negotiation_attempts": negotiation != "disabled",
            "recovery_reserve": water or bento, "bento": bento, "medicine": medicine}
