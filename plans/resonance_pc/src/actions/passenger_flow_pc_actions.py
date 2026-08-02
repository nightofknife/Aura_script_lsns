"""Fixed Cape City <-> Lanxin City passenger round-trip flow for Resonance PC."""

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
from .city_trade_flow_pc_actions import (
    resonance_pc_go_city_main_direct,
    resonance_pc_open_city_panel_from_main,
    resonance_pc_read_city_name_on_city_panel,
)
from .city_travel_pc_actions import IntercityDestinationError, resonance_pc_intercity_depart_and_wait
from .passenger_pc_actions import (
    PassengerPcError,
    resonance_pc_enter_city_and_settle_passengers,
    resonance_pc_recruit_passengers_by_flyer,
)


_PASSENGER_PROGRESS_EVENT = "task.resonance_pc_passenger_progress"
_PASSENGER_PROGRESS_SCHEMA = "resonance_pc.passenger_progress.v1"

_ROUTE_BY_ID: Dict[str, Dict[str, str]] = {
    "11": {"city_id": "11", "city_key": "cape_city", "city_name": "海角城"},
    "15": {"city_id": "15", "city_key": "lanxin_city", "city_name": "岚心城"},
}
_CITY_ID_BY_KEY = {
    "shoggolith_city": "1",
    "brcl_outpost": "2",
    "freeport": "3",
    "clarity_data_center_administration_bureau": "4",
    "anita_weapon_research_institute": "5",
    "anita_energy_research_institute": "6",
    "wilderness_station": "7",
    "mander_mine": "8",
    "onederland": "9",
    "anita_rocket_base": "10",
    "cape_city": "11",
    "yunxiuqiao_base": "12",
    "confluence_tower": "13",
    "farstar_bridge": "14",
    "lanxin_city": "15",
    "qiyu_station": "16",
    "tatu_station": "17",
    "black_moon_amusement_park": "18",
    "gronru_city": "19",
    "vitilin_forest": "20",
}
_ROUTE_ID_BY_KEY = {value["city_key"]: city_id for city_id, value in _ROUTE_BY_ID.items()}
_OPPOSITE_CITY_ID = {"11": "15", "15": "11"}


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


def _normalize_round_trips(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("round_trips must be an integer >= 1")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("round_trips must be an integer >= 1") from exc
    if normalized < 1 or str(normalized) != str(value).strip():
        raise ValueError("round_trips must be an integer >= 1")
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


def _result_base(round_trips: int) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "not_started",
        "reason": None,
        "start_city": None,
        "end_city": None,
        "requested_round_trips": round_trips,
        "completed_round_trips": 0,
        "completed_legs": [],
        "reposition_leg": None,
        "expected_fatigue_used": 0,
        "recruited_passengers": 0,
        "flyers_used": 0,
        "ticket_revenue": 0,
        "extra_revenue": 0,
        "total_revenue": 0,
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


def _run_passenger_roundtrip_sync(
    *,
    round_trips: int,
    reposition_to_route: bool,
    preferred_start_city_id: str,
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
) -> Dict[str, Any]:
    result = _result_base(round_trips)
    medicine_usage: Dict[str, int] = {}
    loaded_destination: Optional[Dict[str, str]] = None

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
    current_city_id = _ROUTE_ID_BY_KEY.get(current_key)
    result["detected_start_city"] = {
        "city_key": current_key,
        "city_name": str(current.get("city_name") or ""),
    }

    if current_city_id is None:
        if not reposition_to_route:
            return _block(result, "outside_passenger_route", "resolve_start", detail=current)
        preferred_start_id = str(preferred_start_city_id or "11")
        if preferred_start_id not in _ROUTE_BY_ID:
            raise ValueError("preferred_start_city_id must be 11 or 15")
        current_market_id = _CITY_ID_BY_KEY.get(current_key)
        if current_market_id is None:
            return _block(result, "current_city_unknown", "reposition", detail=current)
        endpoint_fatigue = {
            endpoint_id: market_data.get_travel_fatigue(current_market_id, endpoint_id)
            for endpoint_id in _ROUTE_BY_ID
        }
        start_id = min(
            endpoint_fatigue,
            key=lambda endpoint_id: (
                endpoint_fatigue[endpoint_id],
                endpoint_id != preferred_start_id,
            ),
        )
        destination = dict(_ROUTE_BY_ID[start_id])
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
                _ROUTE_BY_ID[endpoint_id]["city_name"]: cost
                for endpoint_id, cost in endpoint_fatigue.items()
            },
            "travel": travel,
        }
        result["expected_fatigue_used"] += reposition_cost
        current_city_id = start_id
        _emit(
            "reposition",
            "completed",
            destination_city=destination["city_name"],
            expected_fatigue_used=result["expected_fatigue_used"],
        )

    assert current_city_id in _ROUTE_BY_ID
    result["start_city"] = dict(_ROUTE_BY_ID[current_city_id])
    expected_total = int(result["expected_fatigue_used"])
    route_leg_fatigue = market_data.get_travel_fatigue("11", "15")
    expected_total += route_leg_fatigue * round_trips * 2
    _emit(
        "resolve_start",
        "completed",
        source_city=_ROUTE_BY_ID[current_city_id]["city_name"],
        expected_fatigue_total=expected_total,
    )

    total_legs = round_trips * 2
    for leg_index in range(total_legs):
        source = dict(_ROUTE_BY_ID[current_city_id])
        destination_id = _OPPOSITE_CITY_ID[current_city_id]
        destination = dict(_ROUTE_BY_ID[destination_id])
        round_index = leg_index // 2 + 1
        progress_fields = {
            "round_index": round_index,
            "leg_index": leg_index + 1,
            "leg_count": total_legs,
            "source_city": source["city_name"],
            "destination_city": destination["city_name"],
            "expected_fatigue_total": expected_total,
        }

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
            "round_index": round_index,
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
        result["completed_round_trips"] = len(result["completed_legs"]) // 2
        _emit(
            "settlement",
            "completed",
            **progress_fields,
            leg_revenue=settlement.get("total_revenue"),
            total_revenue=result["total_revenue"],
            expected_fatigue_used=result["expected_fatigue_used"],
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
    name="resonance_pc.auto_passenger_roundtrip_flow",
    public=True,
    read_only=False,
    description="Run fixed Cape City <-> Lanxin City passenger round trips from city-main UI.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
    controller="plans/aura_base/controller",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
    resonance_pc_market_data="resonance_pc_market_data",
    event_bus="core/event_bus",
)
@_with_passenger_progress
async def resonance_pc_auto_passenger_roundtrip_flow(
    round_trips: int = 1,
    reposition_to_route: bool = True,
    preferred_start_city_id: str = "11",
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
    event_bus: EventBus | None = None,
    context: ExecutionContext | None = None,
) -> Dict[str, Any]:
    del event_bus, context
    normalized_round_trips = _normalize_round_trips(round_trips)
    preferred_id = str(preferred_start_city_id or "11").strip()
    if preferred_id not in _ROUTE_BY_ID:
        raise ValueError("preferred_start_city_id must be 11 or 15")
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
    ):
        raise RuntimeError("passenger flow requires app/ocr/vision/controller/city/market services")

    return await asyncio.to_thread(
        _run_passenger_roundtrip_sync,
        round_trips=normalized_round_trips,
        reposition_to_route=bool(reposition_to_route),
        preferred_start_city_id=preferred_id,
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
    )
