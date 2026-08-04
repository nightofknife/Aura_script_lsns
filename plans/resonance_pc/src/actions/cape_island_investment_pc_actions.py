"""Cape City Mirage Island investment flow for the Windows client."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

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


_PLAN_ROOT = Path(__file__).resolve().parents[2]
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
        "silver": (),
        "gold": (),
    },
    "ticket": {
        "bronze": ("templates/cape_island_card_ticket_bronze.png",),
        "silver": (),
        "gold": (),
    },
    "tax": {
        "bronze": ("templates/cape_island_card_tax_bronze.png",),
        "silver": ("templates/cape_island_card_tax_silver.png",),
        "gold": (),
    },
    "all": {"rainbow": ()},
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

_GRADE_PRIORITY = {"bronze": 1, "silver": 2, "gold": 3, "rainbow": 4}
_CATEGORY_PRIORITY = {"tax": 1, "ticket": 2, "share": 3, "all": 4}
_GRADE_LABELS = {"bronze": "铜", "silver": "银", "gold": "金", "rainbow": "彩"}
_CATEGORY_LABELS = {"tax": "税率", "ticket": "票价", "share": "分成", "all": "全部提升"}


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


def _template_path(template: str) -> Path:
    raw = Path(str(template or ""))
    return raw if raw.is_absolute() else _PLAN_ROOT / raw


def _match_template(
    app: Any,
    vision: Any,
    template: str,
    region: Sequence[int],
    *,
    threshold: float = 0.86,
    use_grayscale: bool = True,
    require_configured: bool = True,
) -> Dict[str, Any]:
    rect = _coerce_region(region)
    if not str(template or "").strip():
        if require_configured:
            _raise_error("template_not_configured", "required template path is empty", {"region": list(rect)})
        return {"found": False, "configured": False, "template": "", "region": list(rect), "confidence": 0.0}
    path = _template_path(template)
    if not path.is_file():
        _raise_error(
            "template_not_found",
            "required island investment template file was not found",
            {"template": str(template), "resolved_path": str(path), "region": list(rect)},
        )
    capture = app.capture(rect=rect)
    if not bool(getattr(capture, "success", False)):
        _raise_error("capture_failed", "failed to capture island investment region", {"region": list(rect)})
    result = vision.find_template(
        source_image=capture.image,
        template_image=str(path),
        threshold=float(threshold),
        use_grayscale=bool(use_grayscale),
    )
    center = getattr(result, "center_point", None)
    payload: Dict[str, Any] = {
        "found": bool(getattr(result, "found", False)),
        "configured": True,
        "template": str(template),
        "region": list(rect),
        "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
    }
    if center and len(center) == 2:
        payload["center"] = [int(rect[0] + int(center[0])), int(rect[1] + int(center[1]))]
    logger.debug(
        "Cape island template match template=%s found=%s confidence=%.4f region=%s center=%s",
        template,
        payload["found"],
        payload["confidence"],
        payload["region"],
        payload.get("center"),
    )
    return payload


def _wait_template(
    app: Any,
    vision: Any,
    template: str,
    region: Sequence[int],
    *,
    timeout_sec: float,
    interval_sec: float,
    threshold: float = 0.86,
    should_exist: bool = True,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last: Dict[str, Any] = {"found": False, "template": template, "region": list(region)}
    while True:
        last = _match_template(app, vision, template, region, threshold=threshold)
        if bool(last.get("found")) is bool(should_exist):
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(max(float(interval_sec), 0.05))


def _click_template_required(
    app: Any,
    vision: Any,
    template: str,
    region: Sequence[int],
    *,
    timeout_sec: float,
    interval_sec: float,
    error_code: str,
) -> Dict[str, Any]:
    match = _wait_template(
        app,
        vision,
        template,
        region,
        timeout_sec=timeout_sec,
        interval_sec=interval_sec,
    )
    center = match.get("center")
    if not match.get("found") or not isinstance(center, list) or len(center) != 2:
        _raise_error(error_code, "required island investment control was not found", {"match": match})
    app.click(x=int(center[0]), y=int(center[1]))
    return {"clicked": True, "x": int(center[0]), "y": int(center[1]), "match": match}


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


def _configured_card_template_count() -> int:
    return sum(
        1
        for grades in CARD_OPTION_TEMPLATES.values()
        for templates in grades.values()
        for template in templates
        if str(template).strip()
    )


def _best_option_match(
    app: Any,
    vision: Any,
    region: Sequence[int],
) -> Optional[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for category, grades in CARD_OPTION_TEMPLATES.items():
        for grade, templates in grades.items():
            for template in templates:
                match = _match_template(
                    app,
                    vision,
                    template,
                    region,
                    threshold=0.84,
                    use_grayscale=False,
                    require_configured=False,
                )
                if match.get("found"):
                    match["category"] = category
                    match["grade"] = grade
                    matches.append(match)
    if not matches:
        return None
    return max(matches, key=lambda item: float(item.get("confidence") or 0.0))


def _classify_card(
    app: Any,
    vision: Any,
    slot: int,
    region: Sequence[int],
) -> Dict[str, Any]:
    option_match = _best_option_match(app, vision, region)
    if option_match is None:
        return {
            "slot": int(slot),
            "category": None,
            "grade": None,
            "confidence": 0.0,
            "match": None,
        }
    return {
        "slot": int(slot),
        "category": str(option_match["category"]),
        "grade": str(option_match["grade"]),
        "confidence": float(option_match.get("confidence") or 0.0),
        "match": option_match,
    }


def _option_is_eligible(option: Dict[str, Any], capped: Dict[str, bool]) -> bool:
    category = str(option.get("category") or "")
    grade = str(option.get("grade") or "")
    if category == "all":
        allowed = not all(bool(value) for value in capped.values())
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


def _enter_island(
    app: Any,
    vision: Any,
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
        last_match = _wait_template(
            app,
            vision,
            _ISLAND_HOME_TEMPLATE,
            _ISLAND_HOME_REGION,
            timeout_sec=page_timeout_sec,
            interval_sec=interval_sec,
        )
        if last_match.get("found"):
            return {"attempts": attempt, "click": point, "match": last_match}
    _raise_error(
        "cape_island_entry_timeout",
        "clicked the Cape City island coordinate but the island page was not confirmed",
        {"click": point, "attempts": max(int(transition_attempts), 1), "last_match": last_match},
    )
    return {}


def _open_revenue_overview(
    app: Any,
    vision: Any,
    *,
    page_timeout_sec: float,
    interval_sec: float,
    transition_attempts: int,
) -> Dict[str, Any]:
    last_match: Dict[str, Any] = {}
    for attempt in range(1, max(int(transition_attempts), 1) + 1):
        app.click(x=_OPEN_REVENUE_SAFE_POINT[0], y=_OPEN_REVENUE_SAFE_POINT[1])
        last_match = _wait_template(
            app,
            vision,
            _REVENUE_OVERVIEW_TEMPLATE,
            _REVENUE_OVERVIEW_REGION,
            timeout_sec=page_timeout_sec,
            interval_sec=interval_sec,
        )
        if last_match.get("found"):
            return {
                "attempts": attempt,
                "click": {"x": _OPEN_REVENUE_SAFE_POINT[0], "y": _OPEN_REVENUE_SAFE_POINT[1]},
                "match": last_match,
            }
    _raise_error(
        "revenue_overview_timeout",
        "the fixed island background click did not open the revenue overview",
        {"attempts": max(int(transition_attempts), 1), "last_match": last_match},
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
def resonance_pc_execute_cape_island_investment_from_city_panel(
    location_file_path: str = "data/meta/location_pc.json",
    page_timeout_sec: float = 12.0,
    metric_timeout_sec: float = 8.0,
    interval_sec: float = 0.3,
    transition_attempts: int = 3,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None or resonance_pc_city_shop_data is None:
        raise RuntimeError("app/ocr/vision/resonance_pc_city_shop_data services are required")
    started_at = time.monotonic()
    island_entry = _enter_island(
        app,
        vision,
        resonance_pc_city_shop_data,
        location_file_path=location_file_path,
        page_timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        transition_attempts=transition_attempts,
    )
    revenue_overview = _open_revenue_overview(
        app,
        vision,
        page_timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        transition_attempts=transition_attempts,
    )
    investment_tab = _click_template_required(
        app,
        vision,
        _INVESTMENT_TAB_TEMPLATE,
        _INVESTMENT_TAB_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        error_code="investment_tab_not_found",
    )
    investment_page = _wait_template(
        app,
        vision,
        _INVESTMENT_PAGE_TEMPLATE,
        _INVESTMENT_PAGE_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
    )
    if not investment_page.get("found"):
        _raise_error(
            "investment_page_not_confirmed",
            "the island investment page did not appear after clicking its tab",
            {"investment_tab": investment_tab, "last_match": investment_page},
        )
    metric_result = _read_investment_metrics(
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

    options = [
        _classify_card(app, vision, slot=index + 1, region=region)
        for index, region in enumerate(_CARD_ICON_REGIONS)
    ]
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
    invest_click = _click_template_required(
        app,
        vision,
        _INVEST_BUTTON_TEMPLATE,
        _CARD_BUTTON_REGIONS[slot_index],
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        error_code="investment_button_not_found",
    )
    success_match = _wait_template(
        app,
        vision,
        _INVESTMENT_SUCCESS_TEMPLATE,
        _INVESTMENT_SUCCESS_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
    )
    if not success_match.get("found"):
        _raise_error(
            "investment_success_not_confirmed",
            "the investment success overlay did not appear",
            {"selected_option": selected, "invest_click": invest_click, "last_match": success_match},
        )
    app.click(x=_SUCCESS_DISMISS_POINT[0], y=_SUCCESS_DISMISS_POINT[1])
    dismissed = _wait_template(
        app,
        vision,
        _INVESTMENT_SUCCESS_TEMPLATE,
        _INVESTMENT_SUCCESS_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        should_exist=False,
    )
    returned_page = _wait_template(
        app,
        vision,
        _INVESTMENT_PAGE_TEMPLATE,
        _INVESTMENT_PAGE_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
    )
    if dismissed.get("found") or not returned_page.get("found"):
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
