"""Actions for Resonance trade planner service."""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.observability.logging.core_logger import current_cid

from ..services.resonance_trade_planner_service import ResonanceTradePlannerService


def _require_service(service: Optional[ResonanceTradePlannerService]) -> ResonanceTradePlannerService:
    if service is None:
        raise RuntimeError("resonance_trade_planner service is not available.")
    return service


@action_info(
    name="resonance.trade_plan_next",
    public=True,
    read_only=True,
    description="Plan the next Resonance trade step with rolling horizon optimization.",
)
@requires_services(resonance_trade_planner="resonance_trade_planner")
def resonance_trade_plan_next(
    start_city_id: str,
    fatigue_budget: int,
    book_budget: int,
    cargo_capacity: int,
    book_profit_threshold: float,
    available_city_ids: List[str],
    station_product_whitelist: Optional[Dict[str, List[str]]] = None,
    snapshot_id: Optional[str] = None,
    current_holdings: Optional[Dict[str, Dict[str, float]]] = None,
    resonance_trade_planner: ResonanceTradePlannerService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_trade_planner).plan_next_step(
        start_city_id=start_city_id,
        fatigue_budget=fatigue_budget,
        book_budget=book_budget,
        cargo_capacity=cargo_capacity,
        book_profit_threshold=book_profit_threshold,
        available_city_ids=available_city_ids,
        station_product_whitelist=station_product_whitelist,
        snapshot_id=snapshot_id,
        current_holdings=current_holdings,
    )


@action_info(
    name="resonance.trade_plan_best_cycle",
    public=True,
    read_only=True,
    description="Plan one fixed best-profit trade cycle.",
)
@requires_services(resonance_trade_planner="resonance_trade_planner")
def resonance_trade_plan_best_cycle(
    cargo_capacity: int = 120,
    book_budget: int = 0,
    book_profit_threshold: float = 0,
    available_city_ids: Optional[List[str]] = None,
    start_city_id: Optional[str] = None,
    current_city_id: Optional[str] = None,
    current_city: Optional[str] = None,
    max_cycle_hops: int = 6,
    station_product_whitelist: Optional[Dict[str, List[str]]] = None,
    snapshot_id: Optional[str] = None,
    resonance_trade_planner: ResonanceTradePlannerService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_trade_planner).plan_best_cycle(
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        book_profit_threshold=book_profit_threshold,
        available_city_ids=available_city_ids,
        start_city_id=start_city_id,
        current_city_id=current_city_id,
        current_city=current_city,
        max_cycle_hops=max_cycle_hops,
        station_product_whitelist=station_product_whitelist,
        snapshot_id=snapshot_id,
    )


@action_info(
    name="resonance.trade_plan_cycle_execution",
    public=True,
    read_only=True,
    description="Build executable auto-trade cycle plan under fatigue budget with whitelist constraints.",
)
@requires_services(resonance_trade_planner="resonance_trade_planner")
def resonance_trade_plan_cycle_execution(
    fatigue_budget: int,
    current_city_key: Optional[str] = None,
    current_city_id: Optional[str] = None,
    current_city: Optional[str] = None,
    cargo_capacity: int = 650,
    book_budget: int = 0,
    book_profit_threshold: float = 0,
    max_cycle_hops: int = 6,
    snapshot_id: Optional[str] = None,
    resonance_trade_planner: ResonanceTradePlannerService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_trade_planner).plan_cycle_execution(
        current_city_key=current_city_key,
        fatigue_budget=fatigue_budget,
        current_city_id=current_city_id,
        current_city=current_city,
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        book_profit_threshold=book_profit_threshold,
        max_cycle_hops=max_cycle_hops,
        snapshot_id=snapshot_id,
    )


@action_info(
    name="resonance.trade_plan_next_cycle_execution",
    public=True,
    read_only=True,
    description="Build the next executable trade cycle or final budgeted prefix.",
)
@requires_services(resonance_trade_planner="resonance_trade_planner")
def resonance_trade_plan_next_cycle_execution(
    fatigue_budget: int,
    current_city_key: Optional[str] = None,
    current_city_id: Optional[str] = None,
    current_city: Optional[str] = None,
    cargo_capacity: int = 650,
    book_budget: int = 0,
    book_profit_threshold: float = 0,
    max_cycle_hops: int = 6,
    snapshot_id: Optional[str] = None,
    resonance_trade_planner: ResonanceTradePlannerService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_trade_planner).plan_next_cycle_execution(
        current_city_key=current_city_key,
        fatigue_budget=fatigue_budget,
        current_city_id=current_city_id,
        current_city=current_city,
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        book_profit_threshold=book_profit_threshold,
        max_cycle_hops=max_cycle_hops,
        snapshot_id=snapshot_id,
    )


def _loop_state_key() -> str:
    cid = str(current_cid() or "").strip()
    if not cid or cid == "-":
        cid = uuid.uuid4().hex
    return f"resonance.auto_cycle_trade.{cid}"


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _summary_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": state.get("status") or ("ok" if state.get("route") else "no_plan"),
        "reason": state.get("reason"),
        "snapshot_id": state.get("snapshot_id"),
        "snapshot_ids": list(state.get("snapshot_ids") or []),
        "expected_profit": float(state.get("expected_profit") or 0.0),
        "fatigue_budget": int(state.get("fatigue_budget") or 0),
        "fatigue_used": int(state.get("fatigue_used") or 0),
        "remaining_fatigue": int(state.get("remaining_fatigue") or 0),
        "books_budget": int(state.get("books_budget") or 0),
        "books_used": int(state.get("books_used") or 0),
        "rounds_completed": int(state.get("rounds_completed") or 0),
        "entry_route_count": int(state.get("entry_route_count") or 0),
        "city_cycle": list(state.get("city_cycle") or []),
        "rounds": copy.deepcopy(state.get("rounds") or []),
        "route": copy.deepcopy(state.get("route") or []),
        "current_city": state.get("current_city"),
        "current_city_key": state.get("current_city_key"),
        "should_continue": bool(state.get("should_continue")),
        "blocked_at": state.get("blocked_at"),
        "blocked_leg": copy.deepcopy(state.get("blocked_leg")),
        "fatigue_medicine_used": copy.deepcopy(state.get("fatigue_medicine_used") or []),
        "fatigue_medicine_use_count": int(state.get("fatigue_medicine_use_count") or 0),
    }


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
    name="resonance.trade_loop_init",
    public=True,
    read_only=False,
    description="Initialize one auto-cycle trade loop state.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_loop_init(
    current_city: str,
    current_city_key: Optional[str] = None,
    fatigue_budget: int = 0,
    book_budget: int = 0,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    run_key = _loop_state_key()
    budget = max(_coerce_int(fatigue_budget), 0)
    normalized_current_city = str(current_city or "").strip()
    normalized_current_city_key = str(current_city_key or "").strip()
    state = {
        "status": "running",
        "reason": None,
        "should_continue": budget > 0,
        "current_city": normalized_current_city or normalized_current_city_key,
        "current_city_key": normalized_current_city_key,
        "snapshot_id": None,
        "snapshot_ids": [],
        "expected_profit": 0.0,
        "fatigue_budget": budget,
        "fatigue_used": 0,
        "remaining_fatigue": budget,
        "books_budget": max(_coerce_int(book_budget), 0),
        "books_used": 0,
        "rounds_completed": 0,
        "entry_route_count": 0,
        "city_cycle": [],
        "rounds": [],
        "route": [],
        "blocked_at": None,
        "blocked_leg": None,
        "fatigue_medicine_used": [],
        "fatigue_medicine_use_count": 0,
    }
    if not state["current_city"] and not state["current_city_key"]:
        state["status"] = "no_plan"
        state["reason"] = "current_city_not_resolved"
        state["should_continue"] = False
    await state_store.set(run_key, state)
    summary = _summary_from_state(state)
    summary["run_key"] = run_key
    return summary


@action_info(
    name="resonance.trade_route_execution_init",
    public=True,
    read_only=False,
    description="Initialize sequential execution state for one planned trade route.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_route_execution_init(
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
    name="resonance.trade_route_execution_update",
    public=True,
    read_only=False,
    description="Update sequential route execution state after one trade leg.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_route_execution_update(
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
    name="resonance.trade_route_execution_summary",
    public=True,
    read_only=True,
    description="Return sequential route execution state.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_route_execution_summary(
    run_key: str,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    state = await state_store.get(run_key, {})
    return _route_exec_summary(state if isinstance(state, dict) else {"run_key": run_key})


@action_info(
    name="resonance.trade_route_execution_cleanup",
    public=True,
    read_only=False,
    description="Remove sequential route execution state.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_route_execution_cleanup(
    run_key: str,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    await state_store.delete(run_key)
    return {"success": True, "run_key": run_key}


@action_info(
    name="resonance.trade_loop_update",
    public=True,
    read_only=False,
    description="Accumulate one executed auto-cycle trade round into loop state.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_loop_update(
    run_key: str,
    plan: Optional[Dict[str, Any]] = None,
    execution: Optional[Dict[str, Any]] = None,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    state = await state_store.get(run_key, {})
    if not isinstance(state, dict):
        state = {}
    plan = plan if isinstance(plan, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    route = list(plan.get("route") or [])
    execution_status = str(execution.get("status") or "").strip().lower()
    execution_usage = _merge_usage_lists(execution.get("fatigue_medicine_used"))
    execution_usage_count = _coerce_int(execution.get("fatigue_medicine_use_count"), 0)

    if execution_status == "blocked":
        completed_route = list(execution.get("completed_route") or [])
        all_route = list(state.get("route") or [])
        all_route.extend(copy.deepcopy(completed_route))
        state.update(
            {
                "status": "blocked",
                "reason": execution.get("reason") or "travel_blocked",
                "should_continue": False,
                "snapshot_id": plan.get("snapshot_id") or state.get("snapshot_id"),
                "route": all_route,
                "current_city": str(
                    ((execution.get("blocked_leg") or {}).get("from_city"))
                    or ((completed_route[-1] or {}).get("to_city") if completed_route else "")
                    or state.get("current_city")
                    or ""
                ),
                "current_city_key": "",
                "blocked_at": execution.get("blocked_at") or "departure",
                "blocked_leg": copy.deepcopy(execution.get("blocked_leg")),
                "fatigue_medicine_used": _merge_usage_lists(
                    state.get("fatigue_medicine_used"),
                    execution_usage,
                ),
                "fatigue_medicine_use_count": int(state.get("fatigue_medicine_use_count") or 0)
                + execution_usage_count,
            }
        )
        if plan.get("snapshot_id"):
            snapshot_ids = list(state.get("snapshot_ids") or [])
            snapshot_ids.append(plan.get("snapshot_id"))
            state["snapshot_ids"] = snapshot_ids
        await state_store.set(run_key, state)
        summary = _summary_from_state(state)
        summary["run_key"] = run_key
        return summary

    if plan.get("status") == "ok" and route:
        route_start_index = len(state.get("route") or [])
        route_count = len(route)
        round_complete = bool(plan.get("round_complete"))
        round_summary = {
            "index": len(state.get("rounds") or []),
            "snapshot_id": plan.get("snapshot_id"),
            "round_complete": round_complete,
            "expected_profit": float(plan.get("expected_profit") or 0.0),
            "fatigue_used": int(plan.get("fatigue_used") or 0),
            "books_used": int(plan.get("books_used") or 0),
            "entry_route_count": int(plan.get("entry_route_count") or 0),
            "route_start_index": route_start_index,
            "route_count": route_count,
            "city_cycle": list(plan.get("city_cycle") or []),
        }
        all_route = list(state.get("route") or [])
        all_route.extend(copy.deepcopy(route))
        rounds = list(state.get("rounds") or [])
        rounds.append(round_summary)
        snapshot_ids = list(state.get("snapshot_ids") or [])
        if plan.get("snapshot_id"):
            snapshot_ids.append(plan.get("snapshot_id"))
        fatigue_used = int(state.get("fatigue_used") or 0) + int(plan.get("fatigue_used") or 0)
        remaining_fatigue = max(int(state.get("fatigue_budget") or 0) - fatigue_used, 0)
        state.update(
            {
                "status": "ok",
                "reason": None,
                "snapshot_id": plan.get("snapshot_id"),
                "snapshot_ids": snapshot_ids,
                "expected_profit": float(state.get("expected_profit") or 0.0)
                + float(plan.get("expected_profit") or 0.0),
                "fatigue_used": fatigue_used,
                "remaining_fatigue": remaining_fatigue,
                "books_used": int(state.get("books_used") or 0) + int(plan.get("books_used") or 0),
                "rounds_completed": int(state.get("rounds_completed") or 0) + (1 if round_complete else 0),
                "entry_route_count": int(plan.get("entry_route_count") or 0),
                "city_cycle": list(plan.get("city_cycle") or []),
                "rounds": rounds,
                "route": all_route,
                "current_city": str((route[-1] or {}).get("to_city") or state.get("current_city") or ""),
                "current_city_key": "",
                "should_continue": bool(round_complete and remaining_fatigue > 0),
                "fatigue_medicine_used": _merge_usage_lists(
                    state.get("fatigue_medicine_used"),
                    execution_usage,
                ),
                "fatigue_medicine_use_count": int(state.get("fatigue_medicine_use_count") or 0)
                + execution_usage_count,
            }
        )
    else:
        has_route = bool(state.get("route"))
        state.update(
            {
                "status": "ok" if has_route else "no_plan",
                "reason": None if has_route else (plan.get("reason") or "no_executable_plan"),
                "should_continue": False,
                "snapshot_id": plan.get("snapshot_id") or state.get("snapshot_id"),
            }
        )
        if plan.get("snapshot_id"):
            snapshot_ids = list(state.get("snapshot_ids") or [])
            snapshot_ids.append(plan.get("snapshot_id"))
            state["snapshot_ids"] = snapshot_ids

    await state_store.set(run_key, state)
    summary = _summary_from_state(state)
    summary["run_key"] = run_key
    return summary


@action_info(
    name="resonance.trade_loop_summary",
    public=True,
    read_only=True,
    description="Return one auto-cycle trade loop summary.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_loop_summary(
    run_key: str,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    state = await state_store.get(run_key, {})
    summary = _summary_from_state(state if isinstance(state, dict) else {})
    summary["run_key"] = run_key
    return summary


@action_info(
    name="resonance.trade_loop_cleanup",
    public=True,
    read_only=False,
    description="Remove one auto-cycle trade loop state.",
)
@requires_services(state_store="core/state_store")
async def resonance_trade_loop_cleanup(
    run_key: str,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if state_store is None:
        raise RuntimeError("state_store service is not available.")
    await state_store.delete(run_key)
    return {"success": True, "run_key": run_key}


@action_info(
    name="resonance.trade_assert_allowed_city",
    public=True,
    read_only=True,
    description="Assert target city key is in configured trade constraints whitelist.",
)
@requires_services(resonance_trade_planner="resonance_trade_planner")
def resonance_trade_assert_allowed_city(
    city_key: Optional[str] = None,
    city_id: Optional[str] = None,
    resonance_trade_planner: ResonanceTradePlannerService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_trade_planner).assert_allowed_city(
        city_key=city_key,
        city_id=city_id,
    )


@action_info(
    name="resonance.trade_simulate",
    public=True,
    read_only=True,
    description="Simulate Resonance rolling trade until stop condition.",
)
@requires_services(resonance_trade_planner="resonance_trade_planner")
def resonance_trade_simulate(
    start_city_id: str,
    fatigue_budget: int,
    book_budget: int,
    cargo_capacity: int,
    book_profit_threshold: float,
    available_city_ids: List[str],
    station_product_whitelist: Optional[Dict[str, List[str]]] = None,
    snapshot_id: Optional[str] = None,
    max_iterations: int = 128,
    resonance_trade_planner: ResonanceTradePlannerService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_trade_planner).simulate_until_stop(
        start_city_id=start_city_id,
        fatigue_budget=fatigue_budget,
        book_budget=book_budget,
        cargo_capacity=cargo_capacity,
        book_profit_threshold=book_profit_threshold,
        available_city_ids=available_city_ids,
        station_product_whitelist=station_product_whitelist,
        snapshot_id=snapshot_id,
        max_iterations=max_iterations,
    )
