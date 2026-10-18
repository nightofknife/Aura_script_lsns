"""Actions for ResonancePc trade planner service."""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.observability.logging.core_logger import current_cid

from ..services.resonance_pc_trade_planner_service import ResonancePcTradePlannerService
from ._freight_contract import normalize_planning_inputs


def _require_service(service: Optional[ResonancePcTradePlannerService]) -> ResonancePcTradePlannerService:
    if service is None:
        raise RuntimeError("resonance_pc_trade_planner service is not available.")
    return service


@action_info(
    name="resonance_pc.trade_plan_optimal_route",
    public=True,
    read_only=True,
    description="Plan one exact freight route using the selected trade objective and frozen market snapshot.",
)
@requires_services(resonance_pc_trade_planner="resonance_pc_trade_planner")
def resonance_pc_trade_plan_optimal_route(
    fatigue_budget: int = 700,
    cargo_capacity: int = 750,
    book_budget: Optional[int] = 0,
    trade_mode: str = "free",
    book_policy: str = "profit",
    book_waste_limit: int = 0,
    negotiation_policy: str = "auto",
    fixed_route_city_ids: Optional[List[str]] = None,
    reposition_to_route: bool = False,
    target_profit: Optional[int] = None,
    book_profit_threshold: Any = 500000,
    bargain_success_rates_bps: Optional[List[Any]] = [5000],
    bargain_step_bps: Optional[Any] = 1000,
    raise_success_rates_bps: Optional[List[Any]] = [5000],
    raise_step_bps: Optional[Any] = 1000,
    available_city_ids: Optional[List[str]] = None,
    required_end_city_ids: Optional[List[str]] = None,
    city_prestige: Optional[Dict[str, Any]] = None,
    city_tax_rates_bps: Optional[Dict[str, Any]] = None,
    product_unlocks: Optional[Dict[str, Any]] = None,
    current_city_key: Optional[str] = None,
    current_city_id: Optional[str] = None,
    current_city: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
) -> Dict[str, Any]:
    request = normalize_planning_inputs(locals())
    return _require_service(resonance_pc_trade_planner).plan_optimal_route(
        **request,
        negotiation_budget=None,
        current_city_key=current_city_key,
        current_city_id=current_city_id,
        current_city=current_city,
        snapshot_id=snapshot_id,
    )


def _loop_state_key() -> str:
    cid = str(current_cid() or "").strip()
    if not cid or cid == "-":
        cid = uuid.uuid4().hex
    return f"resonance_pc.auto_cycle_trade.{cid}"


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _merge_usage_lists(*usage_lists: Any) -> List[Dict[str, Any]]:
    merged: Dict[str, int] = {}
    order: List[str] = []
    for usage in usage_lists:
        if not isinstance(usage, list):
            continue
        for item in usage:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if name not in merged:
                order.append(name)
                merged[name] = 0
            merged[name] += _coerce_int(item.get("count"), 0)
    return [{"name": name, "count": count} for name in order if (count := int(merged.get(name) or 0)) > 0]


def _route_exec_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_key": state.get("run_key"),
        "status": state.get("status") or "ok",
        "reason": state.get("reason"),
        "should_continue": bool(state.get("should_continue")),
        "index": int(state.get("index") or 0),
        "route_count": len(state.get("route") or []),
        "completed_leg_count": len(state.get("completed_route") or []),
        "completed_route": copy.deepcopy(state.get("completed_route") or []),
        "current_leg": copy.deepcopy(state.get("current_leg")),
        "blocked_at": state.get("blocked_at"),
        "blocked_leg": copy.deepcopy(state.get("blocked_leg")),
        "fatigue_medicine_used": copy.deepcopy(state.get("fatigue_medicine_used") or []),
        "fatigue_medicine_use_count": int(state.get("fatigue_medicine_use_count") or 0),
    }


@action_info(
    name="resonance_pc.trade_route_execution_init",
    public=True,
    read_only=False,
    description="Initialize sequential execution state for one planned trade route.",
)
@requires_services(state_store="core/state_store")
async def resonance_pc_trade_route_execution_init(
    route: Optional[List[Dict[str, Any]]] = None,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    normalized_route = [copy.deepcopy(item) for item in (route or []) if isinstance(item, dict)]
    run_key = f"{_loop_state_key()}.route"
    state = {
        "run_key": run_key,
        "status": "running" if normalized_route else "ok",
        "reason": None,
        "should_continue": bool(normalized_route),
        "route": normalized_route,
        "index": 0,
        "current_leg": copy.deepcopy(normalized_route[0]) if normalized_route else None,
        "completed_route": [],
        "blocked_at": None,
        "blocked_leg": None,
        "fatigue_medicine_used": [],
        "fatigue_medicine_use_count": 0,
    }
    await state_store.set(run_key, state)
    return _route_exec_summary(state)


@action_info(
    name="resonance_pc.trade_route_execution_update",
    public=True,
    read_only=False,
    description="Update sequential route execution state after one trade leg.",
)
@requires_services(state_store="core/state_store")
async def resonance_pc_trade_route_execution_update(
    run_key: str,
    leg: Optional[Dict[str, Any]] = None,
    travel_status: Optional[str] = None,
    reason: Optional[str] = None,
    blocked_at: Optional[str] = None,
    fatigue_medicine_used: Optional[List[Dict[str, Any]]] = None,
    fatigue_medicine_use_count: int = 0,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    state = await state_store.get(run_key, {})
    if not isinstance(state, dict):
        state = {"run_key": run_key, "route": [], "index": 0, "completed_route": []}

    route = list(state.get("route") or [])
    index = _coerce_int(state.get("index"), 0)
    current_leg = copy.deepcopy(leg if isinstance(leg, dict) else (state.get("current_leg") or {}))
    usage = _merge_usage_lists(state.get("fatigue_medicine_used"), fatigue_medicine_used)
    usage_count = int(state.get("fatigue_medicine_use_count") or 0) + max(
        _coerce_int(fatigue_medicine_use_count), 0
    )
    status = str(travel_status or "ok").strip().lower()

    if status == "blocked":
        state.update(
            {
                "status": "blocked",
                "reason": reason or "travel_blocked",
                "should_continue": False,
                "blocked_at": blocked_at or "departure",
                "blocked_leg": current_leg,
                "current_leg": None,
                "fatigue_medicine_used": usage,
                "fatigue_medicine_use_count": usage_count,
            }
        )
    else:
        completed_route = list(state.get("completed_route") or [])
        if current_leg:
            completed_route.append(current_leg)
        next_index = index + 1
        next_leg = copy.deepcopy(route[next_index]) if next_index < len(route) else None
        state.update(
            {
                "status": "running" if next_leg else "ok",
                "reason": None,
                "should_continue": next_leg is not None,
                "index": next_index,
                "current_leg": next_leg,
                "completed_route": completed_route,
                "fatigue_medicine_used": usage,
                "fatigue_medicine_use_count": usage_count,
            }
        )

    await state_store.set(run_key, state)
    return _route_exec_summary(state)


@action_info(
    name="resonance_pc.trade_route_execution_summary",
    public=True,
    read_only=True,
    description="Return sequential route execution state.",
)
@requires_services(state_store="core/state_store")
async def resonance_pc_trade_route_execution_summary(
    run_key: str,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    state = await state_store.get(run_key, {})
    return _route_exec_summary(state if isinstance(state, dict) else {"run_key": run_key})


@action_info(
    name="resonance_pc.trade_route_execution_cleanup",
    public=True,
    read_only=False,
    description="Remove sequential route execution state.",
)
@requires_services(state_store="core/state_store")
async def resonance_pc_trade_route_execution_cleanup(
    run_key: str,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    await state_store.delete(run_key)
    return {"success": True, "run_key": run_key}


@action_info(
    name="resonance_pc.trade_assert_allowed_city",
    public=False,
    read_only=True,
    description="Assert target city key is in configured trade constraints whitelist.",
)
@requires_services(resonance_pc_trade_planner="resonance_pc_trade_planner")
def resonance_pc_trade_assert_allowed_city(
    city_key: Optional[str] = None,
    city_id: Optional[str] = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_pc_trade_planner).assert_allowed_city(
        city_key=city_key,
        city_id=city_id,
    )
