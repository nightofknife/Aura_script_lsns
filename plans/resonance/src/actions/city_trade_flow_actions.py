"""City-panel trade UI actions and auto-cycle trade flow for Resonance."""

from __future__ import annotations

import time
import asyncio
from typing import Any, Dict, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.observability.logging.core_logger import logger

from ..services.city_shop_data_service import CityShopDataService
from ..services.resonance_market_data_service import ResonanceMarketDataService
from ..services.resonance_trade_planner_service import ResonanceTradePlannerService
from .city_travel_actions import resonance_intercity_depart_and_wait
from .market_data_actions import resonance_market_refresh
from .purchase_book_actions import resonance_use_purchase_books
from .trade_planner_actions import (
    resonance_trade_loop_cleanup,
    resonance_trade_loop_init,
    resonance_trade_loop_summary,
    resonance_trade_loop_update,
    resonance_trade_plan_next_cycle_execution,
    resonance_trade_route_execution_cleanup,
    resonance_trade_route_execution_init,
    resonance_trade_route_execution_summary,
    resonance_trade_route_execution_update,
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
    name="resonance.open_city_panel_from_main",
    public=True,
    read_only=False,
    description="Open the city panel from the city main screen by clicking 访问城市/访问地区.",
)
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr")
def resonance_open_city_panel_from_main(
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


@action_info(name="resonance.tap_back_once", public=True, read_only=False, description="Tap the top-left back button once.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_tap_back_once(wait_sec: float = 1.0, app: Any = None, vision: Any = None) -> Dict[str, Any]:
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
    name="resonance.go_city_main_direct",
    public=True,
    read_only=False,
    description="Tap the top-left direct city-main button.",
)
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_go_city_main_direct(wait_sec: float = 2.0, app: Any = None, vision: Any = None) -> Dict[str, Any]:
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
    name="resonance.read_city_name_on_city_panel",
    public=True,
    read_only=False,
    description="Read and resolve current city name on the city panel. This action does not click.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    resonance_city_shop_data="resonance_city_shop_data",
)
def resonance_read_city_name_on_city_panel(
    location_file_path: str = "data/meta/location_mumu.json",
    app: Any = None,
    ocr: Any = None,
    resonance_city_shop_data: CityShopDataService | None = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or resonance_city_shop_data is None:
        raise RuntimeError("app/ocr/resonance_city_shop_data services are required")
    items = _capture_text_items(app, ocr, _CITY_NAME_REGION)
    ocr_text = " ".join(str(item.get("text") or "") for item in items).strip()
    resolved = resonance_city_shop_data.resolve_city(ocr_text, location_file_path=location_file_path)
    return {
        "success": True,
        "page_state": "city_panel",
        "ocr_city_text": ocr_text,
        "city_key": resolved["city_key"],
        "city_name": resolved["city_name"],
        "ocr_items": items,
    }


@action_info(
    name="resonance.click_city_shop_by_name",
    public=True,
    read_only=False,
    description="Click a city shop by resolved city name and shop name from the city panel.",
)
@requires_services(app="plans/aura_base/app", resonance_city_shop_data="resonance_city_shop_data")
def resonance_click_city_shop_by_name(
    city_name: str,
    shop_name: str,
    location_file_path: str = "data/meta/location_mumu.json",
    wait_sec: float = 1.0,
    app: Any = None,
    resonance_city_shop_data: CityShopDataService | None = None,
) -> Dict[str, Any]:
    if app is None or resonance_city_shop_data is None:
        raise RuntimeError("app/resonance_city_shop_data services are required")
    point = resonance_city_shop_data.resolve_shop_point(
        city_name=city_name,
        shop_name=shop_name,
        location_file_path=location_file_path,
    )
    app.click(x=int(point["x"]), y=int(point["y"]))
    time.sleep(max(float(wait_sec), 0.0))
    return {"success": True, "page_state": "shop_page", "click": point}


@action_info(
    name="resonance.click_shop_menu_node",
    public=True,
    read_only=False,
    description="Click the Nth node in a shop page using fixed MuMu coordinates.",
)
@requires_services(app="plans/aura_base/app")
def resonance_click_shop_menu_node(node_index: int, wait_sec: float = 1.0, app: Any = None) -> Dict[str, Any]:
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
    name="resonance.buy_goods_on_buy_page",
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
def resonance_buy_goods_on_buy_page(
    product_list: Optional[List[str]] = None,
    books_used: int = 0,
    max_scan_rounds: int = 6,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    controller: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None or controller is None:
        raise RuntimeError("app/ocr/vision/controller services are required")

    requested_products = [str(item).strip() for item in (product_list or []) if str(item).strip()]
    book_result: Dict[str, Any] = {"ok": True, "used": 0, "skipped": True}
    if int(books_used or 0) > 0:
        book_result = resonance_use_purchase_books(
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
        back = resonance_tap_back_once(app=app, vision=vision)
    return {
        "success": True,
        "page_state": "shop_page",
        "requested_products": requested_products,
        "selected_products": selected,
        "missing_products": pending,
        "books_requested": int(books_used or 0),
        "book_result": book_result,
        "buy_button": buy_button,
        "settlement": settlement,
        "confirm_panel_found": confirm_panel is not None,
        "confirm_click": confirm_click,
        "settlement_after_confirm": settlement_after_confirm,
        "back": back,
        "scan_trace": scan_trace,
    }


@action_info(
    name="resonance.sell_goods_on_sell_page",
    public=True,
    read_only=False,
    description="Complete the sell-all flow from the 我要卖 page and return to the shop page.",
)
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", vision="plans/aura_base/vision")
def resonance_sell_goods_on_sell_page(
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None:
        raise RuntimeError("app/ocr/vision services are required")

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
    if sell_all_click.get("clicked"):
        time.sleep(0.5)
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

    sold = bool(settlement.get("closed"))
    if sold:
        back = {
            "skipped": True,
            "reason": "sell_success_returns_to_shop_page",
            "page_state": "shop_page",
        }
    else:
        back = resonance_tap_back_once(app=app, vision=vision)
    return {
        "success": True,
        "page_state": "shop_page",
        "sold_confirmed": sold,
        "sell_result": "sold" if sold else "empty_or_no_result",
        "sell_all_click": sell_all_click,
        "sell_button_click": sell_button_click,
        "settlement": settlement,
        "back": back,
    }


def _execute_city_trade_inside_current_city(
    *,
    current_city: str,
    buy_products: Optional[List[str]],
    books_used: int,
    app: Any,
    ocr: Any,
    vision: Any,
    controller: Any,
    city_shop_data: CityShopDataService,
) -> Dict[str, Any]:
    enter_shop = resonance_click_city_shop_by_name(
        city_name=current_city,
        shop_name="交易所",
        wait_sec=_SHOP_ENTRY_SETTLE_SEC,
        app=app,
        resonance_city_shop_data=city_shop_data,
    )
    sell_node = resonance_click_shop_menu_node(node_index=2, app=app)
    sell = resonance_sell_goods_on_sell_page(app=app, ocr=ocr, vision=vision)
    buy = None
    products = [str(item).strip() for item in (buy_products or []) if str(item).strip()]
    if products:
        buy_node = resonance_click_shop_menu_node(node_index=1, app=app)
        buy = resonance_buy_goods_on_buy_page(
            product_list=products,
            books_used=int(books_used or 0),
            app=app,
            ocr=ocr,
            vision=vision,
            controller=controller,
        )
    else:
        buy_node = None
    main = resonance_go_city_main_direct(app=app, vision=vision)
    return {
        "success": True,
        "page_state": "city_main",
        "current_city": current_city,
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
    city_shop_data: CityShopDataService,
    state_store: StateStoreService,
) -> Dict[str, Any]:
    route_state = await resonance_trade_route_execution_init(route=route, state_store=state_store)
    route_run_key = str(route_state.get("run_key") or "")
    page_state = start_page_state
    try:
        for leg in route:
            if page_state == "city_main":
                await asyncio.to_thread(resonance_open_city_panel_from_main, app=app, ocr=ocr)
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
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
                city_shop_data=city_shop_data,
            )
            page_state = str(city_trade.get("page_state") or "city_main")

            travel = await asyncio.to_thread(
                resonance_intercity_depart_and_wait,
                to_city_name=str(leg.get("to_city") or ""),
                enter_station_timeout_seconds=0,
                location_file_path="data/meta/location_mumu.json",
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
            page_state = "city_main"
            update = await resonance_trade_route_execution_update(
                run_key=route_run_key,
                leg=leg,
                travel_status=str(travel.get("status") or "ok"),
                reason=travel.get("reason"),
                blocked_at=travel.get("blocked_at"),
                fatigue_medicine_used=travel.get("fatigue_medicine_used") or [],
                fatigue_medicine_use_count=int(travel.get("fatigue_medicine_use_count") or 0),
                state_store=state_store,
            )
            if str(update.get("status") or "").lower() == "blocked":
                break
            await asyncio.sleep(2.0)
        summary = await resonance_trade_route_execution_summary(route_run_key, state_store=state_store)
        summary["page_state"] = page_state
        return summary
    finally:
        if route_run_key:
            await resonance_trade_route_execution_cleanup(route_run_key, state_store=state_store)


@action_info(
    name="resonance.auto_cycle_trade_flow",
    public=True,
    read_only=False,
    description="Run the full Resonance auto-cycle trade flow from city-main UI.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
    controller="plans/aura_base/controller",
    resonance_city_shop_data="resonance_city_shop_data",
    resonance_market_data="resonance_market_data",
    resonance_trade_planner="resonance_trade_planner",
    state_store="core/state_store",
)
async def resonance_auto_cycle_trade_flow(
    fatigue_budget: int,
    cargo_capacity: int = 650,
    book_budget: int = 0,
    book_profit_threshold: float = 0,
    max_cycle_hops: int = 6,
    max_rounds: int = 64,
    use_fatigue_medicine: bool = False,
    allowed_fatigue_medicines: Optional[List[str]] = None,
    fatigue_medicine_max_uses: int = 4,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    controller: Any = None,
    resonance_city_shop_data: CityShopDataService | None = None,
    resonance_market_data: ResonanceMarketDataService | None = None,
    resonance_trade_planner: ResonanceTradePlannerService | None = None,
    state_store: StateStoreService | None = None,
) -> Dict[str, Any]:
    if (
        app is None
        or ocr is None
        or vision is None
        or controller is None
        or resonance_city_shop_data is None
        or resonance_market_data is None
        or resonance_trade_planner is None
        or state_store is None
    ):
        raise RuntimeError("auto_cycle_trade_flow requires app/ocr/vision/controller/data/planner/state services")

    await asyncio.to_thread(resonance_open_city_panel_from_main, app=app, ocr=ocr)
    current = await asyncio.to_thread(
        resonance_read_city_name_on_city_panel,
        app=app,
        ocr=ocr,
        resonance_city_shop_data=resonance_city_shop_data,
    )
    page_state = "city_panel"

    init = await resonance_trade_loop_init(
        current_city=str(current.get("city_name") or ""),
        current_city_key=str(current.get("city_key") or ""),
        fatigue_budget=int(fatigue_budget),
        book_budget=int(book_budget),
        state_store=state_store,
    )
    run_key = str(init.get("run_key") or "")
    loop_state: Dict[str, Any] = init
    rounds_run = 0
    blocked = False

    while bool(loop_state.get("should_continue")) and rounds_run < max(int(max_rounds), 0):
        refresh = await asyncio.to_thread(
            resonance_market_refresh,
            force=True,
            resonance_market_data=resonance_market_data,
        )
        plan = await asyncio.to_thread(
            resonance_trade_plan_next_cycle_execution,
            current_city=str(loop_state.get("current_city") or ""),
            current_city_key=str(loop_state.get("current_city_key") or ""),
            fatigue_budget=int(loop_state.get("remaining_fatigue") or 0),
            cargo_capacity=int(cargo_capacity),
            book_budget=int(book_budget),
            book_profit_threshold=float(book_profit_threshold),
            max_cycle_hops=int(max_cycle_hops),
            snapshot_id=refresh.get("snapshot_id"),
            resonance_trade_planner=resonance_trade_planner,
        )

        execution: Dict[str, Any] = {}
        route = [dict(item) for item in (plan.get("route") or []) if isinstance(item, dict)]
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
                city_shop_data=resonance_city_shop_data,
                state_store=state_store,
            )
            page_state = str(execution.get("page_state") or "city_main")
            if str(execution.get("status") or "").lower() == "blocked":
                blocked = True

        loop_state = await resonance_trade_loop_update(
            run_key=run_key,
            plan=plan,
            execution=execution,
            state_store=state_store,
        )
        rounds_run += 1
        if blocked:
            break
        if not route:
            break

    summary = await resonance_trade_loop_summary(run_key, state_store=state_store)

    if str(summary.get("status") or "").lower() != "blocked":
        current_city = str(summary.get("current_city") or current.get("city_name") or "")
        if page_state == "city_main":
            await asyncio.to_thread(resonance_open_city_panel_from_main, app=app, ocr=ocr)
            page_state = "city_panel"
        if page_state == "city_panel" and current_city:
            await asyncio.to_thread(
                _execute_city_trade_inside_current_city,
                current_city=current_city,
                buy_products=[],
                books_used=0,
                app=app,
                ocr=ocr,
                vision=vision,
                controller=controller,
                city_shop_data=resonance_city_shop_data,
            )
            page_state = "city_main"
        summary = await resonance_trade_loop_summary(run_key, state_store=state_store)

    await resonance_trade_loop_cleanup(run_key, state_store=state_store)
    summary["success"] = True
    summary["initial_city"] = {
        "city_name": current.get("city_name"),
        "city_key": current.get("city_key"),
        "ocr_city_text": current.get("ocr_city_text"),
    }
    summary["rounds_run"] = rounds_run
    summary["page_state"] = page_state
    return summary
