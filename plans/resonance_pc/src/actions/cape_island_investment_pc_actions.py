"""Cape City Mirage Island investment flow for the Windows client."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from ....aura_base.src.actions.vision_actions import find_best_template_in_set
from ....aura_base.src.actions.wait_actions import (
    wait_for_image,
    wait_for_templates_in_set_to_disappear,
)

from ..services.city_shop_data_pc_service import ResonancePcCityShopDataService


class CapeIslandInvestmentError(RuntimeError):
    """Structured failure raised by the standalone island investment flow."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_CAPE_CITY_NAME = "海角城"
_MIRAGE_ISLAND_NAME = "蜃息岛"

# Page and command templates are populated independently from card grade samples.
_ISLAND_HOME_TEMPLATE = "templates/cape_island_home_anchor.png"
_REVENUE_OVERVIEW_TEMPLATE = "templates/cape_island_revenue_overview_anchor.png"
_INVESTMENT_TAB_TEMPLATE = "templates/cape_island_investment_tab.png"
_INVESTMENT_PAGE_TEMPLATE = "templates/cape_island_investment_page_anchor.png"
_INVEST_BUTTON_TEMPLATE = "templates/cape_island_invest_button.png"
_INVESTMENT_SUCCESS_TEMPLATE = "templates/cape_island_investment_success.png"

CARD_OPTION_TEMPLATES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "share": {
        "bronze": ("templates/cape_island_card_share_bronze.png",),
        "silver": ("templates/cape_island_card_share_silver.png",),
        "gold": ("templates/cape_island_card_share_gold.png",),
    },
    "ticket": {
        "bronze": ("templates/cape_island_card_ticket_bronze.png",),
        "silver": ("templates/cape_island_card_ticket_silver.png",),
        "gold": (),
    },
    "tax": {
        "bronze": ("templates/cape_island_card_tax_bronze.png",),
        "silver": ("templates/cape_island_card_tax_silver.png",),
        "gold": (),
    },
    "all": {
        "bronze": ("templates/cape_island_card_all_basic.png",),
        "silver": (),
        "gold": ("templates/cape_island_card_all_advanced.png",),
    },
}
_CARD_TEMPLATE_METADATA = {
    Path(template).name: (category, grade)
    for category, grades in CARD_OPTION_TEMPLATES.items()
    for grade, templates in grades.items()
    for template in templates
}

_ISLAND_HOME_REGION = (0, 60, 1280, 660)
_REVENUE_OVERVIEW_REGION = (600, 70, 680, 650)
_INVESTMENT_TAB_REGION = (920, 70, 350, 90)
_INVESTMENT_PAGE_REGION = (590, 130, 690, 570)
_INVESTMENT_SUCCESS_REGION = (500, 180, 300, 350)

# WGC client coordinates. The first point is deliberately above the dynamic
# 今日收益 value; the second one dismisses the modal without touching a card.
_OPEN_REVENUE_SAFE_POINT = (220, 580)
_SUCCESS_DISMISS_POINT = (470, 610)
_ISLAND_HOME_SETTLE_SEC = 2.0
_REVENUE_CLICK_RETRY_INTERVAL_SEC = 1.5

_METRIC_SPECS: Dict[str, Dict[str, Any]] = {
    "share_percent": {"region": (860, 180, 160, 90), "kind": "share"},
    "tax_reduction_percent": {"region": (995, 180, 160, 90), "kind": "tax"},
    "ticket_price": {"region": (1125, 180, 155, 90), "kind": "ticket"},
}

_CARD_ICON_REGIONS: Tuple[Tuple[int, int, int, int], ...] = (
    (625, 275, 215, 245),
    (845, 275, 215, 245),
    (1065, 275, 215, 245),
)
_CARD_BUTTON_REGIONS: Tuple[Tuple[int, int, int, int], ...] = (
    (625, 545, 215, 150),
    (845, 545, 215, 150),
    (1065, 545, 215, 150),
)

_GRADE_PRIORITY = {"bronze": 1, "silver": 2, "gold": 3}
_CATEGORY_PRIORITY = {"tax": 1, "ticket": 2, "share": 3, "all": 4}
_GRADE_LABELS = {"bronze": "铜", "silver": "银", "gold": "金"}
_CATEGORY_LABELS = {"tax": "税率", "ticket": "票价", "share": "分成", "all": "全提升"}

_CARD_MATCH_METHOD = cv2.TM_SQDIFF_NORMED
_CARD_MATCH_THRESHOLD = 0.84
_CARD_TEMPLATE_SET = "templates/cape_island_card_*.png"
_CARD_TEMPLATE_MASK = "templates/cape_island_medal_circle_mask.png"


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    logger.error(
        "Cape island investment failed code=%s message=%s detail=%s",
        code,
        message,
        detail or {},
    )
    raise CapeIslandInvestmentError(code=code, message=message, detail=detail)


def _coerce_region(region: Sequence[int]) -> Tuple[int, int, int, int]:
    if len(region) != 4:
        _raise_error("invalid_region", "region must contain x, y, width and height", {"region": list(region)})
    return tuple(int(value) for value in region)  # type: ignore[return-value]


def _match_payload(
    match: Any,
    *,
    template: str,
    region: Sequence[int],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "found": bool(match.found),
        "template": str(template),
        "region": list(_coerce_region(region)),
        "confidence": float(match.confidence or 0.0),
    }
    if match.top_left is not None:
        payload["top_left"] = [int(match.top_left[0]), int(match.top_left[1])]
    if match.center_point is not None:
        payload["center"] = [int(match.center_point[0]), int(match.center_point[1])]
    if match.rect is not None:
        payload["rect"] = [int(value) for value in match.rect]
    return payload


async def _click_template_required(
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
    template: str,
    region: Sequence[int],
    *,
    timeout_sec: float,
    interval_sec: float,
    error_code: str,
) -> Dict[str, Any]:
    match = await wait_for_image(
        app=app,
        vision=vision,
        engine=engine,
        template=template,
        timeout=timeout_sec,
        interval=interval_sec,
        region=_coerce_region(region),
        threshold=0.86,
    )
    payload = _match_payload(match, template=template, region=region)
    center = match.center_point
    if not match.found or center is None:
        _raise_error(error_code, "required island investment control was not found", {"match": payload})
    app.click(x=int(center[0]), y=int(center[1]))
    return {"clicked": True, "x": int(center[0]), "y": int(center[1]), "match": payload}


def _capture_ocr_texts(app: Any, ocr: Any, region: Sequence[int]) -> List[Dict[str, Any]]:
    rect = _coerce_region(region)
    capture = app.capture(rect=rect)
    if not bool(getattr(capture, "success", False)):
        _raise_error("capture_failed", "failed to capture investment metric", {"region": list(rect)})
    result = ocr.recognize_all(source_image=capture.image)
    rows: List[Dict[str, Any]] = []
    for item in getattr(result, "results", []) or []:
        text = str(getattr(item, "text", "") or "").strip()
        if text:
            rows.append(
                {
                    "text": text,
                    "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
                }
            )
    rows.sort(key=lambda item: float(item["confidence"]), reverse=True)
    return rows


def _normalize_numeric_text(text: str) -> str:
    return (
        str(text or "")
        .replace("，", ",")
        .replace("％", "%")
        .replace("−", "-")
        .replace("—", "-")
        .replace("–", "-")
        .replace(" ", "")
    )


def _parse_metric_value(kind: str, texts: Sequence[str]) -> Optional[float | int]:
    for raw in texts:
        normalized = _normalize_numeric_text(raw)
        if kind == "ticket":
            match = re.search(r"\d[\d,]*", normalized)
            if match:
                value = int(match.group(0).replace(",", ""))
                if 0 <= value <= 100000:
                    return value
            continue
        match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
        if not match:
            continue
        value = float(match.group(0))
        if kind == "share" and 0.0 <= value <= 100.0:
            return value
        if kind == "tax" and -100.0 <= value <= 0.0:
            return value
    return None


def _read_investment_metrics(
    app: Any,
    ocr: Any,
    *,
    timeout_sec: float,
    interval_sec: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    histories: Dict[str, List[Dict[str, Any]]] = {name: [] for name in _METRIC_SPECS}
    stable: Dict[str, float | int] = {}
    while True:
        for name, spec in _METRIC_SPECS.items():
            if name in stable:
                continue
            rows = _capture_ocr_texts(app, ocr, spec["region"])
            texts = [str(row["text"]) for row in rows]
            value = _parse_metric_value(str(spec["kind"]), texts)
            histories[name].append({"texts": texts, "value": value})
            logger.debug(
                "Cape island metric OCR metric=%s texts=%s parsed=%s",
                name,
                rows,
                value,
            )
            valid_values = [item["value"] for item in histories[name] if item["value"] is not None]
            if len(valid_values) >= 2 and valid_values[-1] == valid_values[-2]:
                stable[name] = valid_values[-1]
        if len(stable) == len(_METRIC_SPECS):
            return {"values": stable, "ocr_history": histories}
        if time.monotonic() >= deadline:
            _raise_error(
                "investment_metric_unreadable",
                "unable to read all island investment metrics consistently",
                {"values": stable, "ocr_history": histories},
            )
        time.sleep(max(float(interval_sec), 0.05))


def _metric_caps(metrics: Dict[str, Any]) -> Dict[str, bool]:
    return {
        "share": float(metrics["share_percent"]) >= 32.0,
        "ticket": int(metrics["ticket_price"]) >= 1500,
        "tax": float(metrics["tax_reduction_percent"]) <= -5.0,
    }


async def _recognize_card_option(
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
    slot: int,
    region: Sequence[int],
) -> Dict[str, Any]:
    result = await asyncio.to_thread(
        find_best_template_in_set,
        app=app,
        vision=vision,
        engine=engine,
        templates_ref=_CARD_TEMPLATE_SET,
        region=_coerce_region(region),
        threshold=_CARD_MATCH_THRESHOLD,
        use_grayscale=False,
        match_method=_CARD_MATCH_METHOD,
        mask=_CARD_TEMPLATE_MASK,
    )
    template = result.get("template")
    match = result.get("match")
    metadata = _CARD_TEMPLATE_METADATA.get(Path(str(template or "")).name)
    if not template or match is None or not match.found or metadata is None:
        return {
            "slot": int(slot),
            "category": None,
            "grade": None,
            "confidence": 0.0,
            "match": None,
        }
    category, grade = metadata
    match_payload = _match_payload(match, template=str(template), region=region)
    return {
        "slot": int(slot),
        "category": str(category),
        "grade": str(grade),
        "confidence": float(match.confidence or 0.0),
        "match": match_payload,
    }


def _option_is_eligible(option: Dict[str, Any], capped: Dict[str, bool]) -> bool:
    category = str(option.get("category") or "")
    grade = str(option.get("grade") or "")
    if category == "all":
        allowed = not any(bool(value) for value in capped.values())
    else:
        allowed = category in capped and not bool(capped[category])
    return bool(allowed and grade in _GRADE_PRIORITY)


def _select_investment_option(
    options: Sequence[Dict[str, Any]],
    capped: Dict[str, bool],
) -> Optional[Dict[str, Any]]:
    eligible: List[Dict[str, Any]] = []
    for option in options:
        if _option_is_eligible(option, capped):
            eligible.append(dict(option))
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (
            _GRADE_PRIORITY[str(item["grade"])],
            _CATEGORY_PRIORITY[str(item["category"])],
        ),
    )


async def _enter_island(
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
    city_shop_data: ResonancePcCityShopDataService,
    *,
    location_file_path: str,
    page_timeout_sec: float,
    interval_sec: float,
    transition_attempts: int,
) -> Dict[str, Any]:
    point = city_shop_data.resolve_shop_point(
        city_name=_CAPE_CITY_NAME,
        shop_name=_MIRAGE_ISLAND_NAME,
        location_file_path=location_file_path,
    )
    last_match: Dict[str, Any] = {}
    for attempt in range(1, max(int(transition_attempts), 1) + 1):
        app.click(x=int(point["x"]), y=int(point["y"]))
        match = await wait_for_image(
            app=app,
            vision=vision,
            engine=engine,
            template=_ISLAND_HOME_TEMPLATE,
            timeout=page_timeout_sec,
            interval=interval_sec,
            region=_ISLAND_HOME_REGION,
            threshold=0.86,
        )
        last_match = _match_payload(
            match,
            template=_ISLAND_HOME_TEMPLATE,
            region=_ISLAND_HOME_REGION,
        )
        if match.found:
            await asyncio.sleep(_ISLAND_HOME_SETTLE_SEC)
            return {
                "attempts": attempt,
                "click": point,
                "match": last_match,
                "settle_sec": _ISLAND_HOME_SETTLE_SEC,
            }
    _raise_error(
        "cape_island_entry_timeout",
        "clicked the Cape City island coordinate but the island page was not confirmed",
        {"click": point, "attempts": max(int(transition_attempts), 1), "last_match": last_match},
    )
    return {}


async def _open_revenue_overview(
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
    *,
    page_timeout_sec: float,
    interval_sec: float,
    transition_attempts: int,
) -> Dict[str, Any]:
    last_match: Dict[str, Any] = {}
    attempt_limit = max(int(transition_attempts), 1)
    deadline = time.monotonic() + max(float(page_timeout_sec), 0.0)
    attempts_made = 0
    for attempt in range(1, attempt_limit + 1):
        if attempt > 1 and time.monotonic() >= deadline:
            break
        attempts_made = attempt
        app.click(x=_OPEN_REVENUE_SAFE_POINT[0], y=_OPEN_REVENUE_SAFE_POINT[1])
        remaining_sec = max(deadline - time.monotonic(), 0.0)
        wait_timeout_sec = (
            remaining_sec
            if attempt == attempt_limit
            else min(_REVENUE_CLICK_RETRY_INTERVAL_SEC, remaining_sec)
        )
        match = await wait_for_image(
            app=app,
            vision=vision,
            engine=engine,
            template=_REVENUE_OVERVIEW_TEMPLATE,
            timeout=wait_timeout_sec,
            interval=interval_sec,
            region=_REVENUE_OVERVIEW_REGION,
            threshold=0.86,
        )
        last_match = _match_payload(
            match,
            template=_REVENUE_OVERVIEW_TEMPLATE,
            region=_REVENUE_OVERVIEW_REGION,
        )
        if match.found:
            return {
                "attempts": attempt,
                "click": {"x": _OPEN_REVENUE_SAFE_POINT[0], "y": _OPEN_REVENUE_SAFE_POINT[1]},
                "match": last_match,
            }
    _raise_error(
        "revenue_overview_timeout",
        "the fixed island background click did not open the revenue overview",
        {
            "attempts": attempts_made,
            "timeout_sec": max(float(page_timeout_sec), 0.0),
            "last_match": last_match,
        },
    )
    return {}


@action_info(
    name="resonance_pc.execute_cape_island_investment_from_city_panel",
    public=True,
    read_only=False,
    description="Enter Mirage Island from the Cape City panel and perform the best available investment.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
)
async def resonance_pc_execute_cape_island_investment_from_city_panel(
    location_file_path: str = "data/meta/location_pc.json",
    page_timeout_sec: float = 12.0,
    metric_timeout_sec: float = 8.0,
    interval_sec: float = 0.3,
    transition_attempts: int = 3,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
    engine: ExecutionEngine | None = None,
) -> Dict[str, Any]:
    if (
        app is None
        or ocr is None
        or vision is None
        or resonance_pc_city_shop_data is None
        or engine is None
    ):
        raise RuntimeError("app/ocr/vision/resonance_pc_city_shop_data/engine are required")
    started_at = time.monotonic()
    island_entry = await _enter_island(
        app,
        vision,
        engine,
        resonance_pc_city_shop_data,
        location_file_path=location_file_path,
        page_timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        transition_attempts=transition_attempts,
    )
    revenue_overview = await _open_revenue_overview(
        app,
        vision,
        engine,
        page_timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        transition_attempts=transition_attempts,
    )
    investment_tab = await _click_template_required(
        app,
        vision,
        engine,
        _INVESTMENT_TAB_TEMPLATE,
        _INVESTMENT_TAB_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        error_code="investment_tab_not_found",
    )
    investment_page_match = await wait_for_image(
        app=app,
        vision=vision,
        engine=engine,
        template=_INVESTMENT_PAGE_TEMPLATE,
        timeout=page_timeout_sec,
        interval=interval_sec,
        region=_INVESTMENT_PAGE_REGION,
        threshold=0.86,
    )
    investment_page = _match_payload(
        investment_page_match,
        template=_INVESTMENT_PAGE_TEMPLATE,
        region=_INVESTMENT_PAGE_REGION,
    )
    if not investment_page_match.found:
        _raise_error(
            "investment_page_not_confirmed",
            "the island investment page did not appear after clicking its tab",
            {"investment_tab": investment_tab, "last_match": investment_page},
        )
    metric_result = await asyncio.to_thread(
        _read_investment_metrics,
        app,
        ocr,
        timeout_sec=metric_timeout_sec,
        interval_sec=interval_sec,
    )
    metrics = dict(metric_result["values"])
    capped = _metric_caps(metrics)
    logger.info("Cape island investment metrics values=%s capped=%s", metrics, capped)
    logger.debug("Cape island investment OCR history=%s", metric_result["ocr_history"])
    base_result: Dict[str, Any] = {
        "success": True,
        "metrics": metrics,
        "capped": capped,
        "island_entry": island_entry,
        "revenue_overview": revenue_overview,
        "investment_tab": investment_tab,
        "investment_page": investment_page,
        "ocr_history": metric_result["ocr_history"],
        "degraded": False,
        "unclassified_slots": [],
        "page_state": "island_investment",
    }
    if all(capped.values()):
        logger.warning(
            "Cape island investment skipped reason=all_metrics_capped metrics=%s elapsed_ms=%s",
            metrics,
            int((time.monotonic() - started_at) * 1000),
        )
        base_result.update(
            {
                "status": "skipped",
                "reason": "all_metrics_capped",
                "selected_option": None,
                "options": [],
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }
        )
        return base_result

    options = []
    for index, region in enumerate(_CARD_ICON_REGIONS):
        options.append(
            await _recognize_card_option(
                app,
                vision,
                engine,
                slot=index + 1,
                region=region,
            )
        )
    for option in options:
        option["eligible"] = _option_is_eligible(option, capped)
        logger.info(
            "Cape island investment card slot=%s category=%s category_label=%s "
            "grade=%s grade_label=%s confidence=%.4f eligible=%s",
            option.get("slot"),
            option.get("category"),
            _CATEGORY_LABELS.get(str(option.get("category") or ""), "未识别"),
            option.get("grade"),
            _GRADE_LABELS.get(str(option.get("grade") or ""), "未识别"),
            float(option.get("confidence") or 0.0),
            option["eligible"],
        )
    unclassified = [option for option in options if not option.get("category") or not option.get("grade")]
    if unclassified:
        logger.warning(
            "Cape island investment using degraded card selection unclassified_slots=%s",
            [item["slot"] for item in unclassified],
        )
    base_result["degraded"] = bool(unclassified)
    base_result["unclassified_slots"] = [int(item["slot"]) for item in unclassified]
    base_result["options"] = options
    selected = _select_investment_option(options, capped)
    if selected is None:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.warning(
            "Cape island investment skipped reason=no_recognized_eligible_option "
            "unclassified_slots=%s elapsed_ms=%s",
            base_result["unclassified_slots"],
            elapsed_ms,
        )
        base_result.update(
            {
                "status": "skipped",
                "reason": "no_recognized_eligible_option",
                "selected_option": None,
                "elapsed_ms": elapsed_ms,
            }
        )
        return base_result
    logger.info(
        "Cape island investment selected slot=%s category=%s effect=%s grade=%s grade_label=%s "
        "confidence=%.4f",
        selected.get("slot"),
        selected.get("category"),
        _CATEGORY_LABELS.get(str(selected.get("category") or ""), "未识别"),
        selected.get("grade"),
        _GRADE_LABELS.get(str(selected.get("grade") or ""), "未识别"),
        float(selected.get("confidence") or 0.0),
    )
    slot_index = int(selected["slot"]) - 1
    invest_click = await _click_template_required(
        app,
        vision,
        engine,
        _INVEST_BUTTON_TEMPLATE,
        _CARD_BUTTON_REGIONS[slot_index],
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        error_code="investment_button_not_found",
    )
    success_result = await wait_for_image(
        app=app,
        vision=vision,
        engine=engine,
        template=_INVESTMENT_SUCCESS_TEMPLATE,
        timeout=page_timeout_sec,
        interval=interval_sec,
        region=_INVESTMENT_SUCCESS_REGION,
        threshold=0.86,
    )
    success_match = _match_payload(
        success_result,
        template=_INVESTMENT_SUCCESS_TEMPLATE,
        region=_INVESTMENT_SUCCESS_REGION,
    )
    if not success_result.found:
        _raise_error(
            "investment_success_not_confirmed",
            "the investment success overlay did not appear",
            {"selected_option": selected, "invest_click": invest_click, "last_match": success_match},
        )
    app.click(x=_SUCCESS_DISMISS_POINT[0], y=_SUCCESS_DISMISS_POINT[1])
    disappeared = await wait_for_templates_in_set_to_disappear(
        app=app,
        vision=vision,
        engine=engine,
        templates_ref=_INVESTMENT_SUCCESS_TEMPLATE,
        timeout=page_timeout_sec,
        interval=interval_sec,
        region=_INVESTMENT_SUCCESS_REGION,
        threshold=0.86,
    )
    dismissed = {
        "found": not bool(disappeared),
        "template": _INVESTMENT_SUCCESS_TEMPLATE,
        "region": list(_INVESTMENT_SUCCESS_REGION),
    }
    returned_result = await wait_for_image(
        app=app,
        vision=vision,
        engine=engine,
        template=_INVESTMENT_PAGE_TEMPLATE,
        timeout=page_timeout_sec,
        interval=interval_sec,
        region=_INVESTMENT_PAGE_REGION,
        threshold=0.86,
    )
    returned_page = _match_payload(
        returned_result,
        template=_INVESTMENT_PAGE_TEMPLATE,
        region=_INVESTMENT_PAGE_REGION,
    )
    if not disappeared or not returned_result.found:
        _raise_error(
            "investment_result_dismiss_failed",
            "the investment result was not dismissed back to the investment page",
            {"success_match": success_match, "dismissed": dismissed, "investment_page": returned_page},
        )
    base_result.update(
        {
            "status": "invested",
            "reason": None,
            "selected_option": selected,
            "options": options,
            "invest_click": invest_click,
            "success_match": success_match,
            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        }
    )
    logger.info(
        "Cape island investment result status=invested effect=%s slot=%s grade=%s "
        "confidence=%.4f elapsed_ms=%s",
        _CATEGORY_LABELS.get(str(selected.get("category") or ""), selected.get("category")),
        selected.get("slot"),
        selected.get("grade"),
        float(selected.get("confidence") or 0.0),
        base_result["elapsed_ms"],
    )
    return base_result
