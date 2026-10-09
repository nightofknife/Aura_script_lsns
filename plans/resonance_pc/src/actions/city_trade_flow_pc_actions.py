"""City-panel trade UI actions and auto-cycle trade flow for ResonancePc."""

from __future__ import annotations

import time
import asyncio
import contextvars
import functools
import math
import threading
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.engine.action_injector import ActionInjector
from packages.aura_core.config.template import TemplateRenderer
from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ..services.city_shop_data_pc_service import ResonancePcCityShopDataService, CityShopDataError
from ..services.resonance_pc_market_data_service import ResonancePcMarketDataService
from ..services.resonance_pc_trade_exact_solver import (
    expected_fatigue_to_cap,
    trade_solver_progress,
)
from ..services.resonance_pc_trade_planner_service import ResonancePcTradePlannerService
from .cape_island_investment_pc_actions import (
    resonance_pc_execute_cape_island_investment_from_city_panel,
)
from .trade_goods_investment_pc_actions import (
    MODE_TARGETS,
    execute_trade_goods_investment_from_shop,
    normalize_investment_mode,
)
from .city_travel_pc_actions import resonance_pc_intercity_depart_and_wait
from ._operation_progress import (
    observe_operation, observe_worker_future, operation_progress, utc_timestamp,
)
from ._depart_button_vision import DepartButtonError, probe_depart_button, wait_depart_button
from ._city_panel_vision import CityPanelVisionError, open_city_panel, wait_city
from .market_data_pc_actions import resonance_pc_market_refresh
from .purchase_book_pc_actions import resonance_pc_use_purchase_books
from .rubbish_recycling_pc_actions import (
    is_rubbish_recycling_arrival,
    resonance_pc_execute_rubbish_recycling_from_city_panel,
)
from ._sparkling_water_policy import validate_recovery_snapshot
from ._freight_recovery_policy import (
    select_last_water_arrival, estimate_remaining_consumption,
    plan_water_use, validate_bento_priority,
)
from ._player_data_persistence import load_pc_user_info
from ._trade_buy_selection import (
    TradeBuySelectionError, load_product_templates, select_buy_products,
)
from ._freight_contract import (
    fixed_start_stop, integer, normalize_planning_inputs, plan_view, planning_event_data, start_mismatch,
)
from .sparkling_water_pc_actions import (
    resonance_pc_drink_sparkling_water_from_city_panel, _water_counts,
)
from .trade_negotiation_pc_actions import (
    DEFAULT_NEGOTIATION_MAX_ATTEMPTS,
    MAX_NEGOTIATION_MAX_ATTEMPTS,
    NegotiationExecutionError,
    execute_bargain_to_cap,
    execute_raise_to_cap,
)
from .trade_planner_pc_actions import (
    resonance_pc_trade_plan_optimal_route,
    resonance_pc_trade_route_execution_cleanup,
    resonance_pc_trade_route_execution_init,
    resonance_pc_trade_route_execution_summary,
    resonance_pc_trade_route_execution_update,
)


class CityTradeFlowError(RuntimeError):
    """Structured UI flow error for city trade actions."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_TRADE_PROGRESS_EVENT = "task.resonance_pc_trade_progress"
_TRADE_PROGRESS_SCHEMA = "resonance_pc.trade_progress.v1"

class _TradeProgressReporter:
    def __init__(self, event_bus: EventBus, cid: str, loop: asyncio.AbstractEventLoop,
                 initial_sequence: int = 0):
        self._event_bus = event_bus
        self._cid = str(cid)
        self._loop = loop
        self._sequence = initial_sequence
        self._lock = threading.Lock()
        self.fields: Dict[str, Any] = {}
        self.resources: Dict[str, Any] = {"confirmed_books_used": 0,
                                         "confirmed_negotiation_fatigue": 0,
                                         "actual_fatigue": None, "actual_profit": None}
        self._total_units: Optional[int] = None
        self._completed_units: set[Tuple[Any, ...]] = set()
        self._phase_keys: set[Tuple[Any, ...]] = set()
        self.route_revision = 0
        self.operation_fields: Dict[str, Any] = {}
        self.operation_stage = "task"

    @property
    def sequence(self) -> int:
        return self._sequence

    async def emit(self, stage: str, state: str, **fields: Any) -> None:
        if not (fields.get("data") or {}).get("operation"):
            self.operation_fields = {key: value for key, value in fields.items() if key != "data"}
            self.operation_stage = stage
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        payload = {
            "schema": _TRADE_PROGRESS_SCHEMA,
            "cid": self._cid,
            "sequence": sequence,
            "stage": str(stage),
            "state": str(state),
            "timestamp": utc_timestamp(),
        }
        payload.update(self.fields)
        payload.update({key: value for key, value in fields.items() if value is not None})
        data = dict(payload.get("data") or {})
        is_operation = isinstance(data.get("operation"), dict)
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        planning_status = summary.get("planning_status") or summary.get("status")
        if (stage == "planning" and state == "completed" and data.get("city_visits")
                and planning_status in {None, "ok", "planned"} and not is_operation):
            self.route_revision += 1
            self._total_units = (1 + len((data.get("reposition") or {}).get("route") or [])
                                 + sum(phase["status"] == "waiting"
                                       for visit in data["city_visits"] for phase in visit["phases"]))
            self._completed_units = {("preparation",)}
            self._phase_keys = {(phase["key"], visit["city_index"])
                                for visit in data["city_visits"] for phase in visit["phases"]
                                if phase["status"] == "waiting"}
        if self._total_units is not None:
            if stage == "reposition" and state == "completed" and not is_operation:
                self._completed_units.add((stage, (data.get("reposition") or {}).get("leg_index")))
            elif stage in {"arrival", "investment", "trade_goods_investment", "rubbish_recycling", "sparkling_water",
                           "sell", "books", "buy", "travel", "final_sale", "bento"} and state in {"completed", "skipped"} and not is_operation:
                key = (stage, payload.get("city_index"))
                if key in self._phase_keys:
                    self._completed_units.add(key)
                if stage == "arrival" and state == "completed" and type(payload.get("city_index")) is int:
                    self._completed_units.add(("travel", payload["city_index"] - 1))
            completed = len(self._completed_units)
            if stage == "task" and state == "completed" and not is_operation:
                completed = self._total_units
            data["progress"] = {"completed_units": min(completed, self._total_units),
                                "total_units": self._total_units}
        data.setdefault("resources", dict(self.resources))
        data["route_revision"] = self.route_revision
        payload["data"] = data
        try:
            await self._event_bus.publish(Event(name=_TRADE_PROGRESS_EVENT, payload=payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("PC trade progress event could not be published: %s", exc)

    def emit_from_worker(self, stage: str, state: str, **fields: Any) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(self.emit(stage, state, **fields), self._loop)
            if (fields.get("data") or {}).get("operation"):
                future.add_done_callback(observe_worker_future)
            else:
                future.result(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PC trade worker progress could not be scheduled: %s", exc)


_ACTIVE_PROGRESS_REPORTER: contextvars.ContextVar[_TradeProgressReporter | None] = contextvars.ContextVar(
    "resonance_pc_trade_progress_reporter",
    default=None,
)
_WORKER_PROGRESS_CONTEXT: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "resonance_pc_trade_worker_context",
    default={},
)


def _with_trade_progress(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        event_bus = kwargs.get("event_bus")
        context = kwargs.get("context")
        cid = ""
        if isinstance(context, ExecutionContext):
            cid = str(context.data.get("cid") or "")
        reporter = None
        if event_bus is not None and cid:
            reporter = _TradeProgressReporter(event_bus, cid, asyncio.get_running_loop())
        token = _ACTIVE_PROGRESS_REPORTER.set(reporter)
        if reporter is not None:
            reporter.fields.update(trade_mode=kwargs.get("trade_mode", "profit"),
                                   request_kind="preview" if "preview" in func.__name__ else "run")
        try:
            if reporter is not None:
                await reporter.emit("task", "started")
            def operation_observer(operation: Dict[str, Any]) -> None:
                stage = operation["key"].split(".", 1)[0]
                if stage == "travel" and reporter.operation_stage == "reposition":
                    stage = "reposition"
                _report_worker(stage, "progress", data={"operation": operation})

            with operation_progress(operation_observer if reporter is not None else None):
                result = await func(*args, **kwargs)
            if reporter is not None:
                if result.get("bento_pending"):
                    result["bento_progress_sequence"] = reporter.sequence
                    result["bento_progress_checkpoint"] = {
                        "total_units": reporter._total_units,
                        "completed_units": [list(key) for key in reporter._completed_units],
                        "phase_keys": [list(key) for key in reporter._phase_keys],
                        "route_revision": reporter.route_revision,
                    }
                else:
                    failed = (result.get("success") is False or
                              str(result.get("status") or "").lower() in {"failed", "blocked", "error", "cancelled"})
                    terminal = result.get("status") if result.get("status") in {"blocked", "cancelled"} else "failed" if failed else "completed"
                    await reporter.emit("task", terminal,
                                        data={"status": result.get("status")})
            return result
        except Exception as exc:
            if reporter is not None:
                await reporter.emit(
                    "task",
                    "failed",
                    data={"error_type": type(exc).__name__, "message": str(exc)},
                )
            raise
        finally:
            _ACTIVE_PROGRESS_REPORTER.reset(token)

    return wrapper


def _report_worker(stage: str, state: str, **fields: Any) -> None:
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    if reporter is None:
        return
    context = dict(_WORKER_PROGRESS_CONTEXT.get() or reporter.operation_fields)
    context.update(fields)
    if stage == "negotiation" and state == "completed":
        used = (fields.get("data") or {}).get("actual_fatigue_used")
        if type(used) is int:
            reporter.resources["confirmed_negotiation_fatigue"] += used
    reporter.emit_from_worker(stage, state, **context)


_SHOP_MENU_REGION = [720, 280, 280, 420]
_BUY_BUTTON_REGION = [1000, 630, 140, 50]
_BUY_CONFIRM_PANEL_REGION = [850, 80, 180, 60]
_BUY_CONFIRM_BUTTON_REGION = [900, 620, 330, 70]
_SELL_ALL_REGION = [1140, 80, 110, 50]
_SELL_ALL_TEMPLATE = "templates/trade_sell_all_button.png"
_SELL_BUTTON_REGION = [1000, 630, 120, 40]

_BACK_POINT = (82, 37)
_CITY_MAIN_POINT = (198, 37)
_BACK_BUTTON_TEMPLATE = "templates/nav_back_button.png"
_CITY_MAIN_BUTTON_TEMPLATE = "templates/nav_city_main_button.png"
_BACK_BUTTON_REGION = [0, 0, 170, 80]
_CITY_MAIN_BUTTON_REGION = [140, 0, 130, 80]
_NAV_BUTTON_THRESHOLD = 0.86
_NAV_BUTTON_TIMEOUT_SEC = 3.0
_NAV_BUTTON_INTERVAL_SEC = 0.4
_SHOP_MENU_READY_TEMPLATE = "templates/trade_shop_menu_ready.png"
_SHOP_MENU_READY_REGION = [720, 350, 220, 120]
_SHOP_MENU_READY_THRESHOLD = 0.86
_SHOP_MENU_READY_TIMEOUT_SEC = 15.0
_SHOP_MENU_READY_INTERVAL_SEC = 0.3
_SHOP_MENU_READY_STABLE_MATCHES = 2
_SHOP_NODE_X = 1160
_SHOP_NODE_FIRST_Y = 324
_SHOP_NODE_GAP_Y = 83
_SHOP_ENTRY_SETTLE_SEC = 2.0
_SETTLEMENT_EXIT_POINT = (640, 620)
_BUY_SCROLL_START = (670, 450)
_BUY_SCROLL_END = (670, 200)

_BUY_SETTLEMENT = {
    "template": "templates/buy_settlement_scale_badge.png",
    "region": [520, 240, 320, 320],
}
_SELL_SETTLEMENT = {
    "template": "templates/sell_settlement_scale_badge.png",
    "region": [930, 240, 300, 300],
}


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    raise CityTradeFlowError(code=code, message=message, detail=detail)


def _strict_integer(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        normalized = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if normalized.denominator != 1:
        raise ValueError(f"{name} must be an integer")
    return int(normalized)


def _coerce_region(region: Any) -> Tuple[int, int, int, int]:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        _raise_error("invalid_region", "region must be [x, y, w, h]", {"region": region})
    return (int(region[0]), int(region[1]), int(region[2]), int(region[3]))


def _normalize_text(text: Any) -> str:
    import re

    return re.sub(r"[\s\u3000\|:：,，。.!！?？（）()\[\]【】<>《》'\"`~\-]+", "", str(text)).lower()


def _capture_text_items(app: Any, ocr: Any, region: List[int] | Tuple[int, int, int, int]) -> List[Dict[str, Any]]:
    started_at = time.monotonic()
    region_tuple = _coerce_region(region)
    capture = app.capture(rect=region_tuple)
    if not capture.success:
        _raise_error("capture_failed", "failed to capture screen region", {"region": list(region_tuple)})
    multi = ocr.recognize_all(source_image=capture.image)
    items: List[Dict[str, Any]] = []
    observations: List[Dict[str, Any]] = []
    for item in getattr(multi, "results", []) or []:
        text = str(getattr(item, "text", "") or "")
        center = getattr(item, "center_point", None)
        rect = getattr(item, "rect", None)
        payload: Dict[str, Any] = {
            "text": text,
            "norm_text": _normalize_text(text),
            "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
        }
        if center and len(center) == 2:
            payload["center"] = [int(region_tuple[0] + int(center[0])), int(region_tuple[1] + int(center[1]))]
        if rect and len(rect) == 4:
            payload["rect"] = [
                int(region_tuple[0] + int(rect[0])),
                int(region_tuple[1] + int(rect[1])),
                int(rect[2]),
                int(rect[3]),
            ]
        observations.append({**payload, "ignored_empty_normalized": not bool(payload["norm_text"])})
        if payload["norm_text"]:
            items.append(payload)
    logger.info(
        "[TradeOCR] region=%s elapsed_sec=%.3f observations=%s context=%s",
        list(region_tuple), time.monotonic() - started_at,
        observations, dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    items.sort(key=lambda row: float(row.get("confidence") or 0.0), reverse=True)
    return items


def _find_text_hit(
    app: Any,
    ocr: Any,
    text: str,
    region: List[int] | Tuple[int, int, int, int],
    *,
    match_mode: str = "contains",
) -> Optional[Dict[str, Any]]:
    wanted = _normalize_text(text)
    if not wanted:
        return None
    for item in _capture_text_items(app, ocr, region):
        norm = str(item.get("norm_text") or "")
        if not norm:
            continue
        if match_mode == "exact":
            matched = norm == wanted
        else:
            matched = wanted in norm or norm in wanted
        if matched and isinstance(item.get("center"), list) and len(item["center"]) == 2:
            return item
    return None


def _wait_for_text_hit(
    app: Any,
    ocr: Any,
    texts: List[str] | Tuple[str, ...],
    region: List[int] | Tuple[int, int, int, int],
    *,
    timeout_sec: float = 3.0,
    interval_sec: float = 0.3,
) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    options = list(texts)
    while True:
        _check_trade_cancelled()
        for text in options:
            hit = _find_text_hit(app, ocr, text, region)
            if hit is not None:
                hit["marker"] = text
                return hit
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(float(interval_sec), 0.05))


def _click_hit(app: Any, hit: Dict[str, Any]) -> Dict[str, Any]:
    center = hit.get("center")
    if not isinstance(center, list) or len(center) != 2:
        return {"clicked": False, "reason": "missing_center", "hit": hit}
    x, y = int(center[0]), int(center[1])
    _check_trade_cancelled()
    app.click(x=x, y=y)
    return {"clicked": True, "x": x, "y": y, "text": hit.get("text"), "marker": hit.get("marker")}


def _check_trade_cancelled() -> None:
    if is_current_task_cancel_requested():
        _raise_error("trade_cancelled", "Trade input cancelled", {})


def _wait_and_click_text(
    app: Any,
    ocr: Any,
    texts: List[str] | Tuple[str, ...],
    region: List[int] | Tuple[int, int, int, int],
    *,
    timeout_sec: float = 3.0,
    interval_sec: float = 0.3,
) -> Dict[str, Any]:
    hit = _wait_for_text_hit(app, ocr, texts, region, timeout_sec=timeout_sec, interval_sec=interval_sec)
    if hit is None:
        return {"clicked": False, "reason": "text_not_found", "texts": list(texts), "region": list(region)}
    return _click_hit(app, hit)


def _match_template(
    app: Any,
    vision: Any,
    template: str,
    region: List[int] | Tuple[int, int, int, int],
    threshold: float,
) -> Dict[str, Any]:
    region_tuple = _coerce_region(region)
    from pathlib import Path

    template_path = Path(__file__).resolve().parents[2] / template
    capture = app.capture(rect=region_tuple)
    if not capture.success:
        return {"found": False, "template": template, "region": list(region_tuple), "reason": "capture_failed"}
    match = vision.find_template(
        source_image=capture.image,
        template_image=str(template_path),
        threshold=float(threshold),
        use_grayscale=True,
    )
    center = getattr(match, "center_point", None)
    result: Dict[str, Any] = {
        "found": bool(getattr(match, "found", False)),
        "template": template,
        "region": list(region_tuple),
        "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
    }
    if center and len(center) == 2:
        result["center"] = [int(region_tuple[0] + int(center[0])), int(region_tuple[1] + int(center[1]))]
    return result


def _wait_template(
    app: Any,
    vision: Any,
    template: str,
    region: List[int],
    *,
    threshold: float = 0.82,
    timeout_sec: float = 3.0,
    interval_sec: float = 0.3,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last: Dict[str, Any] = {"found": False, "template": template, "region": list(region)}
    while True:
        _check_trade_cancelled()
        last = _match_template(app, vision, template, region, threshold)
        if last.get("found"):
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(max(float(interval_sec), 0.05))


def _wait_for_shop_menu_ready(
    app: Any,
    vision: Any,
    *,
    timeout_sec: float = _SHOP_MENU_READY_TIMEOUT_SEC,
    interval_sec: float = _SHOP_MENU_READY_INTERVAL_SEC,
    stable_matches: int = _SHOP_MENU_READY_STABLE_MATCHES,
) -> Dict[str, Any]:
    required_stable_matches = max(int(stable_matches), 1)
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    consecutive_matches = 0
    poll = 0
    last_match: Dict[str, Any] = {
        "found": False,
        "template": _SHOP_MENU_READY_TEMPLATE,
        "region": list(_SHOP_MENU_READY_REGION),
    }
    while True:
        poll += 1
        last_match = _match_template(
            app,
            vision,
            _SHOP_MENU_READY_TEMPLATE,
            _SHOP_MENU_READY_REGION,
            _SHOP_MENU_READY_THRESHOLD,
        )
        if last_match.get("found"):
            consecutive_matches += 1
        else:
            consecutive_matches = 0
        logger.info(
            "[TradeShopMenuReady] poll=%s found=%s confidence=%.4f stable=%s/%s",
            poll,
            bool(last_match.get("found")),
            float(last_match.get("confidence") or 0.0),
            consecutive_matches,
            required_stable_matches,
        )
        if consecutive_matches >= required_stable_matches:
            return {
                "success": True,
                "page_state": "shop_page",
                "polls": poll,
                "stable_matches": consecutive_matches,
                "template": _SHOP_MENU_READY_TEMPLATE,
                "region": list(_SHOP_MENU_READY_REGION),
                "threshold": _SHOP_MENU_READY_THRESHOLD,
                "match": last_match,
            }
        if time.monotonic() >= deadline:
            _raise_error(
                "shop_menu_not_ready",
                "trade shop menu template was not detected stably before timeout",
                {
                    "polls": poll,
                    "stable_matches": consecutive_matches,
                    "required_stable_matches": required_stable_matches,
                    "timeout_sec": max(float(timeout_sec), 0.0),
                    "template": _SHOP_MENU_READY_TEMPLATE,
                    "region": list(_SHOP_MENU_READY_REGION),
                    "threshold": _SHOP_MENU_READY_THRESHOLD,
                    "last_match": last_match,
                },
            )
        time.sleep(max(float(interval_sec), 0.05))


def _close_settlement(
    app: Any,
    vision: Any,
    kind: str,
    *,
    timeout_sec: float = 3.0,
    threshold: float = 0.82,
) -> Dict[str, Any]:
    cfg = _BUY_SETTLEMENT if str(kind) == "buy" else _SELL_SETTLEMENT
    logger.info(
        "[TradeSettlement] phase=wait kind=%s template=%s region=%s threshold=%.2f timeout_sec=%.1f context=%s",
        kind,
        cfg["template"],
        cfg["region"],
        float(threshold),
        float(timeout_sec),
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    first = _wait_template(
        app,
        vision,
        str(cfg["template"]),
        list(cfg["region"]),
        threshold=threshold,
        timeout_sec=timeout_sec,
        interval_sec=0.3,
    )
    if not first.get("found"):
        logger.warning(
            "[TradeSettlement] phase=initial_not_found kind=%s match=%s context=%s",
            kind,
            first,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
        return {"closed": False, "found": False, "kind": kind, "first_match": first}

    logger.info(
        "[TradeSettlement] phase=initial_found kind=%s match=%s exit_point=%s context=%s",
        kind,
        first,
        _SETTLEMENT_EXIT_POINT,
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    _check_trade_cancelled()
    app.click(x=_SETTLEMENT_EXIT_POINT[0], y=_SETTLEMENT_EXIT_POINT[1])
    time.sleep(0.8)
    recheck = _wait_template(
        app,
        vision,
        str(cfg["template"]),
        list(cfg["region"]),
        threshold=threshold,
        timeout_sec=1.5,
        interval_sec=0.3,
    )
    logger.info(
        "[TradeSettlement] phase=after_first_exit kind=%s still_visible=%s match=%s context=%s",
        kind,
        bool(recheck.get("found")),
        recheck,
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    retried = False
    final_match = recheck
    if recheck.get("found"):
        _check_trade_cancelled()
        app.click(x=_SETTLEMENT_EXIT_POINT[0], y=_SETTLEMENT_EXIT_POINT[1])
        retried = True
        time.sleep(0.8)
        try:
            final_match = _match_template(
                app,
                vision,
                str(cfg["template"]),
                list(cfg["region"]),
                threshold,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not change flow behavior
            final_match = {
                "found": None,
                "template": str(cfg["template"]),
                "region": list(cfg["region"]),
                "reason": "diagnostic_match_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            logger.warning(
                "[TradeSettlement] phase=after_second_exit_diagnostic_failed kind=%s error_type=%s message=%s context=%s",
                kind,
                type(exc).__name__,
                exc,
                dict(_WORKER_PROGRESS_CONTEXT.get()),
            )
        else:
            log_method = logger.warning if final_match.get("found") else logger.info
            log_method(
                "[TradeSettlement] phase=after_second_exit kind=%s still_visible=%s match=%s context=%s",
                kind,
                bool(final_match.get("found")),
                final_match,
                dict(_WORKER_PROGRESS_CONTEXT.get()),
            )
    final_absence_verified = bool(
        final_match.get("found") is False and final_match.get("reason") != "capture_failed"
    )
    if not final_absence_verified:
        logger.warning(
            "[TradeSettlement] phase=completed kind=%s reported_closed=true final_absence_verified=false retried=%s final_match=%s context=%s",
            kind,
            retried,
            final_match,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
    else:
        logger.info(
            "[TradeSettlement] phase=completed kind=%s reported_closed=true final_absence_verified=true retried=%s context=%s",
            kind,
            retried,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
    return {
        "closed": True,
        "found": True,
        "kind": kind,
        "first_match": first,
        "recheck": recheck,
        "retried": retried,
        "final_match": final_match,
        "final_absence_verified": final_absence_verified,
        "exit_point": {"x": _SETTLEMENT_EXIT_POINT[0], "y": _SETTLEMENT_EXIT_POINT[1]},
    }


def _click_required_nav_button(
    app: Any,
    vision: Any,
    *,
    template: str,
    region: List[int],
    error_code: str,
    page_state: str,
    wait_sec: float,
) -> Dict[str, Any]:
    logger.info(
        "[TradeNavigation] phase=wait target_page_state=%s template=%s region=%s threshold=%.2f context=%s",
        page_state,
        template,
        region,
        _NAV_BUTTON_THRESHOLD,
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    match = _wait_template(
        app,
        vision,
        template,
        region,
        threshold=_NAV_BUTTON_THRESHOLD,
        timeout_sec=_NAV_BUTTON_TIMEOUT_SEC,
        interval_sec=_NAV_BUTTON_INTERVAL_SEC,
    )
    center = match.get("center")
    if not match.get("found") or not isinstance(center, list) or len(center) != 2:
        logger.error(
            "[TradeNavigation] phase=not_found code=%s target_page_state=%s match=%s context=%s",
            error_code,
            page_state,
            match,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
        _raise_error(
            error_code,
            "required navigation button template was not found; click skipped",
            {
                "template": template,
                "region": list(region),
                "threshold": _NAV_BUTTON_THRESHOLD,
                "match": match,
            },
        )
    x, y = int(center[0]), int(center[1])
    _check_trade_cancelled()
    app.click(x=x, y=y)
    time.sleep(max(float(wait_sec), 0.0))
    logger.info(
        "[TradeNavigation] phase=clicked target_page_state=%s x=%s y=%s match=%s context=%s",
        page_state,
        x,
        y,
        match,
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    return {
        "success": True,
        "page_state": page_state,
        "x": x,
        "y": y,
        "template": template,
        "match": match,
    }


def _drag_buy_list(app: Any) -> None:
    app.move_to(x=_BUY_SCROLL_START[0], y=_BUY_SCROLL_START[1], duration=0.1)
    app.drag(
        start_x=_BUY_SCROLL_START[0],
        start_y=_BUY_SCROLL_START[1],
        end_x=_BUY_SCROLL_END[0],
        end_y=_BUY_SCROLL_END[1],
        duration=0.5,
        hold_before_release_sec=0.5,
    )
    time.sleep(0.2)


@action_info(
    name="resonance_pc.open_city_panel_from_main",
    public=True,
    read_only=False,
    description="Open the city panel using its visit-entry template and confirm the current city badge.",
)
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_open_city_panel_from_main(
    timeout_sec: float = 12.0,
    app: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    try:
        return open_city_panel(app=app, vision=vision, check_cancelled=_check_trade_cancelled,
                               timeout_sec=timeout_sec)
    except CityPanelVisionError as exc:
        _raise_error(exc.code, exc.message, exc.detail)


@action_info(name="resonance_pc.tap_back_once", public=True, read_only=False, description="Tap the top-left back button once.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_tap_back_once(wait_sec: float = 1.0, app: Any = None, vision: Any = None) -> Dict[str, Any]:
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    return _click_required_nav_button(
        app,
        vision,
        template=_BACK_BUTTON_TEMPLATE,
        region=_BACK_BUTTON_REGION,
        error_code="nav_back_button_not_found",
        page_state="previous",
        wait_sec=wait_sec,
    )


@action_info(
    name="resonance_pc.go_city_main_direct",
    public=True,
    read_only=False,
    description="Return to the city main screen and confirm its departure button.",
)
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_go_city_main_direct(timeout_sec: float = 5.0, app: Any = None, vision: Any = None) -> Dict[str, Any]:
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    def confirm_main():
        return wait_depart_button(app=app, vision=vision, check_cancelled=_check_trade_cancelled,
                                  timeout_sec=timeout_sec)

    try:
        initial = probe_depart_button(app=app, vision=vision, check_cancelled=_check_trade_cancelled)
        if initial["found"]:
            ready = confirm_main()
            if ready["confirmed"]:
                return {"success": True, "page_state": "city_main", "skipped": True,
                        "reason": "already_on_city_main", "main_ready": ready, "click_attempts": 0}
        clicked = _click_required_nav_button(
            app, vision, template=_CITY_MAIN_BUTTON_TEMPLATE, region=_CITY_MAIN_BUTTON_REGION,
            error_code="nav_city_main_button_not_found", page_state="main_transition", wait_sec=0,
        )
        clicks = [clicked]
        ready = confirm_main()
        if not ready["confirmed"]:
            # Retry only while the observed source-page navigation is still present.
            back = _match_template(app, vision, _CITY_MAIN_BUTTON_TEMPLATE,
                                   _CITY_MAIN_BUTTON_REGION, _NAV_BUTTON_THRESHOLD)
            if back.get("found") and isinstance(back.get("center"), list) and len(back["center"]) == 2:
                _check_trade_cancelled()
                app.click(x=int(back["center"][0]), y=int(back["center"][1]))
                clicks.append({"match": back})
                logger.info("[TradeNavigation] phase=return_main_retry match=%s", back)
                ready = confirm_main()
        if not ready["confirmed"]:
            _raise_error("trade_main_not_restored", "Departure button did not confirm the city main screen",
                         {"main_ready": ready, "click_attempts": len(clicks)})
        return {**clicked, "success": True, "page_state": "city_main", "main_ready": ready,
                "click_attempts": len(clicks), "clicks": clicks}
    except DepartButtonError as exc:
        _raise_error(exc.code, exc.message, exc.detail)


@action_info(
    name="resonance_pc.read_city_name_on_city_panel",
    public=True,
    read_only=False,
    description="Identify the current city using two stable masked badge-template matches. Does not click.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
)
def resonance_pc_read_city_name_on_city_panel(
    timeout_sec: float = 3.0,
    app: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    try:
        return wait_city(app=app, vision=vision, check_cancelled=_check_trade_cancelled,
                         timeout_sec=timeout_sec)
    except CityPanelVisionError as exc:
        _raise_error(exc.code, exc.message, exc.detail)


@action_info(
    name="resonance_pc.click_city_shop_by_name",
    public=True,
    read_only=False,
    description="Click a city shop by resolved city name and shop name from the city panel.",
)
@requires_services(app="plans/aura_base/app", resonance_pc_city_shop_data="resonance_pc_city_shop_data")
def resonance_pc_click_city_shop_by_name(
    city_name: str,
    shop_name: str,
    location_file_path: str = "data/meta/location_pc.json",
    wait_sec: float = 1.0,
    app: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
) -> Dict[str, Any]:
    if app is None or resonance_pc_city_shop_data is None:
        raise RuntimeError("app/resonance_pc_city_shop_data services are required")
    point = resonance_pc_city_shop_data.resolve_shop_point(
        city_name=city_name,
        shop_name=shop_name,
        location_file_path=location_file_path,
    )
    app.click(x=int(point["x"]), y=int(point["y"]))
    time.sleep(max(float(wait_sec), 0.0))
    return {"success": True, "page_state": "shop_page", "click": point}


@action_info(
    name="resonance_pc.click_shop_menu_node",
    public=True,
    read_only=False,
    description="Click the Nth node in a shop page using fixed MuMu coordinates.",
)
@requires_services(app="plans/aura_base/app")
def resonance_pc_click_shop_menu_node(node_index: int, wait_sec: float = 1.0, app: Any = None) -> Dict[str, Any]:
    if app is None:
        raise RuntimeError("app service is required")
    index = int(node_index)
    if index < 1 or index > 6:
        _raise_error("invalid_node_index", "node_index must be between 1 and 6", {"node_index": node_index})
    x = _SHOP_NODE_X
    y = _SHOP_NODE_FIRST_Y + (index - 1) * _SHOP_NODE_GAP_Y
    app.click(x=x, y=y)
    time.sleep(max(float(wait_sec), 0.0))
    return {"success": True, "page_state": "shop_node_page", "node_index": index, "x": x, "y": y}


@action_info(
    name="resonance_pc.buy_goods_on_buy_page",
    public=True,
    read_only=False,
    description="Complete the buy-goods flow from the 我要买 page and return to the shop page.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
)
def resonance_pc_buy_goods_on_buy_page(
    product_list: Optional[List[str]] = None,
    books_used: int = 0,
    bargain_to_cap: bool = False,
    negotiation_max_attempts: int = 5,
    max_scan_rounds: int = 6,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None:
        raise RuntimeError("app/ocr/vision services are required")

    requested_products = [str(item).strip() for item in (product_list or []) if str(item).strip()]
    logger.info(
        "[TradeBuy] phase=started products=%s books_used=%s bargain_to_cap=%s context=%s",
        requested_products,
        int(books_used or 0),
        bool(bargain_to_cap),
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    _report_worker(
        "buy",
        "started",
        data={"products": requested_products, "books_used": int(books_used or 0)},
    )
    _check_trade_cancelled()
    try:
        catalog = load_product_templates()
    except TradeBuySelectionError as exc:
        _raise_error(exc.code, exc.message, exc.detail)
    _check_trade_cancelled()
    negotiation = {"skipped": True, "requested_to_cap": False}
    book_result: Dict[str, Any] = {"ok": True, "used": 0, "skipped": True}
    if int(books_used or 0) > 0:
        _report_worker("books", "started", data={"requested": books_used})
        observe_operation("books.use", "使用采买书并核实数量", "started", requested=books_used)
        book_result = resonance_pc_use_purchase_books(
            books_used=int(books_used),
            item_name="进货采买书",
            app=app,
            vision=vision,
        )
        reporter = _ACTIVE_PROGRESS_REPORTER.get()
        if reporter and type(book_result.get("used")) is int and book_result["used"] >= 0:
            reporter.resources["confirmed_books_used"] += book_result["used"]
        if book_result.get("ok") is not True or book_result.get("used") != books_used:
            observe_operation("books.use", "使用采买书并核实数量", "failed", result=book_result)
            _raise_error("purchase_books_not_confirmed", "Requested purchase books were not confirmed",
                         {**book_result, "book_result": book_result})
        observe_operation("books.use", "使用采买书并核实数量", "completed", used=book_result["used"])
        _report_worker("books", "completed", data={"result": book_result})

    def selection_trace(entry):
        logger.info("[TradeBuy] %s context=%s", entry, dict(_WORKER_PROGRESS_CONTEXT.get()))

    observe_operation("buy.selection", "选择并核实商品", "started", requested_products=requested_products)
    try:
        selection = select_buy_products(
            product_list=requested_products, app=app, vision=vision,
            max_scan_rounds=max_scan_rounds, check_cancelled=_check_trade_cancelled,
            trace_callback=selection_trace, catalog=catalog,
        )
    except (TradeBuySelectionError, CityTradeFlowError) as exc:
        _raise_error(exc.code, str(exc), {**exc.detail, "book_result": book_result})
    selected = list(selection["selected_products"])
    pending = list(selection["missing_products"])
    warnings = list(selection["warnings"])
    scan_trace = list(selection["scan_trace"])
    buy_result = "partial" if pending else "complete"
    observe_operation("buy.selection", "选择并核实商品", "completed",
                      selected_products=selected, missing_products=pending,
                      stop_reason=selection["stop_reason"])
    logger.info("[TradeBuy] phase=selection_completed selected=%s missing=%s stop_reason=%s context=%s",
                selected, pending, selection["stop_reason"], dict(_WORKER_PROGRESS_CONTEXT.get()))

    if not selected:
        try:
            _check_trade_cancelled()
            back = resonance_pc_tap_back_once(app=app, vision=vision)
            shop = _wait_for_shop_menu_ready(app, vision)
        except CityTradeFlowError as exc:
            _raise_error(exc.code, str(exc), {**exc.detail, "book_result": book_result})
        result = {
            "success": True, "buy_result": "skipped", "page_state": "shop_page",
            "requested_products": requested_products, "selected_products": [],
            "selected_product_ids": [], "missing_products": pending,
            "books_requested": int(books_used or 0), "book_result": book_result,
            "negotiation": negotiation, "buy_button": None, "settlement": None,
            "settlement_after_confirm": None, "confirm_panel_found": False,
            "confirm_click": None, "back": back, "shop_menu_ready": shop,
            "warnings": warnings, "scan_trace": scan_trace,
            "selection_stop_reason": selection["stop_reason"],
        }
        _report_worker("buy", "skipped", data={"buy_result": "skipped",
                       "selected_products": [], "missing_products": pending, "warnings": warnings})
        return result
    try:
        if bool(bargain_to_cap):
            _report_worker("negotiation", "started", operation="bargain")
            observe_operation("negotiation.bargain", "砍价子任务", "started")
            negotiation = execute_bargain_to_cap(
                requested_to_cap=True,
                app=app,
                vision=vision,
                max_attempts=negotiation_max_attempts,
            )
            observe_operation("negotiation.bargain", "砍价子任务返回", "completed", result=dict(negotiation))
            _report_worker("negotiation", "completed", operation="bargain", data=dict(negotiation))
    except NegotiationExecutionError as exc:
        observe_operation("negotiation.bargain", "砍价子任务失败", "failed", reason=exc.code)
        _report_worker(
            "negotiation",
            "failed",
            operation="bargain",
            data={"code": exc.code, "message": exc.message, "detail": dict(exc.detail)},
        )
        _raise_error(exc.code, exc.message, {**exc.detail, "book_result": book_result})

    try:
        buy_button_hit = _wait_for_text_hit(app, ocr, ("买入",), _BUY_BUTTON_REGION, timeout_sec=2.0, interval_sec=0.3)
        if buy_button_hit is None:
            logger.error(
                "[TradeBuy] phase=buy_button_not_found products=%s selected=%s missing=%s region=%s context=%s",
                requested_products,
                selected,
                pending,
                _BUY_BUTTON_REGION,
                dict(_WORKER_PROGRESS_CONTEXT.get()),
            )
            _raise_error(
                "buy_button_not_found",
                "Unable to find 买入 button on buy page; fixed-coordinate fallback is disabled.",
                {"region": list(_BUY_BUTTON_REGION), "requested_products": requested_products,
                 "selected_products": selected, "book_result": book_result},
            )
        observe_operation("buy.confirming", "确认买入与结算", "started",
                          selected_products=selected)
        buy_button = _click_hit(app, buy_button_hit)
        buy_button["method"] = "text"
        logger.info(
            "[TradeBuy] phase=buy_button_clicked click=%s context=%s",
            buy_button,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
        time.sleep(0.5)

        settlement = _close_settlement(app, vision, "buy", timeout_sec=3.0)
        confirm_panel = None
        confirm_click = None
        settlement_after_confirm = None
        if not settlement.get("closed"):
            confirm_panel = _wait_for_text_hit(
                app,
                ocr,
                ("预计买入",),
                _BUY_CONFIRM_PANEL_REGION,
                timeout_sec=2.0,
                interval_sec=0.3,
            )
            if confirm_panel is not None:
                confirm_click = _wait_and_click_text(
                    app,
                    ocr,
                    ("买入",),
                    _BUY_CONFIRM_BUTTON_REGION,
                    timeout_sec=2.0,
                    interval_sec=0.3,
                )
                time.sleep(0.8)
                settlement_after_confirm = _close_settlement(app, vision, "buy", timeout_sec=3.0)

        bought = bool(settlement.get("closed")) or bool(
            isinstance(settlement_after_confirm, dict) and settlement_after_confirm.get("closed")
        )
        if not bought:
            observe_operation("buy.confirming", "确认买入与结算", "failed",
                              settlement=settlement, settlement_after_confirm=settlement_after_confirm)
            _raise_error("buy_transaction_not_confirmed", "Purchase settlement was not confirmed",
                         {"book_result": book_result, "negotiation": negotiation,
                          "settlement": settlement, "settlement_after_confirm": settlement_after_confirm,
                          "selected_products": selected})
        observe_operation("buy.confirming", "确认买入与结算", "completed", bought_confirmed=bought)
        logger.info(
            "[TradeBuy] phase=confirmation_completed bought_confirmed=%s initial_settlement=%s confirm_panel_found=%s confirm_click=%s settlement_after_confirm=%s context=%s",
            bought,
            settlement,
            confirm_panel is not None,
            confirm_click,
            settlement_after_confirm,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
        back = {
            "skipped": True,
            "reason": "buy_success_returns_to_shop_page",
            "page_state": "shop_page",
        }
        result = {
            "success": True,
            "page_state": "shop_page",
            "requested_products": requested_products,
            "buy_result": buy_result,
            "selected_product_ids": list(selection["selected_product_ids"]),
            "selected_products": selected,
            "missing_products": pending,
            "books_requested": int(books_used or 0),
            "book_result": book_result,
            "negotiation": negotiation,
            "buy_button": buy_button,
            "settlement": settlement,
            "confirm_panel_found": confirm_panel is not None,
            "confirm_click": confirm_click,
            "settlement_after_confirm": settlement_after_confirm,
            "back": back,
            "scan_trace": scan_trace,
            "selection_stop_reason": selection["stop_reason"],
            "warnings": warnings,
        }
        _report_worker(
            "buy",
            "completed",
            data={
                "selected_products": list(selected),
                "missing_products": list(pending),
                "bought": bought,
                "buy_result": buy_result,
                "warnings": warnings,
            },
        )
        logger.info(
            "[TradeBuy] phase=completed reported_success=true declared_page_state=shop_page bought_confirmed=true selected=%s missing=%s context=%s",
            selected, pending, dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
        return result
    except CityTradeFlowError as exc:
        _raise_error(exc.code, str(exc), {**exc.detail, "book_result": book_result})


@action_info(
    name="resonance_pc.sell_goods_on_sell_page",
    public=True,
    read_only=False,
    description="Complete the sell-all flow from the 我要卖 page and return to the shop page.",
)
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", vision="plans/aura_base/vision")
def resonance_pc_sell_goods_on_sell_page(
    raise_to_cap: bool = False,
    negotiation_max_attempts: int = 5,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None:
        raise RuntimeError("app/ocr/vision services are required")

    logger.info(
        "[TradeSell] phase=started raise_to_cap=%s context=%s",
        bool(raise_to_cap),
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    _report_worker("sell", "started", data={"raise_to_cap": bool(raise_to_cap)})
    from ._trade_sell_state import SellSession
    session = SellSession(app, vision, _check_trade_cancelled, _raise_error)
    observe_operation("sell.selection", "核实卖出商品", "started")
    selection = session.select()
    observe_operation("sell.selection", "核实卖出商品", "completed", selection_status=selection['status'])
    sell_all_click = (selection['clicks'][-1] if selection['clicks']
                      else {"clicked": False, "reason": selection['status']})
    log_method = logger.info if sell_all_click.get("clicked") else logger.warning
    log_method(
        "[TradeSell] phase=sell_all_selection clicked=%s detail=%s context=%s",
        bool(sell_all_click.get("clicked")), sell_all_click, dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    sell_button_click = {"clicked": False, "reason": "sell_all_not_clicked"}
    settlement = {"closed": False, "found": False, "kind": "sell"}
    negotiation = execute_raise_to_cap(
        requested_to_cap=False,
        app=app,
        vision=vision,
        max_attempts=negotiation_max_attempts,
    )
    commit = None
    if selection['status'] == 'selected':
        try:
            if bool(raise_to_cap):
                _report_worker("negotiation", "started", operation="raise")
                observe_operation("negotiation.raise", "抬价子任务", "started")
            negotiation = execute_raise_to_cap(
                requested_to_cap=bool(raise_to_cap),
                app=app,
                vision=vision,
                max_attempts=negotiation_max_attempts,
            )
            if bool(raise_to_cap):
                observe_operation("negotiation.raise", "抬价子任务返回", "completed", result=dict(negotiation))
                _report_worker("negotiation", "completed", operation="raise", data=dict(negotiation))
        except NegotiationExecutionError as exc:
            observe_operation("negotiation.raise", "抬价子任务失败", "failed", reason=exc.code)
            _report_worker(
                "negotiation",
                "failed",
                operation="raise",
                data={"code": exc.code, "message": exc.message, "detail": dict(exc.detail)},
            )
            _raise_error(exc.code, exc.message, exc.detail)
        observe_operation("sell.confirming", "确认卖出与结算", "started")
        commit = session.submit()
        sell_button_click = (commit['clicks'][-1] if commit['clicks']
                             else {"clicked": False, "reason": "settlement_already_visible"})
        log_method = logger.info if sell_button_click.get("clicked") else logger.warning
        log_method(
            "[TradeSell] phase=sell_button clicked=%s detail=%s negotiation=%s context=%s",
            bool(sell_button_click.get("clicked")),
            sell_button_click,
            negotiation,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
        _check_trade_cancelled()
        settlement = _close_settlement(app, vision, "sell", timeout_sec=3.0)
        if not settlement.get('closed') or not settlement.get('final_absence_verified'):
            observe_operation("sell.confirming", "确认卖出与结算", "failed", settlement=settlement)
            _raise_error('sell_settlement_close_unconfirmed', '卖货结算关闭未确认', settlement)
        observe_operation("sell.confirming", "确认卖出与结算", "completed", sold_confirmed=True)

    sold = bool(settlement.get("closed"))
    back = session.return_to_shop(sold)
    result = {
        "success": True,
        "page_state": "shop_page",
        "sold_confirmed": sold,
        "sell_result": "sold" if sold else selection['status'],
        "selection": selection,
        "commit": commit,
        "sell_all_click": sell_all_click,
        "negotiation": negotiation,
        "sell_button_click": sell_button_click,
        "settlement": settlement,
        "back": back,
    }
    _report_worker("sell", "completed", data={"sold_confirmed": sold})
    log_method = logger.info if sold else logger.warning
    log_method(
        "[TradeSell] phase=completed reported_success=true declared_page_state=shop_page sold_confirmed=%s sell_result=%s sell_all_click=%s sell_button_click=%s settlement=%s back=%s context=%s",
        sold,
        result["sell_result"],
        sell_all_click,
        sell_button_click,
        settlement,
        back,
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    return result


def _execute_city_trade_inside_current_city(
    *,
    current_city: str,
    buy_products: Optional[List[str]],
    books_used: int,
    sell_raise_to_cap: bool = False,
    buy_bargain_to_cap: bool = False,
    negotiation_max_attempts: int = DEFAULT_NEGOTIATION_MAX_ATTEMPTS,
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
    progress_context: Optional[Dict[str, Any]] = None,
    auto_trade_goods_investment: bool = False,
    trade_goods_investment_mode: str = "unlock",
) -> Dict[str, Any]:
    progress_token = _WORKER_PROGRESS_CONTEXT.set(dict(progress_context or {}))
    try:
        return _execute_city_trade_inside_current_city_scoped(
            current_city=current_city,
            buy_products=buy_products,
            books_used=books_used,
            sell_raise_to_cap=sell_raise_to_cap,
            buy_bargain_to_cap=buy_bargain_to_cap,
            negotiation_max_attempts=negotiation_max_attempts,
            app=app,
            ocr=ocr,
            vision=vision,
            city_shop_data=city_shop_data,
            auto_trade_goods_investment=auto_trade_goods_investment,
            trade_goods_investment_mode=trade_goods_investment_mode,
        )
    finally:
        _WORKER_PROGRESS_CONTEXT.reset(progress_token)


def _execute_city_trade_inside_current_city_scoped(
    *,
    current_city: str,
    buy_products: Optional[List[str]],
    books_used: int,
    sell_raise_to_cap: bool,
    buy_bargain_to_cap: bool,
    negotiation_max_attempts: int = DEFAULT_NEGOTIATION_MAX_ATTEMPTS,
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
    auto_trade_goods_investment: bool = False,
    trade_goods_investment_mode: str = "unlock",
) -> Dict[str, Any]:
    products = [str(item).strip() for item in (buy_products or []) if str(item).strip()]
    logger.info(
        "[CityTrade] phase=started city=%s buy_products=%s books_used=%s sell_raise_to_cap=%s buy_bargain_to_cap=%s context=%s",
        current_city,
        products,
        int(books_used or 0),
        bool(sell_raise_to_cap),
        bool(buy_bargain_to_cap),
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    if bool(buy_bargain_to_cap) and not products:
        _raise_error(
            "negotiation_without_selected_goods",
            "Bargaining was requested for a route leg without buy products.",
            {"current_city": current_city},
        )
    enter_shop = resonance_pc_click_city_shop_by_name(
        city_name=current_city,
        shop_name="交易所",
        wait_sec=_SHOP_ENTRY_SETTLE_SEC,
        app=app,
        resonance_pc_city_shop_data=city_shop_data,
    )
    shop_menu_ready = _wait_for_shop_menu_ready(app, vision)
    goods_investment = {"triggered": False, "status": "not_triggered"}
    if auto_trade_goods_investment:
        goods_investment = execute_trade_goods_investment_from_shop(
            mode=trade_goods_investment_mode,
            city_name=current_city,
            app=app,
            vision=vision,
            progress=lambda state, data: _report_worker("trade_goods_investment", state, data=data),
        )
        if goods_investment.get("success") is not True or goods_investment.get("page_state") != "shop_page":
            _raise_error("goods_investment_not_completed", "Goods investment did not confirm return to the exchange", goods_investment)
        shop_menu_ready = _wait_for_shop_menu_ready(app, vision)
        context_fields = _WORKER_PROGRESS_CONTEXT.get()
        if context_fields.get("leg_count", 0) > 0 and context_fields.get("city_index") == context_fields.get("leg_count"):
            _report_worker("final_sale", "started", data={"raise_to_cap": bool(sell_raise_to_cap)})
    sell_node = resonance_pc_click_shop_menu_node(node_index=2, app=app)
    sell = resonance_pc_sell_goods_on_sell_page(
        raise_to_cap=bool(sell_raise_to_cap),
        negotiation_max_attempts=negotiation_max_attempts,
        app=app,
        ocr=ocr,
        vision=vision,
    )
    if sell.get("success") is not True or sell.get("page_state") != "shop_page":
        _raise_error("sell_transaction_not_confirmed", "Cannot buy after an unconfirmed sale", sell)
    sold_confirmed = bool(sell.get("sold_confirmed"))
    log_method = logger.info if sold_confirmed else logger.warning
    log_method(
        "[CityTrade] phase=sell_returned city=%s action_success=%s sold_confirmed=%s sell_result=%s declared_page_state=%s will_continue_to_buy=%s sell=%s context=%s",
        current_city,
        bool(sell.get("success")),
        sold_confirmed,
        sell.get("sell_result"),
        sell.get("page_state"),
        bool(products),
        sell,
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    buy = None
    if products:
        logger.info(
            "[CityTrade] phase=enter_buy city=%s sold_confirmed=%s products=%s context=%s",
            current_city,
            sold_confirmed,
            products,
            dict(_WORKER_PROGRESS_CONTEXT.get()),
        )
        buy_node = resonance_pc_click_shop_menu_node(node_index=1, app=app)
        buy = resonance_pc_buy_goods_on_buy_page(
            product_list=products,
            books_used=int(books_used or 0),
            bargain_to_cap=bool(buy_bargain_to_cap),
            negotiation_max_attempts=negotiation_max_attempts,
            app=app,
            ocr=ocr,
            vision=vision,
        )
    else:
        buy_node = None
    buy_confirmed = None
    if isinstance(buy, dict):
        buy_confirmed = bool((buy.get("settlement") or {}).get("closed")) or bool(
            (buy.get("settlement_after_confirm") or {}).get("closed")
        )
        skipped = (buy.get("buy_result") == "skipped" and not buy.get("selected_products")
                   and buy.get("page_state") == "shop_page")
        if (buy.get("success") is not True or buy.get("page_state") != "shop_page"
                or (not skipped and (buy.get("buy_result") not in {"complete", "partial"} or not buy_confirmed))):
            _raise_error("buy_transaction_not_confirmed", "Cannot depart after an unconfirmed purchase", buy)
    logger.info(
        "[CityTrade] phase=before_return_city_main city=%s sold_confirmed=%s buy_required=%s buy_confirmed=%s declared_sell_page_state=%s declared_buy_page_state=%s context=%s",
        current_city,
        sold_confirmed,
        bool(products),
        buy_confirmed,
        sell.get("page_state"),
        buy.get("page_state") if isinstance(buy, dict) else None,
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    main = resonance_pc_go_city_main_direct(app=app, vision=vision)
    if main.get("success") is not True or main.get("page_state") != "city_main":
        _raise_error("trade_main_not_restored", "City trade did not return to main", main)
    logger.info(
        "[CityTrade] phase=completed city=%s sold_confirmed=%s buy_confirmed=%s page_state=%s context=%s",
        current_city,
        sold_confirmed,
        buy_confirmed,
        main.get("page_state"),
        dict(_WORKER_PROGRESS_CONTEXT.get()),
    )
    return {
        "success": True,
        "page_state": "city_main",
        "current_city": current_city,
        "sell_raise_to_cap": bool(sell_raise_to_cap),
        "buy_bargain_to_cap": bool(buy_bargain_to_cap),
        "enter_shop": enter_shop,
        "shop_menu_ready": shop_menu_ready,
        "trade_goods_investment": goods_investment,
        "sell_node": sell_node,
        "sell": sell,
        "buy_node": buy_node,
        "buy": buy,
        "go_city_main": main,
    }


def _summarize_goods_investment(execution: Dict[str, Any], final_sale: Optional[Dict[str, Any]],
                                *, enabled: bool, mode: str) -> Dict[str, Any]:
    visits = []
    for leg in execution.get("leg_results") or []:
        investment = (leg.get("city_trade") or {}).get("trade_goods_investment") or {}
        if investment.get("triggered"):
            visits.append({"city_index": int(leg.get("index") or 0), **investment})
    endpoint = (final_sale or {}).get("trade_goods_investment") or {}
    if endpoint.get("triggered"):
        visits.append({"city_index": len(execution.get("leg_results") or []), **endpoint})
    return {"enabled": enabled, "mode": mode, "target_level": MODE_TARGETS[mode], "visits": visits,
            "triggered_count": len(visits),
            "transaction_count": sum(int(visit.get("transaction_count") or 0) for visit in visits),
            "upgraded_levels": sum(int(visit.get("upgraded_levels") or 0) for visit in visits)}


def _has_recovery_rest_point(city_name: str, city_shop_data) -> bool:
    try:
        city_shop_data.resolve_shop_point(city_name, "rest")
    except CityShopDataError as exc:
        if exc.code != "shop_not_found_in_city":
            raise
        return False
    return True


async def _call_recovery_action(action_name: str, params: dict, *, context, engine) -> dict:
    """Use the engine adapter so synchronous recovery work is tracked on cancellation."""
    if context is None or engine is None:
        raise RuntimeError("Freight recovery requires the current execution context and engine")
    injector = ActionInjector(
        context, engine, TemplateRenderer(context, engine.state_store), engine.services,
        current_package=getattr(engine.orchestrator, "loaded_package", None),
        service_resolver=engine.orchestrator.resolve_service,
    )
    result = await injector.execute(action_name, params)
    if not isinstance(result, dict):
        _raise_error("recovery_invalid_result", "Recovery action did not return an object", {"action": action_name})
    return result


async def _refresh_recovery_fatigue(*, page_state: str, context, engine) -> int:
    if page_state == "city_panel":
        main = await _call_recovery_action("resonance_pc.go_city_main_direct", {}, context=context, engine=engine)
        if main.get("success") is not True or main.get("page_state") != "city_main":
            _raise_error("recovery_main_not_restored", "Recovery requires the main screen", main)
    elif page_state != "city_main":
        _raise_error("recovery_invalid_start_page", "Recovery requires a confirmed city page", {"page_state": page_state})
    # This registered action verifies its main-screen return before returning data.
    snapshot = await _call_recovery_action(
        "resonance_pc.player_data_refresh",
        {"stages": ["profile"], "profile_sections": ["fatigue"]},
        context=context, engine=engine,
    )
    fatigue = (snapshot.get("status") or {}).get("fatigue") or {}
    metadata = snapshot.get("metadata") or {}
    current, maximum = fatigue.get("current"), fatigue.get("max")
    if (type(current) is not int or current < 0 or type(maximum) is not int or maximum < 1
            or metadata.get("persisted") is not True
            or metadata.get("executed_profile_sections") != ["fatigue"]):
        _raise_error("recovery_fatigue_invalid", "Current fatigue refresh was not confirmed", snapshot)
    return current


async def _plan_and_execute_water_arrival(
    selection: dict, *, route, travel_costs, page_state, context, engine,
    app, ocr, vision, city_shop_data, persistent_data,
) -> dict:
    observe_operation("sparkling_water.fatigue_refresh", "刷新当前疲劳", "started")
    current = await _refresh_recovery_fatigue(page_state=page_state, context=context, engine=engine)
    observe_operation("sparkling_water.fatigue_refresh", "当前疲劳已刷新", "completed", actual_fatigue=current)
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    if reporter:
        reporter.resources["actual_fatigue"] = current
    remaining = estimate_remaining_consumption(route, selection["city_index"], travel_costs)
    free_uses, _ = _water_counts(load_pc_user_info(persistent_data))
    selection.update(remaining)
    selection["remaining_free_uses"] = free_uses
    selection.update(plan_water_use(current, remaining["remaining_consumption"],
                                    selection["base_fatigue_reserve"], free_uses))
    logger.info("[FreightRecovery] water arrival plan=%s", selection)
    if selection["drink_count"] == 0:
        if reporter:
            await reporter.emit("sparkling_water", "skipped", city_index=selection["city_index"],
                                current_city=selection["city_name"], data={"reason": selection["reason"]})
        return {"success": True, "triggered": False, "status": "skipped",
                "reason": selection["reason"], "page_state": "city_main", "selection": dict(selection)}
    return await _execute_sparkling_water_stop(
        selection, page_state="city_main", app=app, ocr=ocr, vision=vision,
        city_shop_data=city_shop_data, persistent_data=persistent_data,
        context=context, engine=engine,
    )


async def _execute_sparkling_water_stop(
    selection: Dict[str, Any], *, page_state: str, app: Any, ocr: Any, vision: Any,
    city_shop_data: ResonancePcCityShopDataService, persistent_data: PersistentDataService,
    context: ExecutionContext | None = None, engine: ExecutionEngine | None = None,
) -> Dict[str, Any]:
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    fields = {"city_index": selection["city_index"], "current_city": selection["city_name"]}
    if reporter is not None:
        await reporter.emit("sparkling_water", "started", **fields, data={"selection": selection})
    try:
        if page_state == "city_main":
            opened = await _call_recovery_action(
                "resonance_pc.open_city_panel_from_main", {}, context=context, engine=engine,
            )
            if opened.get("success") is not True or opened.get("page_state") != "city_panel":
                _raise_error("sparkling_water_panel_not_restored", "City panel was not confirmed before drinking", opened)
        elif page_state != "city_panel":
            _raise_error("sparkling_water_invalid_start_page", "Expected city main or city panel before drinking", {"page_state": page_state})
        result = await resonance_pc_drink_sparkling_water_from_city_panel(
            city_name=selection["city_name"], drink_count=selection["drink_count"],
            app=app, ocr=ocr, vision=vision, resonance_pc_city_shop_data=city_shop_data,
            persistent_data=persistent_data,
        )
        if result.get("success") is not True or result.get("page_state") != "city_panel":
            _raise_error("sparkling_water_not_completed", "Sparkling water task did not confirm return to city panel", result)
    except Exception as exc:
        if reporter is not None:
            await reporter.emit("sparkling_water", "failed", **fields, data={"error": str(exc), "code": getattr(exc, "code", None)})
        raise
    if reporter is not None:
        reporter.resources["water_basic_recovered_fatigue"] = int(result.get("completed_count") or 0) * 50
        await reporter.emit("sparkling_water", "completed", **fields, data={"result": result})
    return {**result, "triggered": True, "selection": dict(selection)}


async def _execute_route(
    *,
    route: List[Dict[str, Any]],
    start_page_state: str,
    use_fatigue_medicine: bool,
    allowed_fatigue_medicines: Optional[List[str]],
    fatigue_medicine_max_uses: int,
    negotiation_max_attempts: int = DEFAULT_NEGOTIATION_MAX_ATTEMPTS,
    arrival_timeout_seconds: float = 3600.0,
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
    state_store: StateStoreService,
    auto_cape_island_investment: bool = False,
    auto_trade_goods_investment: bool = False,
    trade_goods_investment_mode: str = "unlock",
    auto_rubbish_recycling: bool = True,
    auto_pickup: bool = False,
    engine: ExecutionEngine | None = None,
    sparkling_water_plan: Optional[Dict[str, Any]] = None,
    persistent_data: PersistentDataService | None = None,
    recovery_context: ExecutionContext | None = None,
    recovery_travel_costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    route_state = await resonance_pc_trade_route_execution_init(route=route, state_store=state_store)
    route_run_key = str(route_state.get("run_key") or "")
    page_state = start_page_state
    leg_results: List[Dict[str, Any]] = []
    failure: Optional[Dict[str, Any]] = None
    rubbish_recycling_attempted = False
    selection = sparkling_water_plan if sparkling_water_plan is not None else {}
    water_result: Dict[str, Any] = {"triggered": False, "status": "not_triggered", "reason": selection.get("reason")}
    try:
        for index, leg in enumerate(route):
            progress_fields = {
                "leg_index": index,
                "leg_count": len(route),
                "city_index": index,
                "city_count": len(route) + 1,
                "from_city": str(leg.get("from_city") or ""),
                "to_city": str(leg.get("to_city") or ""),
                "current_city": str(leg.get("from_city") or ""),
            }
            if reporter is not None:
                await reporter.emit("leg", "started", **progress_fields, data={"leg": dict(leg)})
            try:
                leg_result = await _execute_trade_leg(
                    index=index,
                    leg=leg,
                    sell_raise_to_cap=(
                        bool(route[index - 1].get("raise_to_cap")) if index > 0 else False
                    ),
                    page_state=page_state,
                    use_fatigue_medicine=use_fatigue_medicine,
                    allowed_fatigue_medicines=allowed_fatigue_medicines,
                    fatigue_medicine_max_uses=fatigue_medicine_max_uses,
                    negotiation_max_attempts=negotiation_max_attempts,
                    arrival_timeout_seconds=arrival_timeout_seconds,
                    auto_pickup=auto_pickup,
                    app=app,
                    ocr=ocr,
                    vision=vision,
                    city_shop_data=city_shop_data,
                    progress_fields=progress_fields,
                    auto_cape_island_investment=bool(auto_cape_island_investment),
                    auto_trade_goods_investment=bool(auto_trade_goods_investment),
                    trade_goods_investment_mode=trade_goods_investment_mode,
                    auto_rubbish_recycling=bool(
                        auto_rubbish_recycling
                        and not rubbish_recycling_attempted
                        and is_rubbish_recycling_arrival(leg)
                    ),
                    engine=engine,
                )
            except Exception as exc:
                if not hasattr(exc, "code"):
                    raise
                failure = {"status": "cancelled" if "cancel" in str(exc.code) else "failed",
                           "reason": str(exc.code), "failed_leg_index": index,
                           "error": {"code": str(exc.code), "message": str(exc),
                                     "detail": dict(getattr(exc, "detail", {}) or {})}}
                leg_results.append({"leg": leg, **failure})
                page_state = "unknown"
                if reporter:
                    await reporter.emit("leg", failure["status"], **progress_fields, data=failure)
                break
            page_state = str(leg_result.get("page_state") or "city_main")
            if bool((leg_result.get("rubbish_recycling") or {}).get("triggered")):
                rubbish_recycling_attempted = True
            travel = dict(leg_result.get("travel") or {})
            if travel.get("success") is False and str(travel.get("status") or "").lower() != "blocked":
                _raise_error("trade_arrival_not_confirmed", "Arrival failed; cannot continue city actions", travel)
            if (
                selection.get("planned") and selection.get("city_index") == index + 1
                and str(travel.get("status") or "ok").lower() != "blocked"
                and travel.get("success", True)
            ):
                water_result = await _plan_and_execute_water_arrival(
                    selection, page_state=page_state, app=app, ocr=ocr, vision=vision,
                    city_shop_data=city_shop_data, persistent_data=persistent_data,
                    route=route, travel_costs=recovery_travel_costs,
                    context=recovery_context, engine=engine,
                )
                page_state = water_result["page_state"]
                leg_result["page_state"] = page_state
                leg_result["sparkling_water"] = water_result
            update = await resonance_pc_trade_route_execution_update(
                run_key=route_run_key,
                leg=leg,
                travel_status=str(travel.get("status") or "ok"),
                reason=travel.get("reason"),
                blocked_at=travel.get("blocked_at"),
                fatigue_medicine_used=travel.get("fatigue_medicine_used") or [],
                fatigue_medicine_use_count=int(travel.get("fatigue_medicine_use_count") or 0),
                state_store=state_store,
            )
            blocked = str(update.get("status") or "").lower() == "blocked"
            leg_result["status"] = "blocked" if blocked else "completed"
            leg_results.append(leg_result)
            if reporter is not None:
                await reporter.emit(
                    "leg",
                    "blocked" if blocked else "completed",
                    **progress_fields,
                    data={"travel": travel},
                )
            if blocked:
                break
            await asyncio.sleep(2.0)
        summary = await resonance_pc_trade_route_execution_summary(route_run_key, state_store=state_store)
        if failure:
            summary.update(failure)
        summary["page_state"] = page_state
        summary["leg_results"] = leg_results
        summary["sparkling_water"] = water_result
        # The shared route store uses "ok" for completion. Freight exposes
        # "completed" only after every leg has a confirmed arrival.
        completed_leg_count = sum(
            item.get("status") == "completed"
            and (item.get("travel") or {}).get("success") is True
            and str((item.get("travel") or {}).get("status") or "ok").lower()
            not in {"blocked", "failed", "cancelled"}
            for item in leg_results
        )
        store_status = str(summary.get("status") or "").lower()
        if not failure and store_status == "ok":
            if route and len(leg_results) == len(route) == completed_leg_count:
                summary.update(status="completed", should_continue=False,
                               completed_leg_count=completed_leg_count, route_count=len(route))
            else:
                summary.update(status="failed", should_continue=False,
                               reason="route_completion_not_confirmed")
        logger.info(
            "[TradeRoute] phase=summary store_status=%s status=%s confirmed_legs=%s/%s "
            "page_state=%s reason=%s",
            store_status, summary.get("status"), completed_leg_count, len(route),
            page_state, summary.get("reason"),
        )
        island_results = [
            dict(item.get("cape_island_investment") or {})
            for item in leg_results
            if bool((item.get("cape_island_investment") or {}).get("triggered"))
        ]
        summary["cape_island_triggered_count"] = len(island_results)
        summary["cape_island_invested_count"] = sum(
            1 for item in island_results if str(item.get("status") or "") == "invested"
        )
        summary["cape_island_skipped_count"] = sum(
            1 for item in island_results if str(item.get("status") or "") == "skipped"
        )
        rubbish_results = [
            dict(item.get("rubbish_recycling") or {})
            for item in leg_results
            if bool((item.get("rubbish_recycling") or {}).get("triggered"))
        ]
        summary["rubbish_recycling_triggered_count"] = len(rubbish_results)
        summary["rubbish_recycling_status"] = (
            str(rubbish_results[0].get("status") or "unknown")
            if rubbish_results
            else "not_triggered"
        )
        summary["rubbish_recycling_city_id"] = (
            str(rubbish_results[0].get("city_id") or "") if rubbish_results else None
        )
        summary["rubbish_recycling_city_name"] = (
            str(rubbish_results[0].get("city_name") or "") if rubbish_results else None
        )
        return summary
    finally:
        if route_run_key:
            await resonance_pc_trade_route_execution_cleanup(route_run_key, state_store=state_store)


async def _execute_trade_leg(
    *,
    index: int,
    leg: Dict[str, Any],
    sell_raise_to_cap: bool,
    page_state: str,
    use_fatigue_medicine: bool,
    allowed_fatigue_medicines: Optional[List[str]],
    fatigue_medicine_max_uses: int,
    negotiation_max_attempts: int,
    arrival_timeout_seconds: float = 3600.0,
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
    progress_fields: Optional[Dict[str, Any]] = None,
    auto_cape_island_investment: bool = False,
    auto_trade_goods_investment: bool = False,
    trade_goods_investment_mode: str = "unlock",
    auto_rubbish_recycling: bool = True,
    auto_pickup: bool = False,
    engine: ExecutionEngine | None = None,
) -> Dict[str, Any]:
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    progress_fields = dict(progress_fields or {})
    if page_state == "city_main":
        await asyncio.to_thread(resonance_pc_open_city_panel_from_main, app=app, vision=vision)
        page_state = "city_panel"
    elif page_state != "city_panel":
        _raise_error(
            "unexpected_page_state_before_city_trade",
            "Route execution expected city_panel or city_main.",
            {"page_state": page_state, "leg": leg},
        )

    city_trade = await asyncio.to_thread(
        _execute_city_trade_inside_current_city,
        current_city=str(leg.get("from_city") or ""),
        buy_products=list(leg.get("buy_products") or []),
        books_used=int(leg.get("books_used") or 0),
        sell_raise_to_cap=bool(sell_raise_to_cap),
        buy_bargain_to_cap=bool(leg.get("bargain_to_cap")),
        negotiation_max_attempts=negotiation_max_attempts,
        app=app,
        ocr=ocr,
        vision=vision,
        city_shop_data=city_shop_data,
        progress_context=progress_fields,
        auto_trade_goods_investment=bool(auto_trade_goods_investment and index > 0),
        trade_goods_investment_mode=trade_goods_investment_mode,
    )
    page_state = str(city_trade.get("page_state") or "city_main")
    if city_trade.get("success") is not True or page_state != "city_main":
        _raise_error("city_trade_not_confirmed", "Trade failed; departure is disabled", city_trade)

    if reporter is not None:
        await reporter.emit("travel", "started", **progress_fields)
    travel = await asyncio.to_thread(
        resonance_pc_intercity_depart_and_wait,
        to_city_name=str(leg.get("to_city") or ""),
        from_city_name=str(leg.get("from_city") or ""),
        enter_station_timeout_seconds=arrival_timeout_seconds,
        auto_pickup=auto_pickup,
        location_file_path="data/meta/location_pc.json",
        city_search_region=[130, 120, 1000, 500],  # exclude top HUD; bottom unchanged
        drag_center=[640, 360],
        drag_span_px=450,
        max_search_steps=12,
        fallback_enabled=True,
        target_match_mode="contains",
        click_y_offset=-15,
        drag_duration_sec=1.0,
        drag_hold_sec=0.5,
        use_fatigue_medicine=bool(use_fatigue_medicine),
        allowed_fatigue_medicines=allowed_fatigue_medicines or [],
        fatigue_medicine_max_uses=int(fatigue_medicine_max_uses),
        app=app,
        ocr=ocr,
        vision=vision,
    )
    if reporter is not None:
        travel_status = str(travel.get("status") or "ok").lower()
        arrival_fields = dict(progress_fields)
        arrival_fields["current_city"] = str(leg.get("to_city") or "")
        arrival_fields["city_index"] = (
            int(progress_fields.get("city_index", index))
            if travel_status == "blocked"
            else int(progress_fields.get("city_index", index)) + 1
        )
        await reporter.emit(
            "arrival",
            "blocked" if travel_status == "blocked" else "completed",
            **arrival_fields,
            data={"travel": dict(travel)},
        )
    travel_status = str(travel.get("status") or "ok").lower()
    page_state = "city_main"
    cape_island_investment: Dict[str, Any] = {
        "triggered": False,
        "status": "not_applicable",
        "reason": None,
    }
    if (
        bool(auto_cape_island_investment)
        and travel_status != "blocked"
        and bool(travel.get("success", True))
        and _is_cape_city_arrival(leg)
    ):
        investment_fields = dict(progress_fields)
        investment_fields.update(
            city_index=int(progress_fields.get("city_index", index)) + 1,
            current_city=str(leg.get("to_city") or ""),
        )
        if reporter is not None:
            await reporter.emit("investment", "started", **investment_fields)
        try:
            cape_island_investment = await _execute_cape_island_investment_after_arrival(
                leg_index=index,
                leg=leg,
                app=app,
                ocr=ocr,
                vision=vision,
                city_shop_data=city_shop_data,
                engine=engine,
            )
            page_state = str(cape_island_investment.get("page_state") or "city_main")
        except Exception as exc:
            if reporter is not None:
                await reporter.emit(
                    "investment",
                    "failed",
                    **investment_fields,
                    data={"error_type": type(exc).__name__, "message": str(exc)},
                )
            raise
        if reporter is not None:
            investment_status = str(cape_island_investment.get("status") or "").lower()
            await reporter.emit(
                "investment",
                "skipped" if investment_status == "skipped" else "completed",
                **investment_fields,
                data={"investment": dict(cape_island_investment)},
            )
    rubbish_recycling: Dict[str, Any] = {
        "triggered": False,
        "attempted": False,
        "status": "not_applicable",
        "reason": None,
    }
    if (
        bool(auto_rubbish_recycling)
        and travel_status != "blocked"
        and bool(travel.get("success", True))
        and is_rubbish_recycling_arrival(leg)
    ):
        rubbish_fields = dict(progress_fields)
        rubbish_fields.update(
            city_index=int(progress_fields.get("city_index", index)) + 1,
            current_city=str(leg.get("to_city") or ""),
        )
        if reporter is not None:
            await reporter.emit("rubbish_recycling", "started", **rubbish_fields)
        try:
            rubbish_recycling = await _execute_rubbish_recycling_after_arrival(
                leg_index=index,
                leg=leg,
                app=app,
                ocr=ocr,
                vision=vision,
                city_shop_data=city_shop_data,
            )
            page_state = str(rubbish_recycling.get("page_state") or "city_panel")
        except Exception as exc:
            if reporter is not None:
                await reporter.emit(
                    "rubbish_recycling",
                    "failed",
                    **rubbish_fields,
                    data={"error_type": type(exc).__name__, "message": str(exc)},
                )
            raise
        if reporter is not None:
            rubbish_status = str(rubbish_recycling.get("status") or "").lower()
            await reporter.emit(
                "rubbish_recycling",
                "skipped" if rubbish_status == "empty" else "completed",
                **rubbish_fields,
                data={"rubbish_recycling": dict(rubbish_recycling)},
            )
    return {
        "index": int(index),
        "status": "pending",
        "leg": dict(leg),
        "city_trade": city_trade,
        "travel": travel,
        "cape_island_investment": cape_island_investment,
        "rubbish_recycling": rubbish_recycling,
        "page_state": page_state,
    }


def _is_cape_city_arrival(leg: Dict[str, Any]) -> bool:
    return bool(
        str(leg.get("to_city_id") or "").strip() == "11"
        or str(leg.get("to_city_key") or "").strip().lower() == "cape_city"
        or str(leg.get("to_city") or "").strip() == "海角城"
    )


async def _execute_cape_island_investment_after_arrival(
    *,
    leg_index: int,
    leg: Dict[str, Any],
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
    engine: ExecutionEngine | None,
) -> Dict[str, Any]:
    if engine is None:
        raise RuntimeError("Cape island investment requires the active execution engine")
    started_at = time.monotonic()
    arrival_city = str(leg.get("to_city") or "海角城")
    logger.info(
        "Cape island investment triggered leg_index=%s from_city=%s to_city=%s to_city_id=%s",
        int(leg_index),
        leg.get("from_city"),
        arrival_city,
        leg.get("to_city_id"),
    )
    try:
        open_city = await asyncio.to_thread(
            resonance_pc_open_city_panel_from_main,
            app=app,
            vision=vision,
        )
        observe_operation("investment.execute", "投资子任务", "started")
        investment = await resonance_pc_execute_cape_island_investment_from_city_panel(
            app=app,
            ocr=ocr,
            vision=vision,
            resonance_pc_city_shop_data=city_shop_data,
            engine=engine,
        )
        observe_operation("investment.execute", "投资子任务返回", "completed",
                          status=investment.get("status"), reason=investment.get("reason"),
                          selected_option=investment.get("selected_option"))
        observe_operation("investment.return_main", "投资后返回城市", "started")
        return_main = await asyncio.to_thread(
            resonance_pc_go_city_main_direct,
            app=app,
            vision=vision,
        )
        observe_operation("investment.return_main", "投资后返回城市子任务返回", "completed",
                          success=return_main.get("success"), page_state=return_main.get("page_state"))
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        logger.exception(
            "Cape island investment route hook failed leg_index=%s city=%s code=%s elapsed_ms=%s",
            int(leg_index),
            arrival_city,
            code,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    result = {
        "triggered": True,
        "status": str(investment.get("status") or "unknown"),
        "reason": investment.get("reason"),
        "degraded": bool(investment.get("degraded")),
        "unclassified_slots": list(investment.get("unclassified_slots") or []),
        "arrival_city": arrival_city,
        "open_city": open_city,
        "investment": investment,
        "return_main": return_main,
        "page_state": str(return_main.get("page_state") or "city_main"),
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }
    selected = investment.get("selected_option") or {}
    logger.info(
        "Cape island investment route result leg_index=%s status=%s reason=%s "
        "selected_slot=%s selected_category=%s selected_grade=%s returned_page=%s elapsed_ms=%s",
        int(leg_index),
        result["status"],
        result["reason"],
        selected.get("slot"),
        selected.get("category"),
        selected.get("grade"),
        result["page_state"],
        result["elapsed_ms"],
    )
    return result


async def _execute_rubbish_recycling_after_arrival(
    *,
    leg_index: int,
    leg: Dict[str, Any],
    app: Any,
    ocr: Any,
    vision: Any,
    city_shop_data: ResonancePcCityShopDataService,
) -> Dict[str, Any]:
    started_at = time.monotonic()
    arrival_city = str(leg.get("to_city") or "")
    logger.info(
        "Rubbish recycling route hook triggered leg_index=%s from_city=%s to_city=%s to_city_id=%s",
        int(leg_index),
        leg.get("from_city"),
        arrival_city,
        leg.get("to_city_id"),
    )
    try:
        open_city = await asyncio.to_thread(
            resonance_pc_open_city_panel_from_main,
            app=app,
            vision=vision,
        )
        observe_operation("rubbish_recycling.execute", "垃圾回收子任务", "started")
        recycling = await asyncio.to_thread(
            resonance_pc_execute_rubbish_recycling_from_city_panel,
            city_name=arrival_city,
            app=app,
            vision=vision,
            resonance_pc_city_shop_data=city_shop_data,
        )
        observe_operation("rubbish_recycling.execute", "垃圾回收子任务返回", "completed",
                          status=recycling.get("status"), reason=recycling.get("reason"),
                          final_state=recycling.get("final_state"),
                          reward_overlay_seen=recycling.get("reward_overlay_seen"))
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        logger.exception(
            "Rubbish recycling route hook failed leg_index=%s city=%s code=%s elapsed_ms=%s",
            int(leg_index),
            arrival_city,
            code,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    result = {
        "triggered": True,
        "attempted": True,
        "status": str(recycling.get("status") or "unknown"),
        "reason": recycling.get("reason"),
        "city_id": str(recycling.get("city_id") or leg.get("to_city_id") or ""),
        "city_name": str(recycling.get("city_name") or arrival_city),
        "initial_state": recycling.get("initial_state"),
        "final_state": recycling.get("final_state"),
        "reward_overlay_seen": bool(recycling.get("reward_overlay_seen")),
        "open_city": open_city,
        "recycling": recycling,
        "page_state": str(recycling.get("page_state") or "city_panel"),
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }
    logger.info(
        "Rubbish recycling route result leg_index=%s city=%s status=%s reason=%s page_state=%s elapsed_ms=%s",
        int(leg_index),
        result["city_name"],
        result["status"],
        result["reason"],
        result["page_state"],
        result["elapsed_ms"],
    )
    return result


def _summarize_negotiation_execution(
    execution: Dict[str, Any],
    final_sale: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []

    def append_city_trade(city_trade: Any) -> None:
        if not isinstance(city_trade, dict):
            return
        city = str(city_trade.get("current_city") or "")
        for result_key, operation in (("sell", "raise"), ("buy", "bargain")):
            operation_result = city_trade.get(result_key)
            if not isinstance(operation_result, dict):
                continue
            negotiation = operation_result.get("negotiation")
            if not isinstance(negotiation, dict):
                continue
            entries.append(
                {
                    "city": city,
                    "operation": operation,
                    **dict(negotiation),
                }
            )

    for leg_result in execution.get("leg_results") or []:
        if isinstance(leg_result, dict):
            append_city_trade(leg_result.get("city_trade"))
    append_city_trade(final_sale)

    degraded_entries = [
        entry
        for entry in entries
        if bool(entry.get("requested_to_cap"))
        and str(entry.get("stop_reason") or "") in {
            "attempt_limit_reached", "attempts_exhausted",
        }
    ]
    warnings = []
    for entry in degraded_entries:
        operation_name = "买入砍价" if entry.get("operation") == "bargain" else "卖出抬价"
        city_prefix = f"{entry.get('city')} " if entry.get("city") else ""
        attempts = int(entry.get("attempts_used") or 0)
        if entry.get("stop_reason") == "attempts_exhausted":
            warnings.append(
                f"{city_prefix}{operation_name}次数已耗尽，本次尝试 {attempts} 次且未达到 20%，"
                "已按当前价格继续成交。"
            )
        else:
            warnings.append(
                f"{city_prefix}{operation_name}尝试 {attempts} 次仍未达到 20%，"
                "已按当前价格继续成交。"
            )
    return {
        "negotiation_results": entries,
        "negotiation_attempts_used_total": sum(
            int(entry.get("attempts_used") or 0) for entry in entries
        ),
        "negotiation_actual_fatigue_used": sum(
            int(entry.get("actual_fatigue_used") or 0) for entry in entries
        ),
        "negotiation_cap_miss_count": len(degraded_entries),
        "negotiation_degraded": bool(degraded_entries),
        "warnings": warnings,
    }


async def _execute_freight_reposition(
    route: List[Dict[str, Any]], *, page_state: str, app, ocr, vision,
    arrival_timeout_seconds: float, use_fatigue_medicine: bool,
    allowed_fatigue_medicines: Optional[List[str]], fatigue_medicine_max_uses: int,
) -> Dict[str, Any]:
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    results: List[Dict[str, Any]] = []
    if page_state == "city_panel":
        restored = await asyncio.to_thread(resonance_pc_go_city_main_direct, app=app, vision=vision)
        if restored.get("success") is not True or restored.get("page_state") != "city_main":
            _raise_error("reposition_main_not_restored", "Cannot depart for route start", restored)
    elif page_state != "city_main":
        _raise_error("reposition_invalid_start_page", "Cannot depart from unknown page", {"page_state": page_state})
    for index, leg in enumerate(route):
        _check_trade_cancelled()
        fields = {"current_city": leg["from_city"], "from_city": leg["from_city"],
                  "to_city": leg["to_city"], "data": {"reposition": {
                      "leg_index": index, "leg_count": len(route), "leg": leg}}}
        if reporter:
            await reporter.emit("reposition", "started", **fields)
        try:
            travel = await asyncio.to_thread(
                resonance_pc_intercity_depart_and_wait,
                to_city_name=leg["to_city"], from_city_name=leg["from_city"],
                enter_station_timeout_seconds=arrival_timeout_seconds,
                location_file_path="data/meta/location_pc.json", city_search_region=[130, 120, 1000, 500],
                drag_center=[640, 360], drag_span_px=450, max_search_steps=12,
                fallback_enabled=True, target_match_mode="contains", click_y_offset=-15,
                drag_duration_sec=1.0, drag_hold_sec=0.5, auto_pickup=False,
                use_fatigue_medicine=use_fatigue_medicine,
                allowed_fatigue_medicines=allowed_fatigue_medicines or [],
                fatigue_medicine_max_uses=fatigue_medicine_max_uses, app=app, ocr=ocr, vision=vision,
            )
        except Exception as exc:
            if not hasattr(exc, "code"):
                raise
            return {"success": False, "status": "cancelled" if "cancel" in str(exc.code) else "failed",
                    "reason": str(exc.code), "page_state": "unknown", "leg_results": results,
                    "blocked_leg": leg, "error": {"code": str(exc.code), "message": str(exc),
                                                  "detail": dict(getattr(exc, "detail", {}) or {})}}
        results.append({"leg": dict(leg), "travel": travel})
        blocked = travel.get("status") == "blocked"
        confirmed = travel.get("success") is True and not blocked
        if reporter:
            await reporter.emit("reposition", "completed" if confirmed else "blocked" if blocked else "failed",
                                current_city=leg["to_city"] if confirmed else leg["from_city"],
                                data={"reposition": {"leg_index": index, "leg_count": len(route),
                                                     "travel": travel}})
        if not confirmed:
            return {"success": False, "status": "blocked" if blocked else "failed",
                    "reason": travel.get("reason") or "reposition_arrival_not_confirmed",
                    "leg_results": results, "page_state": travel.get("page_state", "unknown"),
                    "blocked_at": travel.get("blocked_at"), "blocked_leg": leg}
    opened = await asyncio.to_thread(resonance_pc_open_city_panel_from_main, app=app, vision=vision)
    if opened.get("success") is not True:
        _raise_error("reposition_panel_not_opened", "Cannot confirm route starting city", opened)
    current = opened["city"]
    if current.get("city_name") != route[-1]["to_city"]:
        _raise_error("reposition_city_mismatch", "Route starting city was not confirmed", current)
    return {"success": True, "status": "completed", "leg_results": results,
            "page_state": "city_panel", "current_city": current}


async def _preview_trade_plan_from_start_city(
    start_city_id: str,
    fatigue_budget: int = 700,
    cargo_capacity: int = 750,
    book_budget: Optional[int] = 0,
    trade_mode: str = "profit",
    book_policy: str = "profit",
    negotiation_policy: str = "auto",
    fixed_route_city_ids: Optional[List[str]] = None,
    reposition_to_route: bool = False,
    target_profit: Optional[int] = None,
    book_profit_threshold: int = 500000,
    bargain_success_rates_bps: Optional[List[Any]] = [5000],
    bargain_step_bps: Optional[Any] = 1000,
    raise_success_rates_bps: Optional[List[Any]] = [5000],
    raise_step_bps: Optional[Any] = 1000,
    available_city_ids: Optional[List[str]] = None,
    required_end_city_ids: Optional[List[str]] = None,
    city_prestige: Optional[Dict[str, Any]] = None,
    product_unlocks: Optional[Dict[str, Any]] = None,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
    reporter: _TradeProgressReporter | None = None,
) -> Dict[str, Any]:
    request = normalize_planning_inputs(locals())
    normalized_start_city_id = str(start_city_id or "").strip()
    if not normalized_start_city_id:
        raise ValueError("start_city_id is required")
    expected_fatigue_to_cap(
        success_rates_bps=[5000] if bargain_success_rates_bps is None else bargain_success_rates_bps,
        step_bps=1000 if bargain_step_bps is None else bargain_step_bps,
    )
    expected_fatigue_to_cap(
        success_rates_bps=[5000] if raise_success_rates_bps is None else raise_success_rates_bps,
        step_bps=1000 if raise_step_bps is None else raise_step_bps,
    )
    if start_mismatch(request, normalized_start_city_id):
        result = plan_view(fixed_start_stop(request), request, kind="preview")
        result.update(success=False, status="stopped", preview=True, page_state="not_applicable")
        result.update(initial_city={"city_id": normalized_start_city_id, "source": "user_input"},
                      market_refreshed=False, market_source=None, market_stale_reason=None,
                      market_fetched_at=None)
        return result
    if resonance_pc_market_data is None or resonance_pc_trade_planner is None:
        raise RuntimeError("preview_trade_plan_flow requires market-data and planner services")

    if reporter is not None:
        await reporter.emit(
            "market",
            "started",
            current_city=normalized_start_city_id,
            data={"source": "refresh"},
        )
    market = await asyncio.to_thread(
        resonance_pc_market_refresh,
        force=True,
        resonance_pc_market_data=resonance_pc_market_data,
    )
    snapshot_id = str(market.get("snapshot_id") or "")
    stale = bool(market.get("stale"))
    market_source = "fallback_cache" if stale else "refresh"
    cities = market.get("cities") if isinstance(market.get("cities"), dict) else {}
    city_payload = cities.get(normalized_start_city_id)
    start_city_name = (
        str(city_payload.get("name") or normalized_start_city_id)
        if isinstance(city_payload, dict)
        else normalized_start_city_id
    )
    if reporter is not None:
        await reporter.emit(
            "market",
            "completed",
            current_city=start_city_name,
            snapshot_id=snapshot_id,
            data={"source": market_source, "stale_reason": market.get("stale_reason")},
        )
        await reporter.emit(
            "planning",
            "started",
            current_city=start_city_name,
            snapshot_id=snapshot_id,
        )
    solver_progress_callback = (
        None
        if reporter is None
        else lambda payload: reporter.emit_from_worker(
            "planning",
            "progress",
            data=payload,
        )
    )
    with trade_solver_progress(solver_progress_callback):
        plan = await asyncio.to_thread(
            resonance_pc_trade_plan_optimal_route,
            current_city_id=normalized_start_city_id,
            **request,
            snapshot_id=market.get("snapshot_id"),
            resonance_pc_trade_planner=resonance_pc_trade_planner,
        )
    plan = plan_view(plan, request, kind="preview")
    route = plan["route"]
    if reporter is not None:
        await reporter.emit(
            "planning",
            "completed",
            leg_count=len(route),
            current_city=start_city_name,
            snapshot_id=snapshot_id,
            data=planning_event_data(plan),
        )
    result = dict(plan)
    result.update(
        {
            "success": plan["planning_status"] == "ok" and bool(route),
            "status": "planned" if plan["planning_status"] == "ok" and route else "stopped",
            "preview": True,
            "market_refreshed": not stale,
            "market_source": market_source,
            "market_stale_reason": market.get("stale_reason"),
            "market_fetched_at": market.get("fetched_at"),
            "initial_city": {
                "city_id": normalized_start_city_id,
                "city_name": start_city_name,
                "source": "user_input",
            },
            "page_state": "not_applicable",
        }
    )
    return result


@action_info(
    name="resonance_pc.preview_trade_plan_flow",
    public=True,
    read_only=False,
    description="Refresh market data and calculate a PC trade route from a user-selected start city.",
)
@requires_services(
    resonance_pc_market_data="resonance_pc_market_data",
    resonance_pc_trade_planner="resonance_pc_trade_planner",
    event_bus="core/event_bus",
)
@_with_trade_progress
async def resonance_pc_preview_trade_plan_flow(
    start_city_id: str,
    fatigue_budget: int = 700,
    cargo_capacity: int = 750,
    book_budget: Optional[int] = 0,
    trade_mode: str = "profit",
    book_policy: str = "profit",
    negotiation_policy: str = "auto",
    fixed_route_city_ids: Optional[List[str]] = None,
    reposition_to_route: bool = False,
    target_profit: Optional[int] = None,
    book_profit_threshold: int = 500000,
    bargain_success_rates_bps: Optional[List[Any]] = [5000],
    bargain_step_bps: Optional[Any] = 1000,
    raise_success_rates_bps: Optional[List[Any]] = [5000],
    raise_step_bps: Optional[Any] = 1000,
    available_city_ids: Optional[List[str]] = None,
    required_end_city_ids: Optional[List[str]] = None,
    city_prestige: Optional[Dict[str, Any]] = None,
    product_unlocks: Optional[Dict[str, Any]] = None,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
    event_bus: EventBus | None = None,
    context: ExecutionContext | None = None,
) -> Dict[str, Any]:
    del event_bus, context
    return await _preview_trade_plan_from_start_city(
        start_city_id=start_city_id,
        fatigue_budget=fatigue_budget,
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        trade_mode=trade_mode,
        book_policy=book_policy,
        negotiation_policy=negotiation_policy,
        fixed_route_city_ids=fixed_route_city_ids,
        reposition_to_route=reposition_to_route,
        target_profit=target_profit,
        book_profit_threshold=book_profit_threshold,
        bargain_success_rates_bps=bargain_success_rates_bps,
        bargain_step_bps=bargain_step_bps,
        raise_success_rates_bps=raise_success_rates_bps,
        raise_step_bps=raise_step_bps,
        available_city_ids=available_city_ids,
        required_end_city_ids=required_end_city_ids,
        city_prestige=city_prestige,
        product_unlocks=product_unlocks,
        resonance_pc_market_data=resonance_pc_market_data,
        resonance_pc_trade_planner=resonance_pc_trade_planner,
        reporter=_ACTIVE_PROGRESS_REPORTER.get(),
    )


@action_info(
    name="resonance_pc.auto_cycle_trade_flow",
    public=True,
    read_only=False,
    description="Plan and execute one exact full-budget ResonancePc trade route from city-main UI.",
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
    persistent_data="core/persistent_data",
)
@_with_trade_progress
async def resonance_pc_auto_cycle_trade_flow(
    fatigue_budget: int = 700,
    cargo_capacity: int = 750,
    book_budget: Optional[int] = 0,
    trade_mode: str = "profit",
    book_policy: str = "profit",
    negotiation_policy: str = "auto",
    fixed_route_city_ids: Optional[List[str]] = None,
    reposition_to_route: bool = False,
    target_profit: Optional[int] = None,
    book_profit_threshold: int = 500000,
    negotiation_max_attempts: int = 5,
    bargain_success_rates_bps: Optional[List[Any]] = [5000],
    bargain_step_bps: Optional[Any] = 1000,
    raise_success_rates_bps: Optional[List[Any]] = [5000],
    raise_step_bps: Optional[Any] = 1000,
    available_city_ids: Optional[List[str]] = None,
    required_end_city_ids: Optional[List[str]] = None,
    city_prestige: Optional[Dict[str, Any]] = None,
    product_unlocks: Optional[Dict[str, Any]] = None,
    use_fatigue_medicine: bool = False,
    allowed_fatigue_medicines: Optional[List[str]] = None,
    fatigue_medicine_max_uses: int = 4,
    arrival_timeout_seconds: float = 3600.0,
    auto_cape_island_investment: bool = True,
    auto_trade_goods_investment: bool = False,
    trade_goods_investment_mode: str = "unlock",
    auto_rubbish_recycling: bool = True,
    auto_sparkling_water: bool = False,
    auto_bento: bool = False,
    bento_priority: Optional[List[str]] = None,
    base_fatigue_reserve: int = 200,
    auto_pickup: bool = False,
    recovery_snapshot: Optional[Dict[str, Any]] = None,
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
    persistent_data: PersistentDataService | None = None,
) -> Dict[str, Any]:
    del event_bus
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    request = normalize_planning_inputs(locals())
    for name, value in (("auto_pickup", auto_pickup), ("use_fatigue_medicine", use_fatigue_medicine),
                        ("auto_cape_island_investment", auto_cape_island_investment),
                        ("auto_trade_goods_investment", auto_trade_goods_investment),
                        ("auto_rubbish_recycling", auto_rubbish_recycling)):
        if type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
    integer("fatigue_medicine_max_uses", fatigue_medicine_max_uses)
    trade_goods_investment_mode = normalize_investment_mode(trade_goods_investment_mode)
    if type(auto_sparkling_water) is not bool:
        raise ValueError("auto_sparkling_water must be a boolean")
    if type(auto_bento) is not bool:
        raise ValueError("auto_bento must be a boolean")
    if auto_bento:
        bento_priority = validate_bento_priority(
            ["work_meals", "love_bentos"] if bento_priority is None else bento_priority,
        )
    if type(base_fatigue_reserve) is not int or base_fatigue_reserve < 0:
        raise ValueError("base_fatigue_reserve must be a nonnegative integer")
    if auto_sparkling_water:
        recovery_snapshot = validate_recovery_snapshot(recovery_snapshot)
        if persistent_data is None:
            raise RuntimeError("Automatic sparkling water requires persistent_data")
    if isinstance(negotiation_max_attempts, bool) or not isinstance(
        negotiation_max_attempts,
        int,
    ):
        raise ValueError("negotiation_max_attempts must be an integer")
    normalized_negotiation_max_attempts = int(negotiation_max_attempts)
    if not 1 <= normalized_negotiation_max_attempts <= MAX_NEGOTIATION_MAX_ATTEMPTS:
        raise ValueError(
            f"negotiation_max_attempts must be between 1 and {MAX_NEGOTIATION_MAX_ATTEMPTS}"
        )
    if isinstance(arrival_timeout_seconds, bool):
        raise ValueError("arrival_timeout_seconds must be a positive number")
    try:
        normalized_arrival_timeout_seconds = float(arrival_timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("arrival_timeout_seconds must be a positive number") from exc
    if not math.isfinite(normalized_arrival_timeout_seconds) or normalized_arrival_timeout_seconds <= 0:
        raise ValueError("arrival_timeout_seconds must be a positive number")
    expected_fatigue_to_cap(
        success_rates_bps=(
            [5000] if bargain_success_rates_bps is None else bargain_success_rates_bps
        ),
        step_bps=1000 if bargain_step_bps is None else bargain_step_bps,
    )
    expected_fatigue_to_cap(
        success_rates_bps=[5000] if raise_success_rates_bps is None else raise_success_rates_bps,
        step_bps=1000 if raise_step_bps is None else raise_step_bps,
    )
    if (
        app is None
        or ocr is None
        or vision is None
        or resonance_pc_city_shop_data is None
        or resonance_pc_market_data is None
        or resonance_pc_trade_planner is None
        or state_store is None
    ):
        raise RuntimeError("auto_cycle_trade_flow requires app/ocr/vision/data/planner/state services")

    # The only market refresh for this task happens after current-city recognition
    # and before the exact full-route plan is built.
    if reporter is not None:
        await reporter.emit("target", "started")
        await reporter.emit("city", "started")
    opened = await asyncio.to_thread(resonance_pc_open_city_panel_from_main, app=app, vision=vision)
    current = opened["city"]
    page_state = "city_panel"
    if reporter is not None:
        await reporter.emit("target", "completed")
        await reporter.emit(
            "city",
            "completed",
            current_city=str(current.get("city_name") or ""),
            data={"city_key": current.get("city_key")},
        )

    if reporter is not None:
        await reporter.emit("market", "started", current_city=str(current.get("city_name") or ""))
    refresh = await asyncio.to_thread(
        resonance_pc_market_refresh,
        force=True,
        resonance_pc_market_data=resonance_pc_market_data,
    )
    if reporter is not None:
        await reporter.emit(
            "market",
            "completed",
            current_city=str(current.get("city_name") or ""),
            snapshot_id=str(refresh.get("snapshot_id") or ""),
        )
        await reporter.emit("planning", "started", snapshot_id=str(refresh.get("snapshot_id") or ""))
    solver_progress_callback = (
        None
        if reporter is None
        else lambda payload: reporter.emit_from_worker(
            "planning",
            "progress",
            data=payload,
        )
    )
    mismatch = False
    if request["trade_mode"] == "fixed":
        cities = resonance_pc_market_data.get_all_travel_fatigue()["cities"]
        actual_city_id = next((str(key) for key, value in cities.items()
                               if value == current.get("city_name")), None)
        if actual_city_id is None:
            _raise_error("unsupported_city", "Current city could not be mapped to the travel graph", current)
        current["city_id"] = actual_city_id
        mismatch = start_mismatch(request, actual_city_id)
    if mismatch:
        plan = fixed_start_stop(request)
    else:
        with trade_solver_progress(solver_progress_callback):
            plan = await asyncio.to_thread(
                resonance_pc_trade_plan_optimal_route,
                current_city=str(current.get("city_name") or ""),
                current_city_key=str(current.get("city_key") or ""),
                **request,
                snapshot_id=refresh.get("snapshot_id"),
                resonance_pc_trade_planner=resonance_pc_trade_planner,
            )
    plan = plan_view(plan, request, kind="run", auto_bento=auto_bento,
                     investment=auto_cape_island_investment, rubbish=auto_rubbish_recycling)
    current.setdefault("city_id", plan.get("start_city_id"))
    route = plan["route"]
    water_plan = {
        "planned": False,
        "reason": "disabled",
        "drink_count": 0,
        "base_fatigue_reserve": base_fatigue_reserve,
    }
    if auto_sparkling_water:
        free_uses = recovery_snapshot["recovery"]["sparkling_water"]["remaining_free_uses"]
        water_plan.update(remaining_free_uses=free_uses)
        if free_uses:
            water_plan.update(select_last_water_arrival(
                route, lambda city: _has_recovery_rest_point(city, resonance_pc_city_shop_data),
            ))
        else:
            water_plan["reason"] = "no_remaining_free_uses"
        logger.info("Sparkling water selection=%s", water_plan)
    plan["sparkling_water_plan"] = water_plan
    plan = plan_view(plan, request, kind="run", auto_bento=auto_bento, water_plan=water_plan,
                     investment=auto_cape_island_investment, rubbish=auto_rubbish_recycling)
    plan["sparkling_water_plan"] = water_plan
    if auto_trade_goods_investment:
        for visit in plan["city_visits"]:
            if visit["city_index"] == 0:
                continue
            phases = visit["phases"]
            sale_index = next(index for index, phase in enumerate(phases)
                              if phase["key"] in {"sell", "final_sale"})
            phases.insert(sale_index, {"key": "trade_goods_investment",
                                       "status": "waiting", "reason": None})
    if reporter is not None:
        await reporter.emit(
            "planning",
            "completed",
            leg_count=len(route),
            city_count=len(route) + 1 if route else 0,
            current_city=str(current.get("city_name") or ""),
            snapshot_id=str(refresh.get("snapshot_id") or ""),
            data=planning_event_data(plan),
        )
    execution: Dict[str, Any] = {
        "status": "not_started",
        "reason": plan.get("reason"),
        "negotiation_max_attempts": normalized_negotiation_max_attempts,
        "arrival_timeout_seconds": normalized_arrival_timeout_seconds,
        "completed_leg_count": 0,
        "completed_route": [],
        "leg_results": [],
        "blocked_at": None,
        "blocked_leg": None,
        "fatigue_medicine_used": [],
        "fatigue_medicine_use_count": 0,
        "cape_island_triggered_count": 0,
        "cape_island_invested_count": 0,
        "cape_island_skipped_count": 0,
        "rubbish_recycling_triggered_count": 0,
        "rubbish_recycling_status": "not_triggered",
        "rubbish_recycling_city_id": None,
        "rubbish_recycling_city_name": None,
    }
    final_sale: Optional[Dict[str, Any]] = None
    reposition_execution = None
    if plan.get("status") == "ok" and route and plan["reposition"]["required"]:
        reposition_execution = await _execute_freight_reposition(
            plan["reposition"]["route"], page_state=page_state,
            app=app, ocr=ocr, vision=vision,
            arrival_timeout_seconds=normalized_arrival_timeout_seconds,
            use_fatigue_medicine=use_fatigue_medicine,
            allowed_fatigue_medicines=allowed_fatigue_medicines,
            fatigue_medicine_max_uses=fatigue_medicine_max_uses,
        )
        page_state = reposition_execution["page_state"]
        if reposition_execution["success"] is not True:
            execution.update(reposition_execution)
    if plan.get("status") == "ok" and route and (reposition_execution is None or reposition_execution["success"]):
        execution = await _execute_route(
            route=route,
            start_page_state=page_state,
            use_fatigue_medicine=bool(use_fatigue_medicine),
            allowed_fatigue_medicines=allowed_fatigue_medicines or [],
            fatigue_medicine_max_uses=int(fatigue_medicine_max_uses),
            negotiation_max_attempts=normalized_negotiation_max_attempts,
            arrival_timeout_seconds=normalized_arrival_timeout_seconds,
            auto_pickup=auto_pickup,
            app=app,
            ocr=ocr,
            vision=vision,
            city_shop_data=resonance_pc_city_shop_data,
            state_store=state_store,
            auto_cape_island_investment=bool(auto_cape_island_investment),
            auto_trade_goods_investment=auto_trade_goods_investment,
            trade_goods_investment_mode=trade_goods_investment_mode,
            auto_rubbish_recycling=bool(auto_rubbish_recycling),
            engine=engine,
            sparkling_water_plan=water_plan,
            persistent_data=persistent_data,
            recovery_context=context,
            recovery_travel_costs=(resonance_pc_market_data.get_all_travel_fatigue()["costs"] if water_plan.get("planned") else None),
        )
        page_state = str(execution.get("page_state") or "city_main")

        if str(execution.get("status") or "").lower() == "completed":
            endpoint_city = str(route[-1].get("to_city") or "")
            logger.info("[FinalSale] phase=started city=%s page_state=%s", endpoint_city, page_state)
            if page_state == "city_main":
                await asyncio.to_thread(resonance_pc_open_city_panel_from_main, app=app, vision=vision)
                page_state = "city_panel"
            if reporter is not None and not auto_trade_goods_investment:
                await reporter.emit(
                    "final_sale",
                    "started",
                    city_index=len(route),
                    city_count=len(route) + 1,
                    leg_count=len(route),
                    current_city=endpoint_city,
                    data={"raise_to_cap": bool(route[-1].get("raise_to_cap"))},
                )
            try:
                final_sale = await asyncio.to_thread(
                    _execute_city_trade_inside_current_city,
                    current_city=endpoint_city,
                    buy_products=[],
                    books_used=0,
                    sell_raise_to_cap=bool(route[-1].get("raise_to_cap")),
                    buy_bargain_to_cap=False,
                    auto_trade_goods_investment=auto_trade_goods_investment,
                    trade_goods_investment_mode=trade_goods_investment_mode,
                    negotiation_max_attempts=normalized_negotiation_max_attempts,
                    app=app,
                    ocr=ocr,
                    vision=vision,
                    city_shop_data=resonance_pc_city_shop_data,
                    progress_context={
                        "leg_index": len(route),
                        "leg_count": len(route),
                        "city_index": len(route),
                        "city_count": len(route) + 1,
                        "current_city": endpoint_city,
                        "from_city": endpoint_city,
                        "to_city": endpoint_city,
                    },
                )
            except Exception as exc:
                if not hasattr(exc, "code"):
                    raise
                final_sale = {"success": False, "page_state": "unknown", "reason": str(exc.code),
                              "error": {"code": str(exc.code), "message": str(exc),
                                        "detail": dict(getattr(exc, "detail", {}) or {})}}
            page_state = str(final_sale.get("page_state") or "unknown")
            sale_confirmed = final_sale.get("success") is True and page_state == "city_main"
            sale_log = logger.info if sale_confirmed else logger.error
            sale_log(
                "[FinalSale] phase=%s city=%s sold_confirmed=%s sell_result=%s page_state=%s "
                "reason=%s error=%s",
                "completed" if sale_confirmed else "failed", endpoint_city,
                (final_sale.get("sell") or {}).get("sold_confirmed"),
                (final_sale.get("sell") or {}).get("sell_result"), page_state,
                final_sale.get("reason"), final_sale.get("error"),
            )
            if reporter is not None:
                await reporter.emit(
                    "final_sale",
                    "completed" if final_sale.get("success") is True and page_state == "city_main" else "failed",
                    city_index=len(route),
                    city_count=len(route) + 1,
                    leg_count=len(route),
                    current_city=endpoint_city,
                    data={"final_sale": final_sale},
                )
    elif page_state == "city_panel" and reposition_execution is None:
        cleanup = await asyncio.to_thread(
            resonance_pc_go_city_main_direct,
            app=app,
            vision=vision,
        )
        execution["page_cleanup"] = cleanup
        page_state = str(cleanup.get("page_state") or "city_main")

    execution["reposition_execution"] = reposition_execution
    negotiation_execution = _summarize_negotiation_execution(execution, final_sale)
    execution.update(negotiation_execution)
    execution["negotiation_max_attempts"] = normalized_negotiation_max_attempts
    execution["arrival_timeout_seconds"] = normalized_arrival_timeout_seconds
    execution_status = str(execution.get("status") or "not_started").lower()
    if final_sale is not None and (final_sale.get("success") is not True or page_state != "city_main"):
        status = "cancelled" if "cancel" in str(final_sale.get("reason")) else "failed"
        reason = final_sale.get("reason") or "final_sale_not_confirmed"
        success = False
    elif execution_status == "blocked":
        status = "blocked"
        reason = execution.get("reason") or "travel_blocked"
        success = False
    elif execution_status == "completed" and final_sale is not None and final_sale.get("success") is True:
        status = "completed"
        reason = None
        success = True
    else:
        status = "failed" if execution_status == "failed" else "cancelled" if execution_status == "cancelled" else "stopped"
        reason = execution.get("reason") or plan.get("reason") or plan["planning_status"]
        success = False

    bento_pending = bool(auto_bento and success and status == "completed" and final_sale is not None)
    result = dict(plan)
    result_warnings = list(result.get("warnings") or [])
    result_warnings.extend(negotiation_execution["warnings"])
    for leg_result in execution.get("leg_results") or []:
        buy = ((leg_result.get("city_trade") or {}).get("buy") or {})
        result_warnings.extend({**warning, "leg_index": leg_result.get("leg_index", leg_result.get("index"))}
                               for warning in buy.get("warnings") or [])
    goods_investment_summary = _summarize_goods_investment(
        execution, final_sale, enabled=auto_trade_goods_investment, mode=trade_goods_investment_mode,
    )
    execution["trade_goods_investment"] = goods_investment_summary
    if auto_trade_goods_investment:
        result_warnings.append("投资支出不计入贸易收益；商品升级后的进货量可能与预计方案不同。")
    result.update(
        {
            "success": success,
            "status": status,
            "reason": reason,
            "negotiation_max_attempts": normalized_negotiation_max_attempts,
            "arrival_timeout_seconds": normalized_arrival_timeout_seconds,
            "warnings": result_warnings,
            "execution": execution,
            "final_sale": final_sale,
            "trade_goods_investment": goods_investment_summary,
            "bento_pending": bento_pending,
            "sparkling_water": execution.get("sparkling_water") or {
                "triggered": False, "status": "not_triggered", "reason": water_plan.get("reason"),
            },
            "blocked_at": execution.get("blocked_at"),
            "blocked_leg": execution.get("blocked_leg"),
            "fatigue_medicine_used": list(execution.get("fatigue_medicine_used") or []),
            "fatigue_medicine_use_count": int(execution.get("fatigue_medicine_use_count") or 0),
            "initial_city": {
                "city_id": current.get("city_id"),
                "city_name": current.get("city_name"),
                "city_key": current.get("city_key"),
            },
            "page_state": page_state,
        }
    )
    confirmed_books = sum(
        int(((item.get("city_trade") or {}).get("buy") or {}).get("book_result", {}).get("used") or 0)
        for item in execution.get("leg_results") or []
    )
    for item in execution.get("leg_results") or []:
        detail = (item.get("error") or {}).get("detail") or {}
        partial = (detail.get("book_result") or {}).get("used", detail.get("used_before_failure", 0))
        if type(partial) is int and partial >= 0:
            confirmed_books += partial
    result["resources"] = dict(reporter.resources) if reporter else {
        "confirmed_books_used": confirmed_books,
        "confirmed_negotiation_fatigue": negotiation_execution["negotiation_actual_fatigue_used"],
        "actual_fatigue": None, "actual_profit": None,
    }
    result["resources"]["confirmed_books_used"] = max(
        confirmed_books, result["resources"]["confirmed_books_used"],
    )
    if execution.get("error") or (final_sale or {}).get("error"):
        result["error"] = execution.get("error") or final_sale["error"]
    if reporter is not None:
        if bento_pending:
            await reporter.emit("bento", "started", city_index=len(route),
                                current_city=str(route[-1].get("to_city") or ""),
                                data={"phase": "onsite"})
        else:
            await reporter.emit(
                "route",
                "blocked" if status == "blocked" else ("failed" if not success else "completed"),
                leg_count=len(route),
                current_city=str(route[-1].get("to_city") or "") if route else str(current.get("city_name") or ""),
                data={"status": status, "reason": reason},
            )
    return result


def _log_trade_outcome(result: Dict[str, Any]) -> None:
    log = logger.info if result.get("success") is True else logger.error
    log(
        "[AutoTradeResult] success=%s status=%s reason=%s page_state=%s "
        "final_sale_success=%s error=%s",
        result.get("success"), result.get("status"), result.get("reason"),
        result.get("page_state"), (result.get("final_sale") or {}).get("success"),
        result.get("error"),
    )


@action_info(
    name="resonance_pc.finish_auto_cycle_trade",
    public=True,
    read_only=False,
    description="Merge the optional bento sub-task into the completed freight result.",
)
@requires_services(event_bus="core/event_bus")
async def resonance_pc_finish_auto_cycle_trade(
    trade_result: Dict[str, Any],
    bento_framework: Optional[Dict[str, Any]] = None,
    auto_bento: bool = False,
    base_fatigue_reserve: int = 200,
    event_bus: EventBus | None = None,
    context: ExecutionContext | None = None,
) -> Dict[str, Any]:
    if (not isinstance(trade_result, dict) or type(auto_bento) is not bool
            or type(base_fatigue_reserve) is not int or base_fatigue_reserve < 0):
        raise ValueError("Invalid freight finalization inputs")
    result = dict(trade_result)
    pending = result.pop("bento_pending", False)
    progress_sequence = result.pop("bento_progress_sequence", 0)
    checkpoint = result.pop("bento_progress_checkpoint", None)
    if type(pending) is not bool or type(progress_sequence) is not int or progress_sequence < 0:
        raise ValueError("Invalid freight bento handoff")
    if pending and (not auto_bento or result.get("success") is not True
                    or result.get("status") != "completed" or result.get("page_state") != "city_main"):
        raise ValueError("Inconsistent freight bento handoff")
    if not pending:
        result["bento_consumption"] = {
            "success": True, "triggered": False, "status": "skipped",
            "reason": "disabled" if not auto_bento else "not_at_freight_end",
            "page_state": result.get("page_state", "unknown"),
        }
        _log_trade_outcome(result)
        return result

    nodes = bento_framework.get("nodes") if isinstance(bento_framework, dict) else None
    consume_node = nodes.get("consume") if isinstance(nodes, dict) else None
    child = consume_node.get("output") if isinstance(consume_node, dict) else None
    valid = (isinstance(child, dict) and type(child.get("success")) is bool
             and isinstance(child.get("status"), str) and isinstance(child.get("page_state"), str)
             and type(child.get("consumed_count")) is int
             and child["consumed_count"] >= 0
             and type(child.get("recovered_fatigue")) is int
             and 0 <= child["recovered_fatigue"] <= 2000
             and (type(child.get("computed_fatigue")) is int or
                  (child.get("computed_fatigue") is None and child["success"] is False
                   and child["consumed_count"] == 0))
             and child.get("target_recovery_amount") == 2000
             and child.get("allow_exceed_target") is False
             and child.get("base_fatigue_reserve") == base_fatigue_reserve
             and (child["consumed_count"] == 0 or child["computed_fatigue"] >= base_fatigue_reserve))
    if not valid:
        child_result = {"success": False, "status": "failed", "reason": "bento_result_invalid",
                        "page_state": "unknown", "triggered": True,
                        "error": "Bento sub-task did not return a valid consumption result"}
    else:
        child_result = {**child, "triggered": True}
    result["bento_consumption"] = child_result
    resources = dict(result.get("resources") or {})
    resources["bento_basic_recovered_fatigue"] = child_result.get("recovered_fatigue")
    resources["bento_computed_fatigue"] = child_result.get("computed_fatigue")
    result["resources"] = resources
    if child_result["success"] is not True or child_result["page_state"] != "city_main":
        result.update(success=False,
                      status="cancelled" if child_result["status"] == "cancelled" else "failed",
                      reason=(child_result.get("reason") or "bento_recovery_failed")
                      if child_result["success"] is not True else "bento_main_not_restored",
                      page_state=child_result["page_state"])
    else:
        result["page_state"] = "city_main"

    cid = str(context.data.get("cid") or "") if isinstance(context, ExecutionContext) else ""
    if event_bus is not None and cid:
        reporter = _TradeProgressReporter(event_bus, cid, asyncio.get_running_loop(), progress_sequence)
        reporter.fields.update(trade_mode=result.get("trade_mode", "profit"), request_kind="run")
        reporter.resources.update(resources)
        if isinstance(checkpoint, dict):
            reporter._total_units = checkpoint["total_units"]
            reporter._completed_units = {tuple(key) for key in checkpoint["completed_units"]}
            reporter._phase_keys = {tuple(key) for key in checkpoint["phase_keys"]}
            reporter.route_revision = checkpoint.get("route_revision", 0)
        await reporter.emit("bento", "completed" if result["success"] else "failed",
                            city_index=len(result.get("route") or []),
                            current_city=str((result.get("route") or [{}])[-1].get("to_city") or ""),
                            data={"result": child_result})
        await reporter.emit("task", "completed" if result["success"] else "cancelled" if result["status"] == "cancelled" else "failed",
                            data={"status": result["status"], "reason": result.get("reason")})
    _log_trade_outcome(result)
    return result
