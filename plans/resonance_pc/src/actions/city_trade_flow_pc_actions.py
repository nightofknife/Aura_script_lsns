"""City-panel trade UI actions and auto-cycle trade flow for ResonancePc."""

from __future__ import annotations

import time
import asyncio
import contextvars
import functools
import threading
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger

from ..services.city_shop_data_pc_service import ResonancePcCityShopDataService
from ..services.resonance_pc_market_data_service import ResonancePcMarketDataService
from ..services.resonance_pc_trade_exact_solver import expected_fatigue_to_cap
from ..services.resonance_pc_trade_planner_service import ResonancePcTradePlannerService
from .city_travel_pc_actions import resonance_pc_intercity_depart_and_wait
from .market_data_pc_actions import resonance_pc_market_refresh
from .purchase_book_pc_actions import resonance_pc_use_purchase_books
from .trade_negotiation_pc_actions import (
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
            "schema": _TRADE_PROGRESS_SCHEMA,
            "cid": self._cid,
            "sequence": sequence,
            "stage": str(stage),
            "state": str(state),
        }
        payload.update({key: value for key, value in fields.items() if value is not None})
        try:
            await self._event_bus.publish(Event(name=_TRADE_PROGRESS_EVENT, payload=payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("PC trade progress event could not be published: %s", exc)

    def emit_from_worker(self, stage: str, state: str, **fields: Any) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(self.emit(stage, state, **fields), self._loop)
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
        try:
            if reporter is not None:
                await reporter.emit("task", "started")
            result = await func(*args, **kwargs)
            if reporter is not None:
                await reporter.emit("task", "completed", data={"status": result.get("status")})
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
    context = dict(_WORKER_PROGRESS_CONTEXT.get())
    context.update(fields)
    reporter.emit_from_worker(stage, state, **context)


_VISIT_BUTTON_REGION = [1000, 450, 250, 70]
_CITY_NAME_REGION = [170, 520, 400, 50]
_SHOP_MENU_REGION = [720, 280, 280, 420]
_BUY_PRODUCTS_REGION = [620, 130, 210, 520]
_BUY_BUTTON_REGION = [1000, 630, 140, 50]
_BUY_CONFIRM_PANEL_REGION = [850, 80, 180, 60]
_BUY_CONFIRM_BUTTON_REGION = [900, 620, 330, 70]
_SELL_ALL_REGION = [1140, 80, 110, 50]
_SELL_BUTTON_REGION = [1000, 630, 120, 40]

_BACK_POINT = (82, 37)
_CITY_MAIN_POINT = (198, 37)
_BACK_BUTTON_TEMPLATE = "templates/nav_back_button.png"
_CITY_MAIN_BUTTON_TEMPLATE = "templates/nav_city_main_button.png"
_BACK_BUTTON_REGION = [0, 0, 170, 80]
_CITY_MAIN_BUTTON_REGION = [140, 0, 130, 80]
_NAV_BUTTON_THRESHOLD = 0.86
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
    region_tuple = _coerce_region(region)
    capture = app.capture(rect=region_tuple)
    if not capture.success:
        _raise_error("capture_failed", "failed to capture screen region", {"region": list(region_tuple)})
    multi = ocr.recognize_all(source_image=capture.image)
    items: List[Dict[str, Any]] = []
    for item in getattr(multi, "results", []) or []:
        text = str(getattr(item, "text", "") or "")
        if not text.strip():
            continue
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
        items.append(payload)
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
    app.click(x=x, y=y)
    return {"clicked": True, "x": x, "y": y, "text": hit.get("text"), "marker": hit.get("marker")}


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
        last = _match_template(app, vision, template, region, threshold)
        if last.get("found"):
            return last
        if time.monotonic() >= deadline:
            return last
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
        return {"closed": False, "found": False, "kind": kind, "first_match": first}

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
    retried = False
    if recheck.get("found"):
        app.click(x=_SETTLEMENT_EXIT_POINT[0], y=_SETTLEMENT_EXIT_POINT[1])
        retried = True
        time.sleep(0.8)
    return {
        "closed": True,
        "found": True,
        "kind": kind,
        "first_match": first,
        "recheck": recheck,
        "retried": retried,
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
    match = _wait_template(
        app,
        vision,
        template,
        region,
        threshold=_NAV_BUTTON_THRESHOLD,
        timeout_sec=1.0,
        interval_sec=0.2,
    )
    center = match.get("center")
    if not match.get("found") or not isinstance(center, list) or len(center) != 2:
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
    app.click(x=x, y=y)
    time.sleep(max(float(wait_sec), 0.0))
    return {
        "success": True,
        "page_state": page_state,
        "x": x,
        "y": y,
        "template": template,
        "match": match,
    }


def _drag_buy_list(app: Any, controller: Any) -> None:
    app.move_to(x=_BUY_SCROLL_START[0], y=_BUY_SCROLL_START[1], duration=0.1)
    pressed = False
    try:
        controller.mouse_down("left")
        pressed = True
        app.move_to(x=_BUY_SCROLL_END[0], y=_BUY_SCROLL_END[1], duration=0.5)
        time.sleep(0.5)
    finally:
        if pressed:
            controller.mouse_up("left")
    time.sleep(0.2)


@action_info(
    name="resonance_pc.open_city_panel_from_main",
    public=True,
    read_only=False,
    description="Open the city panel from the city main screen by clicking 访问城市/访问地区.",
)
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr")
def resonance_pc_open_city_panel_from_main(
    timeout_sec: float = 12.0,
    settle_sec: float = 3.0,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        raise RuntimeError("app/ocr services are required")
    click = _wait_and_click_text(
        app,
        ocr,
        ("访问城市", "访问地区"),
        _VISIT_BUTTON_REGION,
        timeout_sec=timeout_sec,
        interval_sec=0.5,
    )
    if not click.get("clicked"):
        _raise_error("open_city_panel_failed", "Unable to find 访问城市/访问地区 on city main screen.", click)
    time.sleep(max(float(settle_sec), 0.0))
    return {"success": True, "page_state": "city_panel", "click": click}


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
    description="Tap the top-left direct city-main button.",
)
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_go_city_main_direct(wait_sec: float = 2.0, app: Any = None, vision: Any = None) -> Dict[str, Any]:
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    return _click_required_nav_button(
        app,
        vision,
        template=_CITY_MAIN_BUTTON_TEMPLATE,
        region=_CITY_MAIN_BUTTON_REGION,
        error_code="nav_city_main_button_not_found",
        page_state="city_main",
        wait_sec=wait_sec,
    )


@action_info(
    name="resonance_pc.read_city_name_on_city_panel",
    public=True,
    read_only=False,
    description="Read and resolve current city name on the city panel. This action does not click.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
)
def resonance_pc_read_city_name_on_city_panel(
    location_file_path: str = "data/meta/location_pc.json",
    app: Any = None,
    ocr: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or resonance_pc_city_shop_data is None:
        raise RuntimeError("app/ocr/resonance_pc_city_shop_data services are required")
    items = _capture_text_items(app, ocr, _CITY_NAME_REGION)
    ocr_text = " ".join(str(item.get("text") or "") for item in items).strip()
    resolved = resonance_pc_city_shop_data.resolve_city(ocr_text, location_file_path=location_file_path)
    return {
        "success": True,
        "page_state": "city_panel",
        "ocr_city_text": ocr_text,
        "city_key": resolved["city_key"],
        "city_name": resolved["city_name"],
        "ocr_items": items,
    }


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
    controller="plans/aura_base/controller",
)
def resonance_pc_buy_goods_on_buy_page(
    product_list: Optional[List[str]] = None,
    books_used: int = 0,
    bargain_to_cap: bool = False,
    max_scan_rounds: int = 6,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    controller: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None or controller is None:
        raise RuntimeError("app/ocr/vision/controller services are required")

    requested_products = [str(item).strip() for item in (product_list or []) if str(item).strip()]
    _report_worker(
        "buy",
        "started",
        data={"products": requested_products, "books_used": int(books_used or 0)},
    )
    negotiation = execute_bargain_to_cap(requested_to_cap=False, app=app, vision=vision)
    book_result: Dict[str, Any] = {"ok": True, "used": 0, "skipped": True}
    if int(books_used or 0) > 0:
        book_result = resonance_pc_use_purchase_books(
            books_used=int(books_used),
            item_name="进货采买书",
            app=app,
            ocr=ocr,
            vision=vision,
        )

    pending = list(requested_products)
    selected: List[str] = []
    scan_trace: List[Dict[str, Any]] = []
    rounds = max(int(max_scan_rounds), 1)
    for round_index in range(rounds):
        items = _capture_text_items(app, ocr, _BUY_PRODUCTS_REGION)
        visible = [str(item.get("text") or "") for item in items]
        round_hits: List[str] = []
        for product in list(pending):
            product_norm = _normalize_text(product)
            hit = None
            for item in items:
                item_norm = str(item.get("norm_text") or "")
                if product_norm and (product_norm in item_norm or item_norm in product_norm):
                    hit = item
                    break
            if hit is not None:
                click = _click_hit(app, hit)
                if click.get("clicked"):
                    selected.append(product)
                    round_hits.append(product)
                    pending.remove(product)
                    time.sleep(0.15)
        scan_trace.append({"round": round_index + 1, "visible_texts": visible, "round_hits": round_hits})
        if not pending:
            break
        if round_index < rounds - 1:
            _drag_buy_list(app, controller)

    if bool(bargain_to_cap) and not selected:
        _raise_error(
            "negotiation_without_selected_goods",
            "Bargaining was requested, but no goods were selected for purchase.",
            {"requested_products": requested_products, "missing_products": pending},
        )
    try:
        if bool(bargain_to_cap):
            _report_worker("negotiation", "started", operation="bargain")
        negotiation = execute_bargain_to_cap(
            requested_to_cap=bool(bargain_to_cap),
            app=app,
            vision=vision,
        )
        if bool(bargain_to_cap):
            _report_worker("negotiation", "completed", operation="bargain", data=dict(negotiation))
    except NegotiationExecutionError as exc:
        _report_worker(
            "negotiation",
            "failed",
            operation="bargain",
            data={"code": exc.code, "message": exc.message, "detail": dict(exc.detail)},
        )
        _raise_error(exc.code, exc.message, exc.detail)

    buy_button_hit = _wait_for_text_hit(app, ocr, ("买入",), _BUY_BUTTON_REGION, timeout_sec=2.0, interval_sec=0.3)
    if buy_button_hit is None:
        _raise_error(
            "buy_button_not_found",
            "Unable to find 买入 button on buy page; fixed-coordinate fallback is disabled.",
            {"region": list(_BUY_BUTTON_REGION), "requested_products": requested_products, "selected_products": selected},
        )
    buy_button = _click_hit(app, buy_button_hit)
    buy_button["method"] = "text"
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
    if bought:
        back = {
            "skipped": True,
            "reason": "buy_success_returns_to_shop_page",
            "page_state": "shop_page",
        }
    else:
        back = resonance_pc_tap_back_once(app=app, vision=vision)
    result = {
        "success": True,
        "page_state": "shop_page",
        "requested_products": requested_products,
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
    }
    _report_worker(
        "buy",
        "completed",
        data={
            "selected_products": list(selected),
            "missing_products": list(pending),
            "bought": bought,
        },
    )
    return result


@action_info(
    name="resonance_pc.sell_goods_on_sell_page",
    public=True,
    read_only=False,
    description="Complete the sell-all flow from the 我要卖 page and return to the shop page.",
)
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", vision="plans/aura_base/vision")
def resonance_pc_sell_goods_on_sell_page(
    raise_to_cap: bool = False,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None:
        raise RuntimeError("app/ocr/vision services are required")

    _report_worker("sell", "started", data={"raise_to_cap": bool(raise_to_cap)})
    sell_all_click = _wait_and_click_text(
        app,
        ocr,
        ("全部卖出",),
        _SELL_ALL_REGION,
        timeout_sec=3.0,
        interval_sec=0.3,
    )
    sell_button_click = {"clicked": False, "reason": "sell_all_not_clicked"}
    settlement = {"closed": False, "found": False, "kind": "sell"}
    negotiation = execute_raise_to_cap(requested_to_cap=False, app=app, vision=vision)
    if sell_all_click.get("clicked"):
        time.sleep(0.5)
        try:
            if bool(raise_to_cap):
                _report_worker("negotiation", "started", operation="raise")
            negotiation = execute_raise_to_cap(
                requested_to_cap=bool(raise_to_cap),
                app=app,
                vision=vision,
            )
            if bool(raise_to_cap):
                _report_worker("negotiation", "completed", operation="raise", data=dict(negotiation))
        except NegotiationExecutionError as exc:
            _report_worker(
                "negotiation",
                "failed",
                operation="raise",
                data={"code": exc.code, "message": exc.message, "detail": dict(exc.detail)},
            )
            _raise_error(exc.code, exc.message, exc.detail)
        sell_button_click = _wait_and_click_text(
            app,
            ocr,
            ("卖出",),
            _SELL_BUTTON_REGION,
            timeout_sec=3.0,
            interval_sec=0.3,
        )
        if sell_button_click.get("clicked"):
            time.sleep(0.5)
            settlement = _close_settlement(app, vision, "sell", timeout_sec=3.0)
    elif bool(raise_to_cap):
        _raise_error(
            "negotiation_without_selected_goods",
            "Raising was requested, but the sell-all selection could not be made.",
            {"sell_all_click": sell_all_click},
        )

    sold = bool(settlement.get("closed"))
    if sold:
        back = {
            "skipped": True,
            "reason": "sell_success_returns_to_shop_page",
            "page_state": "shop_page",
        }
    else:
        back = resonance_pc_tap_back_once(app=app, vision=vision)
    result = {
        "success": True,
        "page_state": "shop_page",
        "sold_confirmed": sold,
        "sell_result": "sold" if sold else "empty_or_no_result",
        "sell_all_click": sell_all_click,
        "negotiation": negotiation,
        "sell_button_click": sell_button_click,
        "settlement": settlement,
        "back": back,
    }
    _report_worker("sell", "completed", data={"sold_confirmed": sold})
    return result


def _execute_city_trade_inside_current_city(
    *,
    current_city: str,
    buy_products: Optional[List[str]],
    books_used: int,
    sell_raise_to_cap: bool = False,
    buy_bargain_to_cap: bool = False,
    app: Any,
    ocr: Any,
    vision: Any,
    controller: Any,
    city_shop_data: ResonancePcCityShopDataService,
    progress_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    progress_token = _WORKER_PROGRESS_CONTEXT.set(dict(progress_context or {}))
    try:
        return _execute_city_trade_inside_current_city_scoped(
            current_city=current_city,
            buy_products=buy_products,
            books_used=books_used,
            sell_raise_to_cap=sell_raise_to_cap,
            buy_bargain_to_cap=buy_bargain_to_cap,
            app=app,
            ocr=ocr,
            vision=vision,
            controller=controller,
            city_shop_data=city_shop_data,
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
    app: Any,
    ocr: Any,
    vision: Any,
    controller: Any,
    city_shop_data: ResonancePcCityShopDataService,
) -> Dict[str, Any]:
    products = [str(item).strip() for item in (buy_products or []) if str(item).strip()]
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
    sell_node = resonance_pc_click_shop_menu_node(node_index=2, app=app)
    sell = resonance_pc_sell_goods_on_sell_page(
        raise_to_cap=bool(sell_raise_to_cap),
        app=app,
        ocr=ocr,
        vision=vision,
    )
    buy = None
    if products:
        buy_node = resonance_pc_click_shop_menu_node(node_index=1, app=app)
        buy = resonance_pc_buy_goods_on_buy_page(
            product_list=products,
            books_used=int(books_used or 0),
            bargain_to_cap=bool(buy_bargain_to_cap),
            app=app,
            ocr=ocr,
            vision=vision,
            controller=controller,
        )
    else:
        buy_node = None
    main = resonance_pc_go_city_main_direct(app=app, vision=vision)
    return {
        "success": True,
        "page_state": "city_main",
        "current_city": current_city,
        "sell_raise_to_cap": bool(sell_raise_to_cap),
        "buy_bargain_to_cap": bool(buy_bargain_to_cap),
        "enter_shop": enter_shop,
        "sell_node": sell_node,
        "sell": sell,
        "buy_node": buy_node,
        "buy": buy,
        "go_city_main": main,
    }


async def _execute_route(
    *,
    route: List[Dict[str, Any]],
    start_page_state: str,
    use_fatigue_medicine: bool,
    allowed_fatigue_medicines: Optional[List[str]],
    fatigue_medicine_max_uses: int,
    app: Any,
    ocr: Any,
    vision: Any,
    controller: Any,
    city_shop_data: ResonancePcCityShopDataService,
    state_store: StateStoreService,
) -> Dict[str, Any]:
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    route_state = await resonance_pc_trade_route_execution_init(route=route, state_store=state_store)
    route_run_key = str(route_state.get("run_key") or "")
    page_state = start_page_state
    leg_results: List[Dict[str, Any]] = []
    try:
        for index, leg in enumerate(route):
            progress_fields = {
                "leg_index": index,
                "leg_count": len(route),
                "from_city": str(leg.get("from_city") or ""),
                "to_city": str(leg.get("to_city") or ""),
                "current_city": str(leg.get("from_city") or ""),
            }
            if reporter is not None:
                await reporter.emit("leg", "started", **progress_fields, data={"leg": dict(leg)})
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
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
                city_shop_data=city_shop_data,
                progress_fields=progress_fields,
            )
            page_state = str(leg_result.get("page_state") or "city_main")
            travel = dict(leg_result.get("travel") or {})
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
        summary["page_state"] = page_state
        summary["leg_results"] = leg_results
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
    app: Any,
    ocr: Any,
    vision: Any,
    controller: Any,
    city_shop_data: ResonancePcCityShopDataService,
    progress_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    progress_fields = dict(progress_fields or {})
    if page_state == "city_main":
        await asyncio.to_thread(resonance_pc_open_city_panel_from_main, app=app, ocr=ocr)
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
        app=app,
        ocr=ocr,
        vision=vision,
        controller=controller,
        city_shop_data=city_shop_data,
        progress_context=progress_fields,
    )
    page_state = str(city_trade.get("page_state") or "city_main")

    if reporter is not None:
        await reporter.emit("travel", "started", **progress_fields)
    travel = await asyncio.to_thread(
        resonance_pc_intercity_depart_and_wait,
        to_city_name=str(leg.get("to_city") or ""),
        enter_station_timeout_seconds=0,
        location_file_path="data/meta/location_pc.json",
        city_search_region=[130, 70, 1000, 550],
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
        controller=controller,
    )
    if reporter is not None:
        travel_status = str(travel.get("status") or "ok").lower()
        arrival_fields = dict(progress_fields)
        arrival_fields["current_city"] = str(leg.get("to_city") or "")
        await reporter.emit(
            "arrival",
            "blocked" if travel_status == "blocked" else "completed",
            **arrival_fields,
            data={"travel": dict(travel)},
        )
    return {
        "index": int(index),
        "status": "pending",
        "leg": dict(leg),
        "city_trade": city_trade,
        "travel": travel,
        "page_state": "city_main",
    }


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
    fatigue_budget: int = 100,
    cargo_capacity: int = 650,
    book_budget: int = 0,
    book_profit_threshold: float = 0,
    negotiation_budget: int = 0,
    all_plan: int = 0,
    bargain_success_rates_bps: Optional[List[Any]] = [5000],
    bargain_step_bps: Optional[Any] = 1000,
    raise_success_rates_bps: Optional[List[Any]] = [5000],
    raise_step_bps: Optional[Any] = 1000,
    trade_level: int = 20,
    available_city_ids: Optional[List[str]] = None,
    city_prestige: Optional[Dict[str, Any]] = None,
    product_unlocks: Optional[Dict[str, Any]] = None,
    active_events: Optional[List[Any]] = None,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
    event_bus: EventBus | None = None,
    context: ExecutionContext | None = None,
) -> Dict[str, Any]:
    del event_bus, context
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    normalized_all_plan = _strict_integer("all_plan", all_plan)
    if normalized_all_plan not in {0, 1}:
        raise ValueError("all_plan must be 0 or 1")
    normalized_negotiation_budget = _strict_integer("negotiation_budget", negotiation_budget)
    if normalized_negotiation_budget < 0:
        raise ValueError("negotiation_budget must be >= 0")
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
    plan = await asyncio.to_thread(
        resonance_pc_trade_plan_optimal_route,
        current_city_id=normalized_start_city_id,
        fatigue_budget=int(fatigue_budget),
        cargo_capacity=int(cargo_capacity),
        book_budget=int(book_budget),
        book_profit_threshold=book_profit_threshold,
        negotiation_budget=normalized_negotiation_budget,
        all_plan=normalized_all_plan,
        bargain_success_rates_bps=bargain_success_rates_bps,
        bargain_step_bps=bargain_step_bps,
        raise_success_rates_bps=raise_success_rates_bps,
        raise_step_bps=raise_step_bps,
        trade_level=int(trade_level),
        available_city_ids=available_city_ids,
        city_prestige=city_prestige or {"default": 20, "overrides": {}},
        product_unlocks=product_unlocks or {"mode": "all", "product_ids": []},
        active_events=active_events or [],
        snapshot_id=market.get("snapshot_id"),
        resonance_pc_trade_planner=resonance_pc_trade_planner,
    )
    route = [dict(item) for item in (plan.get("route") or []) if isinstance(item, dict)]
    if reporter is not None:
        await reporter.emit(
            "planning",
            "completed",
            leg_count=len(route),
            current_city=start_city_name,
            snapshot_id=snapshot_id,
            data={
                "route": route,
                "summary": {
                    "status": plan.get("status"),
                    "expected_profit": plan.get("expected_profit"),
                    "expected_fatigue_used": plan.get("expected_fatigue_used"),
                    "remaining_expected_fatigue": plan.get("remaining_expected_fatigue"),
                    "books_used": plan.get("books_used"),
                    "full_bargain_count": plan.get("full_bargain_count"),
                    "full_raise_count": plan.get("full_raise_count"),
                },
            },
        )
    result = dict(plan)
    result.update(
        {
            "success": True,
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
    name="resonance_pc.auto_cycle_trade_flow",
    public=True,
    read_only=False,
    description="Plan and execute one exact full-budget ResonancePc trade route from city-main UI.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
    controller="plans/aura_base/controller",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
    resonance_pc_market_data="resonance_pc_market_data",
    resonance_pc_trade_planner="resonance_pc_trade_planner",
    state_store="core/state_store",
    event_bus="core/event_bus",
)
@_with_trade_progress
async def resonance_pc_auto_cycle_trade_flow(
    fatigue_budget: int = 100,
    cargo_capacity: int = 650,
    book_budget: int = 0,
    book_profit_threshold: float = 0,
    negotiation_budget: int = 0,
    all_plan: int = 0,
    bargain_success_rates_bps: Optional[List[Any]] = [5000],
    bargain_step_bps: Optional[Any] = 1000,
    raise_success_rates_bps: Optional[List[Any]] = [5000],
    raise_step_bps: Optional[Any] = 1000,
    trade_level: int = 20,
    available_city_ids: Optional[List[str]] = None,
    city_prestige: Optional[Dict[str, Any]] = None,
    product_unlocks: Optional[Dict[str, Any]] = None,
    active_events: Optional[List[Any]] = None,
    use_fatigue_medicine: bool = False,
    allowed_fatigue_medicines: Optional[List[str]] = None,
    fatigue_medicine_max_uses: int = 4,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    controller: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
    resonance_pc_trade_planner: ResonancePcTradePlannerService | None = None,
    state_store: StateStoreService | None = None,
    event_bus: EventBus | None = None,
    context: ExecutionContext | None = None,
) -> Dict[str, Any]:
    del event_bus, context
    reporter = _ACTIVE_PROGRESS_REPORTER.get()
    normalized_all_plan = _strict_integer("all_plan", all_plan)
    if normalized_all_plan not in {0, 1}:
        raise ValueError("all_plan must be 0 or 1")
    normalized_negotiation_budget = _strict_integer("negotiation_budget", negotiation_budget)
    if normalized_negotiation_budget < 0:
        raise ValueError("negotiation_budget must be >= 0")
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
        or controller is None
        or resonance_pc_city_shop_data is None
        or resonance_pc_market_data is None
        or resonance_pc_trade_planner is None
        or state_store is None
    ):
        raise RuntimeError("auto_cycle_trade_flow requires app/ocr/vision/controller/data/planner/state services")

    # The only market refresh for this task happens after current-city recognition
    # and before the exact full-route plan is built.
    if reporter is not None:
        await reporter.emit("target", "started")
        await reporter.emit("city", "started")
    await asyncio.to_thread(resonance_pc_open_city_panel_from_main, app=app, ocr=ocr)
    current = await asyncio.to_thread(
        resonance_pc_read_city_name_on_city_panel,
        app=app,
        ocr=ocr,
        resonance_pc_city_shop_data=resonance_pc_city_shop_data,
    )
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
    plan = await asyncio.to_thread(
        resonance_pc_trade_plan_optimal_route,
        current_city=str(current.get("city_name") or ""),
        current_city_key=str(current.get("city_key") or ""),
        fatigue_budget=int(fatigue_budget),
        cargo_capacity=int(cargo_capacity),
        book_budget=int(book_budget),
        book_profit_threshold=book_profit_threshold,
        negotiation_budget=normalized_negotiation_budget,
        all_plan=normalized_all_plan,
        bargain_success_rates_bps=bargain_success_rates_bps,
        bargain_step_bps=bargain_step_bps,
        raise_success_rates_bps=raise_success_rates_bps,
        raise_step_bps=raise_step_bps,
        trade_level=int(trade_level),
        available_city_ids=available_city_ids,
        city_prestige=city_prestige or {"default": 20, "overrides": {}},
        product_unlocks=product_unlocks or {"mode": "all", "product_ids": []},
        active_events=active_events or [],
        snapshot_id=refresh.get("snapshot_id"),
        resonance_pc_trade_planner=resonance_pc_trade_planner,
    )
    route = [dict(item) for item in (plan.get("route") or []) if isinstance(item, dict)]
    if reporter is not None:
        await reporter.emit(
            "planning",
            "completed",
            leg_count=len(route),
            current_city=str(current.get("city_name") or ""),
            snapshot_id=str(refresh.get("snapshot_id") or ""),
            data={
                "route": route,
                "summary": {
                    "status": plan.get("status"),
                    "expected_profit": plan.get("expected_profit"),
                    "expected_fatigue_used": plan.get("expected_fatigue_used"),
                    "remaining_expected_fatigue": plan.get("remaining_expected_fatigue"),
                    "books_used": plan.get("books_used"),
                    "full_bargain_count": plan.get("full_bargain_count"),
                    "full_raise_count": plan.get("full_raise_count"),
                },
            },
        )
    execution: Dict[str, Any] = {
        "status": "not_started",
        "reason": plan.get("reason"),
        "completed_leg_count": 0,
        "completed_route": [],
        "leg_results": [],
        "blocked_at": None,
        "blocked_leg": None,
        "fatigue_medicine_used": [],
        "fatigue_medicine_use_count": 0,
    }
    final_sale: Optional[Dict[str, Any]] = None

    if plan.get("status") == "ok" and route:
        execution = await _execute_route(
            route=route,
            start_page_state=page_state,
            use_fatigue_medicine=bool(use_fatigue_medicine),
            allowed_fatigue_medicines=allowed_fatigue_medicines or [],
            fatigue_medicine_max_uses=int(fatigue_medicine_max_uses),
            app=app,
            ocr=ocr,
            vision=vision,
            controller=controller,
            city_shop_data=resonance_pc_city_shop_data,
            state_store=state_store,
        )
        page_state = str(execution.get("page_state") or "city_main")

        if str(execution.get("status") or "").lower() != "blocked":
            if page_state == "city_main":
                await asyncio.to_thread(resonance_pc_open_city_panel_from_main, app=app, ocr=ocr)
                page_state = "city_panel"
            endpoint_city = str(route[-1].get("to_city") or "")
            if reporter is not None:
                await reporter.emit(
                    "final_sale",
                    "started",
                    leg_count=len(route),
                    current_city=endpoint_city,
                    data={"raise_to_cap": bool(route[-1].get("raise_to_cap"))},
                )
            final_sale = await asyncio.to_thread(
                _execute_city_trade_inside_current_city,
                current_city=endpoint_city,
                buy_products=[],
                books_used=0,
                sell_raise_to_cap=bool(route[-1].get("raise_to_cap")),
                buy_bargain_to_cap=False,
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
                city_shop_data=resonance_pc_city_shop_data,
                progress_context={
                    "leg_index": len(route),
                    "leg_count": len(route),
                    "current_city": endpoint_city,
                    "from_city": endpoint_city,
                    "to_city": endpoint_city,
                },
            )
            page_state = str(final_sale.get("page_state") or "city_main")
            if reporter is not None:
                await reporter.emit(
                    "final_sale",
                    "completed",
                    leg_count=len(route),
                    current_city=endpoint_city,
                    data={"final_sale": final_sale},
                )
    elif page_state == "city_panel":
        cleanup = await asyncio.to_thread(
            resonance_pc_go_city_main_direct,
            app=app,
            vision=vision,
        )
        execution["page_cleanup"] = cleanup
        page_state = str(cleanup.get("page_state") or "city_main")

    execution_status = str(execution.get("status") or "not_started").lower()
    if execution_status == "blocked":
        status = "blocked"
        reason = execution.get("reason") or "travel_blocked"
        success = False
    elif plan.get("status") == "ok" and route:
        status = "completed"
        reason = None
        success = True
    else:
        status = str(plan.get("status") or "no_plan")
        reason = plan.get("reason")
        success = True

    result = dict(plan)
    result.update(
        {
            "success": success,
            "status": status,
            "reason": reason,
            "execution": execution,
            "final_sale": final_sale,
            "blocked_at": execution.get("blocked_at"),
            "blocked_leg": execution.get("blocked_leg"),
            "fatigue_medicine_used": list(execution.get("fatigue_medicine_used") or []),
            "fatigue_medicine_use_count": int(execution.get("fatigue_medicine_use_count") or 0),
            "initial_city": {
                "city_name": current.get("city_name"),
                "city_key": current.get("city_key"),
                "ocr_city_text": current.get("ocr_city_text"),
            },
            "page_state": page_state,
        }
    )
    if reporter is not None:
        await reporter.emit(
            "route",
            "blocked" if status == "blocked" else "completed",
            leg_count=len(route),
            current_city=str(route[-1].get("to_city") or "") if route else str(current.get("city_name") or ""),
            data={"status": status, "reason": reason},
        )
    return result
