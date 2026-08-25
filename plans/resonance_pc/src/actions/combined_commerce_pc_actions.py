"""Combined freight and passenger orchestration for Resonance PC."""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List, Mapping, Optional

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.events import EventBus

from ..services.city_shop_data_pc_service import ResonancePcCityShopDataService
from ..services.resonance_pc_market_data_service import ResonancePcMarketDataService
from ..services.resonance_pc_trade_planner_service import ResonancePcTradePlannerService
from .city_trade_flow_pc_actions import (
    _preview_trade_plan_from_start_city,
    resonance_pc_auto_cycle_trade_flow,
)
from .passenger_flow_pc_actions import (
    _build_passenger_route,
    _normalize_trip_count,
    _read_current_city,
    resonance_pc_auto_passenger_trips_flow,
)
from .rubbish_recycling_pc_actions import is_rubbish_recycling_arrival


_ORDERS = {"trade_first", "passenger_first"}
_TRADE_INPUT_KEYS = {
    "cargo_capacity",
    "book_budget",
    "book_profit_threshold",
    "negotiation_max_attempts",
    "bargain_success_rates_bps",
    "bargain_step_bps",
    "raise_success_rates_bps",
    "raise_step_bps",
    "trade_level",
    "available_city_ids",
    "city_prestige",
    "product_unlocks",
    "active_events",
    "use_fatigue_medicine",
    "allowed_fatigue_medicines",
    "fatigue_medicine_max_uses",
    "arrival_timeout_seconds",
    "auto_cape_island_investment",
    "auto_rubbish_recycling",
}
_PREVIEW_INPUT_KEYS = _TRADE_INPUT_KEYS - {
    "negotiation_max_attempts",
    "use_fatigue_medicine",
    "allowed_fatigue_medicines",
    "fatigue_medicine_max_uses",
    "arrival_timeout_seconds",
    "auto_cape_island_investment",
    "auto_rubbish_recycling",
}
_PASSENGER_INPUT_KEYS = {
    "passenger_city_a_id",
    "passenger_city_b_id",
    "trip_count",
    "reposition_to_route",
    "use_fatigue_medicine",
    "allowed_fatigue_medicines",
    "fatigue_medicine_max_uses",
    "arrival_timeout_seconds",
}


def _normalized_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer > 0")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer > 0") from exc
    if normalized <= 0:
        raise ValueError(f"{name} must be an integer > 0")
    return normalized


def _filtered(source: Mapping[str, Any], allowed: set[str]) -> Dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in source.items() if key in allowed}


def _base_result(order: str, total_fatigue_budget: int) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "not_started",
        "reason": None,
        "failure_stage": None,
        "order": order,
        "preflight": {},
        "trade": None,
        "passenger": None,
        "total_fatigue_budget": total_fatigue_budget,
        "expected_fatigue_used": 0,
        "remaining_fatigue": total_fatigue_budget,
        "end_city_id": None,
        "page_state": "city_main",
    }


def _blocked(
    result: Dict[str, Any],
    reason: str,
    stage: str,
    *,
    detail: Any = None,
) -> Dict[str, Any]:
    used = 0
    for key in ("trade", "passenger"):
        child = result.get(key)
        if not isinstance(child, Mapping):
            continue
        try:
            used += max(int(child.get("expected_fatigue_used") or 0), 0)
        except (TypeError, ValueError):
            continue
    result.update(
        {
            "success": False,
            "status": "blocked",
            "reason": str(reason),
            "failure_stage": str(stage),
            "expected_fatigue_used": used,
            "remaining_fatigue": max(
                int(result.get("total_fatigue_budget") or 0) - used,
                0,
            ),
        }
    )
    if detail is not None:
        result["failure_detail"] = detail
    return result


def _exception_detail(exc: Exception) -> Dict[str, Any]:
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        detail = to_dict()
        if isinstance(detail, dict):
            return detail
    return {"error_type": type(exc).__name__, "message": str(exc)}


def _passenger_forecast(
    *,
    passenger_inputs: Mapping[str, Any],
    current: Mapping[str, Any],
    city_shop_data: ResonancePcCityShopDataService,
    market_data: ResonancePcMarketDataService,
) -> Dict[str, Any]:
    trip_count = _normalize_trip_count(passenger_inputs.get("trip_count", 1))
    route = _build_passenger_route(
        city_a_id=str(passenger_inputs.get("passenger_city_a_id") or "11"),
        city_b_id=str(passenger_inputs.get("passenger_city_b_id") or "15"),
        city_shop_data=city_shop_data,
        market_data=market_data,
    )
    route_by_id = dict(route["route_by_id"])
    current_key = str(current.get("city_key") or "")
    start_city_id = dict(route["route_id_by_key"]).get(current_key)
    reposition_fatigue = 0
    if start_city_id is None:
        if not bool(passenger_inputs.get("reposition_to_route", True)):
            raise ValueError("current city is outside the passenger route")
        current_city_id = dict(route["city_id_by_key"]).get(current_key)
        if current_city_id is None:
            raise LookupError("current city could not be mapped to the travel-fatigue table")
        endpoint_fatigue = {
            city_id: market_data.get_travel_fatigue(current_city_id, city_id)
            for city_id in route_by_id
        }
        start_city_id = min(
            endpoint_fatigue,
            key=lambda city_id: (
                endpoint_fatigue[city_id],
                city_id != route["city_a_id"],
            ),
        )
        reposition_fatigue = int(endpoint_fatigue[start_city_id])
    end_city_id = str(start_city_id)
    for _index in range(trip_count):
        end_city_id = str(route["opposite_city_id"][end_city_id])
    route_fatigue = int(route["trip_fatigue"]) * trip_count
    return {
        "detected_current_city": dict(current),
        "start_city": dict(route_by_id[str(start_city_id)]),
        "end_city": dict(route_by_id[end_city_id]),
        "trip_count": trip_count,
        "trip_fatigue": int(route["trip_fatigue"]),
        "route_fatigue": route_fatigue,
        "reposition_fatigue": reposition_fatigue,
        "expected_fatigue": route_fatigue + reposition_fatigue,
    }


def _trade_handoff_error(
    trade: Mapping[str, Any],
    *,
    allowed_end_city_ids: Optional[set[str]],
    auto_cape_island_investment: bool,
    auto_rubbish_recycling: bool,
) -> Optional[str]:
    if trade.get("success") is False or str(trade.get("status") or "") != "completed":
        return "trade task did not complete"
    route = [row for row in (trade.get("route") or []) if isinstance(row, dict)]
    execution = trade.get("execution") if isinstance(trade.get("execution"), dict) else {}
    end_city_id = str(
        (route[-1].get("to_city_id") if route else None)
        or trade.get("selected_end_city_id")
        or ""
    )
    if not route:
        return "trade task returned no route"
    if allowed_end_city_ids is not None and end_city_id not in allowed_end_city_ids:
        return "trade route did not end at a passenger endpoint"
    if int(execution.get("completed_leg_count") or 0) != len(route):
        return "trade route was not fully executed"
    final_sale = trade.get("final_sale") if isinstance(trade.get("final_sale"), dict) else {}
    if not final_sale or final_sale.get("success") is False:
        return "trade final sale did not complete"
    if str(final_sale.get("page_state") or "") != "city_main":
        return "trade final sale did not return to city main"
    if str(trade.get("page_state") or "") != "city_main":
        return "trade task did not return to city main"
    if auto_cape_island_investment:
        arrivals = sum(
            1
            for row in route
            if str(row.get("to_city_id") or "") == "11"
            or str(row.get("to_city") or "") == "海角城"
        )
        if int(execution.get("cape_island_triggered_count") or 0) != arrivals:
            return "trade cape-island investment stages did not complete"
    if auto_rubbish_recycling:
        expected = 1 if any(is_rubbish_recycling_arrival(row) for row in route) else 0
        if int(execution.get("rubbish_recycling_triggered_count") or 0) != expected:
            return "trade rubbish-recycling stage did not complete"
    return None


def _passenger_handoff_error(
    passenger: Mapping[str, Any],
    *,
    expected_trip_count: int,
    expected_end_city_id: Optional[str] = None,
) -> Optional[str]:
    if passenger.get("success") is False or str(passenger.get("status") or "") != "completed":
        return "passenger task did not complete"
    requested = int(passenger.get("requested_trips") or 0)
    completed = int(passenger.get("completed_trips") or 0)
    if requested != expected_trip_count or completed != expected_trip_count:
        return "passenger task did not complete every requested trip"
    if bool(passenger.get("requires_manual_completion")):
        return "passenger task still requires manual completion"
    if passenger.get("loaded_destination") is not None:
        return "passenger task ended with unsettled passengers"
    if str(passenger.get("page_state") or "") != "city_main":
        return "passenger task did not return to city main"
    end_city = passenger.get("end_city") if isinstance(passenger.get("end_city"), dict) else {}
    actual_end_city_id = str(end_city.get("city_id") or "")
    if expected_end_city_id is not None and actual_end_city_id != str(expected_end_city_id):
        return "passenger task ended at an unexpected city"
    return None


def _trade_end_city_id(trade: Mapping[str, Any]) -> str:
    route = [row for row in (trade.get("route") or []) if isinstance(row, dict)]
    return str(
        (route[-1].get("to_city_id") if route else None)
        or trade.get("selected_end_city_id")
        or ""
    )


def _passenger_end_city_id(passenger: Mapping[str, Any]) -> str:
    end_city = passenger.get("end_city") if isinstance(passenger.get("end_city"), dict) else {}
    return str(end_city.get("city_id") or "")


def _usage(result: Mapping[str, Any]) -> int:
    try:
        return max(int(result.get("expected_fatigue_used") or 0), 0)
    except (TypeError, ValueError):
        return 0


async def _run_trade(
    inputs: Mapping[str, Any],
    *,
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
    market_data: ResonancePcMarketDataService,
    trade_planner: ResonancePcTradePlannerService,
    state_store: StateStoreService,
    event_bus: EventBus,
    context: ExecutionContext,
    engine: ExecutionEngine,
) -> Dict[str, Any]:
    return await resonance_pc_auto_cycle_trade_flow(
        **dict(inputs),
        app=app,
        ocr=ocr,
        vision=vision,
        resonance_pc_city_shop_data=city_shop_data,
        resonance_pc_market_data=market_data,
        resonance_pc_trade_planner=trade_planner,
        state_store=state_store,
        event_bus=event_bus,
        context=context,
        engine=engine,
    )


async def _run_passenger(
    inputs: Mapping[str, Any],
    *,
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
    market_data: ResonancePcMarketDataService,
    trade_planner: ResonancePcTradePlannerService,
    event_bus: EventBus,
    context: ExecutionContext,
) -> Dict[str, Any]:
    return await resonance_pc_auto_passenger_trips_flow(
        **dict(inputs),
        app=app,
        ocr=ocr,
        vision=vision,
        resonance_pc_city_shop_data=city_shop_data,
        resonance_pc_market_data=market_data,
        resonance_pc_trade_planner=trade_planner,
        event_bus=event_bus,
        context=context,
    )


@action_info(
    name="resonance_pc.auto_combined_commerce_flow",
    public=True,
    read_only=False,
    description="Run one fatigue-budgeted freight and passenger workflow.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
    resonance_pc_market_data="resonance_pc_market_data",
    resonance_pc_trade_planner="resonance_pc_trade_planner",
    state_store="core/state_store",
    event_bus="core/event_bus",
)
async def resonance_pc_auto_combined_commerce_flow(
    order: str = "trade_first",
    total_fatigue_budget: int = 700,
    trade_inputs: Optional[Dict[str, Any]] = None,
    passenger_inputs: Optional[Dict[str, Any]] = None,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
    state_store: StateStoreService | None = None,
    event_bus: EventBus | None = None,
    context: ExecutionContext | None = None,
    engine: ExecutionEngine | None = None,
) -> Dict[str, Any]:
    normalized_order = str(order or "").strip().lower()
    try:
        total_fatigue = _normalized_positive_int(
            total_fatigue_budget,
            "total_fatigue_budget",
        )
    except ValueError as exc:
        return _blocked(
            _base_result(normalized_order, 0),
            "insufficient_trade_fatigue",
            "preflight",
            detail=_exception_detail(exc),
        )
    result = _base_result(normalized_order, total_fatigue)
    if normalized_order not in _ORDERS:
        return _blocked(result, "invalid_order", "preflight", detail={"order": order})
    if (
        app is None
        or ocr is None
        or vision is None
        or resonance_pc_city_shop_data is None
        or resonance_pc_market_data is None
        or resonance_pc_trade_planner is None
        or state_store is None
        or event_bus is None
        or context is None
        or engine is None
    ):
        raise RuntimeError("combined commerce requires app/ocr/vision/data/planner/state services")

    if trade_inputs is not None and not isinstance(trade_inputs, dict):
        return _blocked(result, "invalid_trade_inputs", "preflight")
    if passenger_inputs is not None and not isinstance(passenger_inputs, dict):
        return _blocked(result, "passenger_route_invalid", "preflight")
    raw_trade = copy.deepcopy(trade_inputs or {})
    raw_passenger = copy.deepcopy(passenger_inputs or {})
    effective_trade = _filtered(raw_trade, _TRADE_INPUT_KEYS)
    effective_passenger = _filtered(raw_passenger, _PASSENGER_INPUT_KEYS)
    effective_passenger["trade_during_trip"] = False
    try:
        trip_count = _normalize_trip_count(effective_passenger.get("trip_count", 1))
        route = _build_passenger_route(
            city_a_id=str(effective_passenger.get("passenger_city_a_id") or "11"),
            city_b_id=str(effective_passenger.get("passenger_city_b_id") or "15"),
            city_shop_data=resonance_pc_city_shop_data,
            market_data=resonance_pc_market_data,
        )
    except Exception as exc:  # noqa: BLE001 - public task returns structured preflight failures
        return _blocked(
            result,
            "passenger_route_invalid",
            "preflight",
            detail=_exception_detail(exc),
        )

    route_city_ids = [str(route["city_a_id"]), str(route["city_b_id"])]
    raw_available = effective_trade.get("available_city_ids")
    available_city_ids = (
        {str(city_id) for city_id in raw_available}
        if isinstance(raw_available, list)
        else {
            str(city_id)
            for city_id in dict(
                resonance_pc_market_data.get_all_travel_fatigue().get("cities") or {}
            )
        }
    )
    unavailable = [city_id for city_id in route_city_ids if city_id not in available_city_ids]
    if unavailable:
        return _blocked(
            result,
            "passenger_endpoint_unavailable",
            "preflight",
            detail={"unavailable_city_ids": unavailable},
        )

    route_fatigue = int(route["trip_fatigue"]) * trip_count
    result["preflight"] = {
        "route_city_ids": route_city_ids,
        "trip_count": trip_count,
        "trip_fatigue": int(route["trip_fatigue"]),
        "route_fatigue": route_fatigue,
    }
    if normalized_order == "trade_first":
        trade_budget = total_fatigue - route_fatigue
        if trade_budget <= 0:
            return _blocked(
                result,
                "insufficient_trade_fatigue",
                "preflight",
                detail={"passenger_fatigue": route_fatigue, "trade_fatigue": trade_budget},
            )
        effective_trade.update(
            fatigue_budget=trade_budget,
            required_end_city_ids=route_city_ids,
        )
        effective_passenger["reposition_to_route"] = False
        result["preflight"].update(
            passenger_fatigue=route_fatigue,
            trade_fatigue=trade_budget,
        )
        try:
            trade = await _run_trade(
                effective_trade,
                app=app,
                ocr=ocr,
                vision=vision,
                city_shop_data=resonance_pc_city_shop_data,
                market_data=resonance_pc_market_data,
                trade_planner=resonance_pc_trade_planner,
                state_store=state_store,
                event_bus=event_bus,
                context=context,
                engine=engine,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return _blocked(result, "trade_failed", "trade", detail=_exception_detail(exc))
        result["trade"] = trade
        trade_error = _trade_handoff_error(
            trade,
            allowed_end_city_ids=set(route_city_ids),
            auto_cape_island_investment=bool(
                effective_trade.get("auto_cape_island_investment", False)
            ),
            auto_rubbish_recycling=bool(
                effective_trade.get("auto_rubbish_recycling", True)
            ),
        )
        if trade_error:
            reason = "trade_failed" if str(trade.get("status") or "") != "completed" else "trade_handoff_invalid"
            stage = "trade" if reason == "trade_failed" else "trade_handoff"
            return _blocked(result, reason, stage, detail={"message": trade_error})
        try:
            passenger = await _run_passenger(
                effective_passenger,
                app=app,
                ocr=ocr,
                vision=vision,
                city_shop_data=resonance_pc_city_shop_data,
                market_data=resonance_pc_market_data,
                trade_planner=resonance_pc_trade_planner,
                event_bus=event_bus,
                context=context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return _blocked(result, "passenger_failed", "passenger", detail=_exception_detail(exc))
        result["passenger"] = passenger
        passenger_error = _passenger_handoff_error(passenger, expected_trip_count=trip_count)
        if passenger_error:
            reason = (
                "passenger_failed"
                if str(passenger.get("status") or "") != "completed"
                else "passenger_handoff_invalid"
            )
            stage = "passenger" if reason == "passenger_failed" else "passenger_handoff"
            return _blocked(result, reason, stage, detail={"message": passenger_error})
        result["end_city_id"] = _passenger_end_city_id(passenger)
    else:
        try:
            current = await asyncio.to_thread(
                _read_current_city,
                app,
                ocr,
                vision,
                resonance_pc_city_shop_data,
            )
            forecast = _passenger_forecast(
                passenger_inputs=effective_passenger,
                current=current,
                city_shop_data=resonance_pc_city_shop_data,
                market_data=resonance_pc_market_data,
            )
        except Exception as exc:  # noqa: BLE001
            return _blocked(result, "current_city_unknown", "preflight", detail=_exception_detail(exc))
        passenger_fatigue = int(forecast["expected_fatigue"])
        trade_budget = total_fatigue - passenger_fatigue
        result["preflight"].update(
            passenger_forecast=forecast,
            passenger_fatigue=passenger_fatigue,
            trade_fatigue=trade_budget,
        )
        if trade_budget <= 0:
            return _blocked(
                result,
                "insufficient_trade_fatigue",
                "preflight",
                detail={"passenger_fatigue": passenger_fatigue, "trade_fatigue": trade_budget},
            )
        preview_inputs = _filtered(effective_trade, _PREVIEW_INPUT_KEYS)
        try:
            preview = await _preview_trade_plan_from_start_city(
                start_city_id=str(forecast["end_city"]["city_id"]),
                fatigue_budget=trade_budget,
                resonance_pc_market_data=resonance_pc_market_data,
                resonance_pc_trade_planner=resonance_pc_trade_planner,
                reporter=None,
                **preview_inputs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return _blocked(
                result,
                "trade_preflight_no_plan",
                "preflight",
                detail=_exception_detail(exc),
            )
        result["preflight"]["trade_preview"] = preview
        if str(preview.get("status") or "") != "ok" or not list(preview.get("route") or []):
            return _blocked(
                result,
                "trade_preflight_no_plan",
                "preflight",
                detail={"status": preview.get("status"), "reason": preview.get("reason")},
            )
        try:
            passenger = await _run_passenger(
                effective_passenger,
                app=app,
                ocr=ocr,
                vision=vision,
                city_shop_data=resonance_pc_city_shop_data,
                market_data=resonance_pc_market_data,
                trade_planner=resonance_pc_trade_planner,
                event_bus=event_bus,
                context=context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return _blocked(result, "passenger_failed", "passenger", detail=_exception_detail(exc))
        result["passenger"] = passenger
        expected_end_city_id = str(forecast["end_city"]["city_id"])
        passenger_error = _passenger_handoff_error(
            passenger,
            expected_trip_count=trip_count,
            expected_end_city_id=expected_end_city_id,
        )
        if passenger_error:
            reason = (
                "passenger_failed"
                if str(passenger.get("status") or "") != "completed"
                else "passenger_handoff_invalid"
            )
            stage = "passenger" if reason == "passenger_failed" else "passenger_handoff"
            return _blocked(result, reason, stage, detail={"message": passenger_error})
        actual_passenger_fatigue = _usage(passenger)
        if actual_passenger_fatigue != passenger_fatigue:
            return _blocked(
                result,
                "passenger_forecast_mismatch",
                "passenger_handoff",
                detail={
                    "predicted_fatigue": passenger_fatigue,
                    "actual_fatigue": actual_passenger_fatigue,
                },
            )
        trade_budget = total_fatigue - actual_passenger_fatigue
        if trade_budget <= 0:
            return _blocked(
                result,
                "insufficient_trade_fatigue",
                "passenger_handoff",
                detail={"passenger_fatigue": actual_passenger_fatigue},
            )
        effective_trade.update(fatigue_budget=trade_budget)
        effective_trade.pop("required_end_city_ids", None)
        try:
            trade = await _run_trade(
                effective_trade,
                app=app,
                ocr=ocr,
                vision=vision,
                city_shop_data=resonance_pc_city_shop_data,
                market_data=resonance_pc_market_data,
                trade_planner=resonance_pc_trade_planner,
                state_store=state_store,
                event_bus=event_bus,
                context=context,
                engine=engine,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return _blocked(result, "trade_failed", "trade", detail=_exception_detail(exc))
        result["trade"] = trade
        if str(trade.get("status") or "") != "completed" or trade.get("success") is False:
            reason = (
                "post_passenger_trade_no_plan"
                if str(trade.get("status") or "") not in {"blocked", "error", "failed"}
                else "trade_failed"
            )
            return _blocked(
                result,
                reason,
                "trade",
                detail={"status": trade.get("status"), "reason": trade.get("reason")},
            )
        trade_error = _trade_handoff_error(
            trade,
            allowed_end_city_ids=None,
            auto_cape_island_investment=bool(
                effective_trade.get("auto_cape_island_investment", False)
            ),
            auto_rubbish_recycling=bool(
                effective_trade.get("auto_rubbish_recycling", True)
            ),
        )
        if trade_error:
            return _blocked(
                result,
                "trade_handoff_invalid",
                "trade_handoff",
                detail={"message": trade_error},
            )
        result["end_city_id"] = _trade_end_city_id(trade)

    trade_result = result.get("trade") if isinstance(result.get("trade"), dict) else {}
    passenger_result = (
        result.get("passenger") if isinstance(result.get("passenger"), dict) else {}
    )
    used = _usage(trade_result) + _usage(passenger_result)
    result.update(
        {
            "success": True,
            "status": "completed",
            "reason": None,
            "failure_stage": None,
            "expected_fatigue_used": used,
            "remaining_fatigue": total_fatigue - used,
            "page_state": "city_main",
        }
    )
    return result
