"""Parameterized alternating one-way passenger trips for Resonance PC."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from typing import Any, Callable, Dict, List, Optional

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger

from ..services.city_shop_data_pc_service import ResonancePcCityShopDataService
from ..services.resonance_pc_market_data_service import ResonancePcMarketDataService
from ..services.resonance_pc_trade_planner_service import ResonancePcTradePlannerService
from .city_trade_flow_pc_actions import (
    CityTradeFlowError,
    _execute_city_trade_inside_current_city,
    resonance_pc_go_city_main_direct,
    resonance_pc_open_city_panel_from_main,
    resonance_pc_read_city_name_on_city_panel,
)
from .city_travel_pc_actions import IntercityDestinationError, resonance_pc_intercity_depart_and_wait
from .market_data_pc_actions import resonance_pc_market_refresh
from .passenger_pc_actions import (
    PassengerPcError,
    resonance_pc_enter_city_and_settle_passengers,
    resonance_pc_recruit_passengers_by_flyer,
)
from .trade_planner_pc_actions import resonance_pc_trade_plan_optimal_route


_PASSENGER_PROGRESS_EVENT = "task.resonance_pc_passenger_progress"
_PASSENGER_PROGRESS_SCHEMA = "resonance_pc.passenger_progress.v1"

_PASSENGER_TRADE_CARGO_CAPACITY = 650
_PASSENGER_TRADE_LEVEL = 20


def _build_passenger_route(
    *,
    city_a_id: str,
    city_b_id: str,
    city_shop_data: ResonancePcCityShopDataService,
    market_data: ResonancePcMarketDataService,
) -> Dict[str, Any]:
    first_id = str(city_a_id or "").strip()
    second_id = str(city_b_id or "").strip()
    if not first_id or not second_id:
        raise ValueError("passenger_city_a_id and passenger_city_b_id are required")
    if first_id == second_id:
        raise ValueError("passenger route cities must be different")

    fatigue_payload = market_data.get_all_travel_fatigue()
    city_names = {
        str(city_id): str(city_name)
        for city_id, city_name in dict(fatigue_payload.get("cities") or {}).items()
    }
    unknown = [city_id for city_id in (first_id, second_id) if city_id not in city_names]
    if unknown:
        raise ValueError(f"unknown passenger city ids: {', '.join(unknown)}")

    city_id_by_key: Dict[str, str] = {}
    identity_by_id: Dict[str, Dict[str, str]] = {}
    for city_id, city_name in city_names.items():
        try:
            resolved = city_shop_data.resolve_city(city_name)
        except Exception:  # noqa: BLE001 - cities without PC location data cannot be detected in UI
            continue
        identity = {
            "city_id": city_id,
            "city_key": str(resolved.get("city_key") or ""),
            "city_name": str(resolved.get("city_name") or city_name),
        }
        identity_by_id[city_id] = identity
        if identity["city_key"]:
            city_id_by_key[identity["city_key"]] = city_id

    missing_identity = [city_id for city_id in (first_id, second_id) if city_id not in identity_by_id]
    if missing_identity:
        raise ValueError(
            "passenger cities are missing PC location metadata: " + ", ".join(missing_identity)
        )

    trip_fatigue = market_data.get_travel_fatigue(first_id, second_id)
    route_by_id = {
        first_id: dict(identity_by_id[first_id]),
        second_id: dict(identity_by_id[second_id]),
    }
    return {
        "city_a_id": first_id,
        "city_b_id": second_id,
        "route_by_id": route_by_id,
        "route_id_by_key": {
            identity["city_key"]: city_id for city_id, identity in route_by_id.items()
        },
        "city_id_by_key": city_id_by_key,
        "opposite_city_id": {first_id: second_id, second_id: first_id},
        "trip_fatigue": int(trip_fatigue),
    }


class _PassengerProgressReporter:
    def __init__(self, event_bus: EventBus, cid: str, loop: asyncio.AbstractEventLoop):
        self._event_bus = event_bus
        self._cid = str(cid)
        self._loop = loop
        self._sequence = 0
        self._lock = threading.Lock()

    async def emit(self, stage: str, state: str, **fields: Any) -> None:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        payload = {
            "schema": _PASSENGER_PROGRESS_SCHEMA,
            "cid": self._cid,
            "sequence": sequence,
            "stage": str(stage),
            "state": str(state),
        }
        payload.update({key: value for key, value in fields.items() if value is not None})
        try:
            await self._event_bus.publish(Event(name=_PASSENGER_PROGRESS_EVENT, payload=payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("PC passenger progress event could not be published: %s", exc)

    def emit_from_worker(self, stage: str, state: str, **fields: Any) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(self.emit(stage, state, **fields), self._loop)
            future.result(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PC passenger worker progress could not be scheduled: %s", exc)


_ACTIVE_PROGRESS: contextvars.ContextVar[_PassengerProgressReporter | None] = contextvars.ContextVar(
    "resonance_pc_passenger_progress_reporter",
    default=None,
)


def _with_passenger_progress(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        event_bus = kwargs.get("event_bus")
        context = kwargs.get("context")
        cid = str(context.data.get("cid") or "") if isinstance(context, ExecutionContext) else ""
        reporter = (
            _PassengerProgressReporter(event_bus, cid, asyncio.get_running_loop())
            if event_bus is not None and cid
            else None
        )
        token = _ACTIVE_PROGRESS.set(reporter)
        try:
            if reporter is not None:
                await reporter.emit("task", "started")
            result = await func(*args, **kwargs)
            if reporter is not None:
                await reporter.emit(
                    "task",
                    "completed",
                    status=result.get("status"),
                    requires_manual_completion=bool(result.get("requires_manual_completion")),
                    data={"result": result},
                )
            return result
        except asyncio.CancelledError:
            if reporter is not None:
                await reporter.emit("task", "cancelled")
            raise
        except Exception as exc:
            if reporter is not None:
                await reporter.emit(
                    "task",
                    "failed",
                    data={"error_type": type(exc).__name__, "message": str(exc)},
                )
            raise
        finally:
            _ACTIVE_PROGRESS.reset(token)

    return wrapper


def _emit(stage: str, state: str, **fields: Any) -> None:
    reporter = _ACTIVE_PROGRESS.get()
    if reporter is not None:
        reporter.emit_from_worker(stage, state, **fields)


def _normalize_trip_count(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("trip_count must be an integer >= 1")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("trip_count must be an integer >= 1") from exc
    if normalized < 1 or str(normalized) != str(value).strip():
        raise ValueError("trip_count must be an integer >= 1")
    return normalized


def _merge_medicine_usage(target: Dict[str, int], rows: Any) -> None:
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        count = int(row.get("count") or 0)
        if name and count > 0:
            target[name] = int(target.get(name) or 0) + count


def _medicine_rows(usage: Dict[str, int]) -> List[Dict[str, Any]]:
    return [{"name": name, "count": count} for name, count in sorted(usage.items()) if count > 0]


def _result_base(trip_count: int, *, trade_during_trip: bool = False) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "not_started",
        "reason": None,
        "start_city": None,
        "end_city": None,
        "passenger_route": None,
        "trip_fatigue": 0,
        "route_fatigue": 0,
        "reposition_fatigue": 0,
        "requested_trips": trip_count,
        "completed_trips": 0,
        "completed_legs": [],
        "reposition_leg": None,
        "expected_fatigue_used": 0,
        "recruited_passengers": 0,
        "flyers_used": 0,
        "ticket_revenue": 0,
        "extra_revenue": 0,
        "total_revenue": 0,
        "trade_during_trip": bool(trade_during_trip),
        "trade_legs": [],
        "trade_expected_profit": 0.0,
        "trade_final_sale": None,
        "trade_warnings": [],
        "fatigue_medicine_used": [],
        "failure_stage": None,
        "requires_manual_completion": False,
        "loaded_destination": None,
        "page_state": "city_main",
    }


def _block(
    result: Dict[str, Any],
    reason: str,
    stage: str,
    *,
    requires_manual_completion: bool = False,
    loaded_destination: Optional[Dict[str, str]] = None,
    detail: Any = None,
) -> Dict[str, Any]:
    result.update(
        {
            "success": False,
            "status": "blocked",
            "reason": str(reason),
            "failure_stage": str(stage),
            "requires_manual_completion": bool(requires_manual_completion),
            "loaded_destination": loaded_destination,
        }
    )
    if detail is not None:
        result["failure_detail"] = detail
    _emit(
        stage,
        "blocked",
        requires_manual_completion=bool(requires_manual_completion),
        destination_city=(loaded_destination or {}).get("city_name"),
        data={"reason": reason, "detail": detail},
    )
    return result


def _read_current_city(
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
) -> Dict[str, Any]:
    resonance_pc_open_city_panel_from_main(app=app, ocr=ocr)
    try:
        current = resonance_pc_read_city_name_on_city_panel(
            app=app,
            ocr=ocr,
            resonance_pc_city_shop_data=city_shop_data,
        )
    finally:
        resonance_pc_go_city_main_direct(app=app, vision=vision)
    return current


def _exception_detail(exc: Exception) -> Dict[str, Any]:
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        detail = to_dict()
        if isinstance(detail, dict):
            return detail
    return {"error_type": type(exc).__name__, "message": str(exc)}


def _prepare_passenger_trade_plan(
    *,
    source_city_id: str,
    destination_city_id: str,
    route_by_id: Dict[str, Dict[str, str]],
    market_data: ResonancePcMarketDataService,
    trade_planner: ResonancePcTradePlannerService,
) -> Dict[str, Any]:
    """Refresh market data and accept only the configured one-way passenger route."""

    source = route_by_id[str(source_city_id)]
    destination = route_by_id[str(destination_city_id)]
    base = {
        "source_city": dict(source),
        "destination_city": dict(destination),
        "status": "skip_buy",
        "reason": None,
        "snapshot_id": None,
        "buy_products": [],
        "expected_profit": 0.0,
    }
    try:
        snapshot = resonance_pc_market_refresh(
            force=True,
            resonance_pc_market_data=market_data,
        )
    except Exception as exc:  # noqa: BLE001 - stale fallback must never become a buy plan
        base.update({"reason": "market_refresh_failed", "refresh_error": _exception_detail(exc)})
        return base

    base["market_refresh"] = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "stale": bool(snapshot.get("stale")),
        "stale_reason": snapshot.get("stale_reason"),
    }
    base["snapshot_id"] = snapshot.get("snapshot_id")
    if bool(snapshot.get("stale")):
        base["reason"] = "stale_market_rejected"
        return base
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    if not snapshot_id:
        base["reason"] = "market_snapshot_id_missing"
        return base

    try:
        plan = resonance_pc_trade_plan_optimal_route(
            fatigue_budget=market_data.get_travel_fatigue(source_city_id, destination_city_id),
            cargo_capacity=_PASSENGER_TRADE_CARGO_CAPACITY,
            book_budget=0,
            negotiation_budget=0,
            all_plan=0,
            trade_level=_PASSENGER_TRADE_LEVEL,
            available_city_ids=[str(source_city_id), str(destination_city_id)],
            city_prestige={"default": 20, "overrides": {}},
            product_unlocks={"mode": "all", "product_ids": []},
            current_city_id=str(source_city_id),
            snapshot_id=snapshot_id,
            resonance_pc_trade_planner=trade_planner,
        )
    except Exception as exc:  # noqa: BLE001 - planning failure means sell-only, never stale buying
        base.update({"reason": "trade_planning_failed", "planning_error": _exception_detail(exc)})
        return base

    base["planner_result"] = plan
    route = [row for row in (plan.get("route") or []) if isinstance(row, dict)]
    if str(plan.get("status") or "") != "ok" or float(plan.get("expected_profit") or 0.0) <= 0:
        base["reason"] = str(plan.get("reason") or "no_positive_profit_goods")
        return base
    if len(route) != 1:
        base["reason"] = "fixed_route_plan_mismatch"
        return base
    leg = route[0]
    if (
        str(leg.get("from_city_id") or "") != str(source_city_id)
        or str(leg.get("to_city_id") or "") != str(destination_city_id)
    ):
        base["reason"] = "fixed_route_plan_mismatch"
        return base
    products = [str(value).strip() for value in (leg.get("buy_products") or []) if str(value).strip()]
    if not products or float(leg.get("expected_profit") or 0.0) <= 0:
        base["reason"] = "no_positive_profit_goods"
        return base
    base.update(
        {
            "status": "planned",
            "reason": None,
            "buy_products": products,
            "expected_profit": float(leg.get("expected_profit") or 0.0),
            "route_leg": dict(leg),
        }
    )
    return base


def _execute_passenger_trade_at_city(
    *,
    current_city_id: str,
    destination_city_id: Optional[str],
    final_sale: bool,
    route_by_id: Dict[str, Dict[str, str]],
    app: Any,
    ocr: Any,
    vision: Any,
    controller: Any,
    city_shop_data: ResonancePcCityShopDataService,
    market_data: ResonancePcMarketDataService,
    trade_planner: ResonancePcTradePlannerService,
) -> Dict[str, Any]:
    current = route_by_id[str(current_city_id)]
    plan = None
    buy_products: List[str] = []
    if not final_sale:
        if destination_city_id is None:
            raise ValueError("destination_city_id is required before a passenger trade leg")
        plan = _prepare_passenger_trade_plan(
            source_city_id=str(current_city_id),
            destination_city_id=str(destination_city_id),
            route_by_id=route_by_id,
            market_data=market_data,
            trade_planner=trade_planner,
        )
        buy_products = list(plan.get("buy_products") or [])

    resonance_pc_open_city_panel_from_main(app=app, ocr=ocr)
    execution = _execute_city_trade_inside_current_city(
        current_city=current["city_name"],
        buy_products=buy_products,
        books_used=0,
        sell_raise_to_cap=False,
        buy_bargain_to_cap=False,
        app=app,
        ocr=ocr,
        vision=vision,
        controller=controller,
        city_shop_data=city_shop_data,
    )
    return {
        "success": True,
        "current_city": dict(current),
        "final_sale": bool(final_sale),
        "plan": plan,
        "buy_products": buy_products,
        "execution": execution,
    }


def _run_passenger_trips_sync(
    *,
    trip_count: int,
    passenger_city_a_id: str,
    passenger_city_b_id: str,
    trade_during_trip: bool,
    reposition_to_route: bool,
    use_fatigue_medicine: bool,
    allowed_fatigue_medicines: List[str],
    fatigue_medicine_max_uses: int,
    arrival_timeout_seconds: float,
    app: Any,
    ocr: Any,
    vision: Any,
    controller: Any,
    city_shop_data: ResonancePcCityShopDataService,
    market_data: ResonancePcMarketDataService,
    trade_planner: Optional[ResonancePcTradePlannerService],
) -> Dict[str, Any]:
    result = _result_base(trip_count, trade_during_trip=trade_during_trip)
    medicine_usage: Dict[str, int] = {}
    loaded_destination: Optional[Dict[str, str]] = None
    route_info = _build_passenger_route(
        city_a_id=passenger_city_a_id,
        city_b_id=passenger_city_b_id,
        city_shop_data=city_shop_data,
        market_data=market_data,
    )
    route_by_id = dict(route_info["route_by_id"])
    route_id_by_key = dict(route_info["route_id_by_key"])
    city_id_by_key = dict(route_info["city_id_by_key"])
    opposite_city_id = dict(route_info["opposite_city_id"])
    route_fatigue = int(route_info["trip_fatigue"]) * int(trip_count)
    result.update(
        {
            "passenger_route": {
                "city_a": dict(route_by_id[route_info["city_a_id"]]),
                "city_b": dict(route_by_id[route_info["city_b_id"]]),
            },
            "trip_fatigue": int(route_info["trip_fatigue"]),
            "route_fatigue": route_fatigue,
        }
    )

    _emit("resolve_start", "started")
    try:
        current = _read_current_city(app, ocr, vision, city_shop_data)
    except Exception as exc:  # noqa: BLE001 - converted into an expected operational result
        return _block(
            result,
            "current_city_unknown",
            "resolve_start",
            detail={"error_type": type(exc).__name__, "message": str(exc)},
        )
    current_key = str(current.get("city_key") or "")
    current_city_id = route_id_by_key.get(current_key)
    result["detected_start_city"] = {
        "city_key": current_key,
        "city_name": str(current.get("city_name") or ""),
    }

    if current_city_id is None:
        if not reposition_to_route:
            return _block(result, "outside_passenger_route", "resolve_start", detail=current)
        current_market_id = city_id_by_key.get(current_key)
        if current_market_id is None:
            return _block(result, "current_city_unknown", "reposition", detail=current)
        endpoint_fatigue = {
            endpoint_id: market_data.get_travel_fatigue(current_market_id, endpoint_id)
            for endpoint_id in route_by_id
        }
        start_id = min(
            endpoint_fatigue,
            key=lambda endpoint_id: (
                endpoint_fatigue[endpoint_id],
                endpoint_id != route_info["city_a_id"],
            ),
        )
        destination = dict(route_by_id[start_id])
        reposition_cost = endpoint_fatigue[start_id]
        _emit(
            "reposition",
            "started",
            source_city=str(current.get("city_name") or ""),
            destination_city=destination["city_name"],
        )
        try:
            travel = resonance_pc_intercity_depart_and_wait(
                to_city_name=destination["city_name"],
                enter_station_timeout_seconds=arrival_timeout_seconds,
                use_fatigue_medicine=use_fatigue_medicine,
                allowed_fatigue_medicines=allowed_fatigue_medicines,
                fatigue_medicine_max_uses=fatigue_medicine_max_uses,
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
            )
        except IntercityDestinationError as exc:
            return _block(result, exc.code, "reposition", detail=exc.to_dict())
        _merge_medicine_usage(medicine_usage, travel.get("fatigue_medicine_used"))
        if not travel.get("success"):
            result["fatigue_medicine_used"] = _medicine_rows(medicine_usage)
            return _block(result, str(travel.get("reason") or "travel_blocked"), "reposition", detail=travel)
        result["reposition_leg"] = {
            "from_city": str(current.get("city_name") or ""),
            "to_city": destination["city_name"],
            "expected_fatigue": reposition_cost,
            "endpoint_fatigue": {
                route_by_id[endpoint_id]["city_name"]: cost
                for endpoint_id, cost in endpoint_fatigue.items()
            },
            "travel": travel,
        }
        result["expected_fatigue_used"] += reposition_cost
        result["reposition_fatigue"] = int(reposition_cost)
        current_city_id = start_id
        _emit(
            "reposition",
            "completed",
            destination_city=destination["city_name"],
            expected_fatigue_used=result["expected_fatigue_used"],
        )

    assert current_city_id in route_by_id
    result["start_city"] = dict(route_by_id[current_city_id])
    expected_total = int(result["expected_fatigue_used"])
    expected_total += route_fatigue
    _emit(
        "resolve_start",
        "completed",
        source_city=route_by_id[current_city_id]["city_name"],
        expected_fatigue_total=expected_total,
    )

    total_legs = int(trip_count)
    for leg_index in range(total_legs):
        source = dict(route_by_id[current_city_id])
        destination_id = opposite_city_id[current_city_id]
        destination = dict(route_by_id[destination_id])
        trip_index = leg_index + 1
        progress_fields = {
            "trip_index": trip_index,
            "leg_index": leg_index + 1,
            "leg_count": total_legs,
            "source_city": source["city_name"],
            "destination_city": destination["city_name"],
            "expected_fatigue_total": expected_total,
        }

        if trade_during_trip:
            assert trade_planner is not None
            _emit("trade", "started", **progress_fields)
            try:
                trade_leg = _execute_passenger_trade_at_city(
                    current_city_id=current_city_id,
                    destination_city_id=destination_id,
                    final_sale=False,
                    route_by_id=route_by_id,
                    app=app,
                    ocr=ocr,
                    vision=vision,
                    controller=controller,
                    city_shop_data=city_shop_data,
                    market_data=market_data,
                    trade_planner=trade_planner,
                )
            except Exception as exc:  # noqa: BLE001 - UI failures are structured before recruitment
                return _block(
                    result,
                    exc.code if isinstance(exc, CityTradeFlowError) else "passenger_trade_failed",
                    "trade",
                    detail=_exception_detail(exc),
                )
            result["trade_legs"].append(trade_leg)
            trade_plan = trade_leg.get("plan") or {}
            result["trade_expected_profit"] += float(trade_plan.get("expected_profit") or 0.0)
            if trade_plan.get("reason"):
                result["trade_warnings"].append(
                    {
                        "leg_index": leg_index + 1,
                        "reason": str(trade_plan.get("reason")),
                    }
                )
            _emit(
                "trade",
                "completed",
                **progress_fields,
                data={
                    "buy_products": list(trade_leg.get("buy_products") or []),
                    "expected_profit": float(trade_plan.get("expected_profit") or 0.0),
                    "buy_skipped_reason": trade_plan.get("reason"),
                },
            )

        _emit("recruit", "started", **progress_fields)
        try:
            recruitment = resonance_pc_recruit_passengers_by_flyer(
                to_city_name=destination["city_name"],
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
            )
        except PassengerPcError as exc:
            result["fatigue_medicine_used"] = _medicine_rows(medicine_usage)
            return _block(result, exc.code, "recruit", detail=exc.to_dict())
        if not recruitment.get("success"):
            result["fatigue_medicine_used"] = _medicine_rows(medicine_usage)
            return _block(
                result,
                str(recruitment.get("reason") or "recruitment_failed"),
                "recruit",
                detail=recruitment,
            )
        loaded_destination = destination
        result["recruited_passengers"] += int(recruitment.get("recruited_passengers") or 0)
        result["flyers_used"] += int(recruitment.get("flyers_used") or 0)
        _emit(
            "recruit",
            "completed",
            **progress_fields,
            recruited_count=int(recruitment.get("recruited_passengers") or 0),
            seat_capacity=int(recruitment.get("seat_capacity") or 0),
        )

        _emit("travel", "started", **progress_fields)
        try:
            travel = resonance_pc_intercity_depart_and_wait(
                to_city_name=destination["city_name"],
                enter_station_timeout_seconds=arrival_timeout_seconds,
                use_fatigue_medicine=use_fatigue_medicine,
                allowed_fatigue_medicines=allowed_fatigue_medicines,
                fatigue_medicine_max_uses=fatigue_medicine_max_uses,
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
            )
        except IntercityDestinationError as exc:
            result["fatigue_medicine_used"] = _medicine_rows(medicine_usage)
            return _block(
                result,
                exc.code,
                "travel",
                requires_manual_completion=True,
                loaded_destination=loaded_destination,
                detail=exc.to_dict(),
            )
        _merge_medicine_usage(medicine_usage, travel.get("fatigue_medicine_used"))
        if not travel.get("success"):
            result["fatigue_medicine_used"] = _medicine_rows(medicine_usage)
            return _block(
                result,
                str(travel.get("reason") or "travel_blocked"),
                "travel",
                requires_manual_completion=True,
                loaded_destination=loaded_destination,
                detail=travel,
            )
        leg_fatigue = market_data.get_travel_fatigue(current_city_id, destination_id)
        result["expected_fatigue_used"] += leg_fatigue
        _emit(
            "travel",
            "completed",
            **progress_fields,
            expected_fatigue_used=result["expected_fatigue_used"],
        )

        _emit("settlement", "started", **progress_fields)
        try:
            settlement = resonance_pc_enter_city_and_settle_passengers(
                app=app,
                ocr=ocr,
                vision=vision,
            )
        except PassengerPcError as exc:
            result["fatigue_medicine_used"] = _medicine_rows(medicine_usage)
            return _block(
                result,
                exc.code,
                "settlement",
                requires_manual_completion=True,
                loaded_destination=loaded_destination,
                detail=exc.to_dict(),
            )

        loaded_destination = None
        for key in ("ticket_revenue", "extra_revenue", "total_revenue"):
            value = settlement.get(key)
            if value is not None:
                result[key] += int(value)
        completed_leg = {
            "leg_index": leg_index + 1,
            "trip_index": trip_index,
            "from_city": source,
            "to_city": destination,
            "expected_fatigue": leg_fatigue,
            "recruitment": recruitment,
            "travel": travel,
            "settlement": settlement,
        }
        result["completed_legs"].append(completed_leg)
        current_city_id = destination_id
        result["end_city"] = dict(destination)
        result["completed_trips"] = len(result["completed_legs"])
        _emit(
            "settlement",
            "completed",
            **progress_fields,
            leg_revenue=settlement.get("total_revenue"),
            total_revenue=result["total_revenue"],
            expected_fatigue_used=result["expected_fatigue_used"],
        )

    if trade_during_trip:
        assert trade_planner is not None
        _emit(
            "final_sale",
            "started",
            source_city=route_by_id[current_city_id]["city_name"],
            expected_fatigue_total=expected_total,
        )
        try:
            result["trade_final_sale"] = _execute_passenger_trade_at_city(
                current_city_id=current_city_id,
                destination_city_id=None,
                final_sale=True,
                route_by_id=route_by_id,
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
                city_shop_data=city_shop_data,
                market_data=market_data,
                trade_planner=trade_planner,
            )
        except Exception as exc:  # noqa: BLE001 - passengers are already settled
            return _block(
                result,
                exc.code if isinstance(exc, CityTradeFlowError) else "passenger_final_sale_failed",
                "final_sale",
                detail=_exception_detail(exc),
            )
        _emit(
            "final_sale",
            "completed",
            source_city=route_by_id[current_city_id]["city_name"],
            expected_fatigue_total=expected_total,
        )

    result.update(
        {
            "success": True,
            "status": "completed",
            "reason": None,
            "failure_stage": None,
            "requires_manual_completion": False,
            "loaded_destination": None,
            "fatigue_medicine_used": _medicine_rows(medicine_usage),
            "page_state": "city_main",
        }
    )
    return result


@action_info(
    name="resonance_pc.auto_passenger_trips_flow",
    public=True,
    read_only=False,
    description="Run configured alternating passenger trips from city-main UI.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
    controller="plans/aura_base/controller",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
    resonance_pc_market_data="resonance_pc_market_data",
    resonance_pc_trade_planner="resonance_pc_trade_planner",
    event_bus="core/event_bus",
)
@_with_passenger_progress
async def resonance_pc_auto_passenger_trips_flow(
    trip_count: int = 1,
    passenger_city_a_id: str = "11",
    passenger_city_b_id: str = "15",
    trade_during_trip: bool = True,
    reposition_to_route: bool = True,
    use_fatigue_medicine: bool = False,
    allowed_fatigue_medicines: Optional[List[str]] = None,
    fatigue_medicine_max_uses: int = 4,
    arrival_timeout_seconds: float = 1800.0,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    controller: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
    event_bus: EventBus | None = None,
    context: ExecutionContext | None = None,
) -> Dict[str, Any]:
    del event_bus, context
    normalized_trip_count = _normalize_trip_count(trip_count)
    city_a_id = str(passenger_city_a_id or "").strip()
    city_b_id = str(passenger_city_b_id or "").strip()
    if not city_a_id or not city_b_id:
        raise ValueError("passenger_city_a_id and passenger_city_b_id are required")
    if city_a_id == city_b_id:
        raise ValueError("passenger route cities must be different")
    if isinstance(fatigue_medicine_max_uses, bool) or int(fatigue_medicine_max_uses) < 0:
        raise ValueError("fatigue_medicine_max_uses must be an integer >= 0")
    if float(arrival_timeout_seconds) <= 0:
        raise ValueError("arrival_timeout_seconds must be > 0")
    if (
        app is None
        or ocr is None
        or vision is None
        or controller is None
        or resonance_pc_city_shop_data is None
        or resonance_pc_market_data is None
        or (bool(trade_during_trip) and resonance_pc_trade_planner is None)
    ):
        raise RuntimeError("passenger flow requires app/ocr/vision/controller/city/market services")

    return await asyncio.to_thread(
        _run_passenger_trips_sync,
        trip_count=normalized_trip_count,
        passenger_city_a_id=city_a_id,
        passenger_city_b_id=city_b_id,
        trade_during_trip=bool(trade_during_trip),
        reposition_to_route=bool(reposition_to_route),
        use_fatigue_medicine=bool(use_fatigue_medicine),
        allowed_fatigue_medicines=[
            str(value).strip() for value in (allowed_fatigue_medicines or []) if str(value).strip()
        ],
        fatigue_medicine_max_uses=int(fatigue_medicine_max_uses),
        arrival_timeout_seconds=float(arrival_timeout_seconds),
        app=app,
        ocr=ocr,
        vision=vision,
        controller=controller,
        city_shop_data=resonance_pc_city_shop_data,
        market_data=resonance_pc_market_data,
        trade_planner=resonance_pc_trade_planner,
    )
