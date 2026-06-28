"""Actions for Resonance trade planner service."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from packages.aura_core.api import action_info, requires_services

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
    current_city_key: str,
    fatigue_budget: int,
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
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        book_profit_threshold=book_profit_threshold,
        max_cycle_hops=max_cycle_hops,
        snapshot_id=snapshot_id,
    )


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
