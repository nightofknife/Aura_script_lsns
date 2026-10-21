"""Actions for ResonancePc intercity destination selection."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.observability.runtime_trace import log_event, log_poll
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from ._city_panel_vision import CityPanelVisionError, probe_visit_entry
from ._depart_button_vision import DepartButtonError, probe_depart_button, wait_depart_button, POLL_INTERVAL
from ._operation_progress import observe_operation
from ._freight_trailer_policy import validate_trailer_options, verified_quota_skip
from ._intercity_map_drag import perform_map_drag
from ._intercity_map_geometry import (
    MapNavigationError, REFERENCE_CLIENT, SCREEN_SCALE, SAFE_DRAG_RECT,
    MAX_CORRECTION_ROUNDS, load_native_cities, require_reference_client,
    coarse_drag, plan_drag_batch, predict_target_from_labels,
)

class IntercityDestinationError(RuntimeError):
    """Structured error for intercity destination action."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_PLAN_ROOT = Path(__file__).resolve().parents[2]

# Exclude the top HUD and leave room for the default -15px label-to-icon click.
_DEFAULT_CITY_SEARCH_REGION = [120, 120, 1100, 560]  # x,y,w,h; bottom unchanged
_DEFAULT_DRAG_CENTER = [640, 360]  # x,y
_DEFAULT_FULL_SCREEN_REGION = [0, 0, 1280, 720]

_DEPART_CONFIRM_MARKERS = ("立即出发",)

_GO_DESTINATION_TEMPLATE = "templates/go_destination_button.png"
_ARRIVAL_BUTTON_TEMPLATE = "templates/enter_station_button.png"
_TRAVEL_HORN_TEMPLATE = "templates/travel_horn.png"
_TRAVEL_HORN_REGION = [960, 640, 80, 80]
_TRAVEL_HORN_THRESHOLD = 0.90
_AUTO_PICKUP_POINT = (770, 400)
_AUTO_PICKUP_INTERVAL = 0.5


def _sleep_with_cancel(duration: float) -> None:
    """Keep UI settling waits responsive without interrupting held gestures."""
    remaining = max(float(duration), 0.0)
    _check_intercity_cancelled()
    while remaining > 0:
        interval = min(remaining, 0.05)
        time.sleep(interval)
        _check_intercity_cancelled()
        remaining -= interval



def _normalize_pickup_interval(value: Any) -> float:
    if type(value) not in (int, float) or not 0.1 <= value <= 60.0 or not math.isfinite(value):
        raise ValueError("auto_pickup_interval_sec must be a finite number between 0.1 and 60 seconds")
    return float(value)


_FATIGUE_PANEL_TEMPLATE = "templates/fatigue_recovery_panel_title.png"
_FATIGUE_BACK_TEMPLATE = "templates/fatigue_recovery_back_button.png"
_FATIGUE_MEDICINE_CONFIRM_TEMPLATE = "templates/fatigue_medicine_confirm_button.png"

_GO_DESTINATION_REGION = [900, 580, 340, 120]
_ARRIVAL_BUTTON_REGION = [780, 325, 240, 70]
_DEPART_CONFIRM_REGION = [450, 360, 760, 220]
_FATIGUE_PANEL_REGION = [70, 80, 520, 190]
_FATIGUE_BACK_REGION = [0, 0, 260, 90]
_FATIGUE_MEDICINE_CONFIRM_REGION = [620, 520, 660, 120]

_DEFAULT_ARRIVAL_TIMEOUT_SECONDS = 1800.0

_WEEKLY_NOTICE_CHECKBOX = [890, 523]
_DEPART_CONFIRM_POINT = [852, 447]
_FATIGUE_BACK_FALLBACK_POINT = [82, 36]

_FATIGUE_MEDICINE_ORDER: List[Dict[str, Any]] = [
    {
        "name": "提神棒棒糖",
        "template": "templates/fatigue_medicine_stimulant_lollipop_button.png",
        "region": [540, 310, 240, 90],
    },
    {
        "name": "提神口香糖",
        "template": "templates/fatigue_medicine_stimulant_gum_button.png",
        "region": [780, 310, 240, 90],
    },
    {
        "name": "仙人掌提神跳糖",
        "template": "templates/fatigue_medicine_cactus_jump_candy_button.png",
        "region": [540, 560, 240, 90],
    },
    {
        "name": "桦石",
        "template": "templates/fatigue_medicine_birch_stone_button.png",
        "region": [780, 560, 240, 90],
    },
]

_CITY_KEY_DISPLAY_NAME: Dict[str, str] = {
    "anita_energy_research_institute": "阿妮塔能源研究所",
    "freeport": "7号自由港",
    "clarity_data_center_administration_bureau": "澄明数据中心",
    "shoggolith_city": "修格里城",
    "brcl_outpost": "铁盟哨站",
    "wilderness_station": "荒原站",
    "mander_mine": "曼德矿场",
    "onederland": "淘金乐园",
    "anita_weapon_research_institute": "阿妮塔战备工厂",
    "anita_rocket_base": "阿妮塔发射中心",
    "yunxiuqiao_base": "云岫桥基地",
    "farstar_bridge": "远星大桥",
    "lanxin_city": "岚心城",
    "qiyu_station": "栖羽站",
    "tatu_station": "塔图站",
    "black_moon_amusement_park": "黑月游乐城",
    "gronru_city": "贡露城",
    "vitilin_forest": "维蒂林场",
    "cape_city": "海角城",
    "confluence_tower": "汇流塔",
    "wulin_source": "武林源",
}

_CITY_ALIAS_TO_KEY: Dict[str, str] = {
    "阿妮塔能源研究所": "anita_energy_research_institute",
    "阿妮塔能原研究所": "anita_energy_research_institute",
    "7号自由港": "freeport",
    "七号自由港": "freeport",
    "7号自电港": "freeport",
    "澄明数据中心": "clarity_data_center_administration_bureau",
    "修格里城": "shoggolith_city",
    "修格果城": "shoggolith_city",
    "修格男城": "shoggolith_city",
    "铁盟哨站": "brcl_outpost",
    "荒原站": "wilderness_station",
    "曼德矿场": "mander_mine",
    "淘金乐园": "onederland",
    "阿妮塔战备工厂": "anita_weapon_research_institute",
    "阿妮塔发射中心": "anita_rocket_base",
    "云岫桥基地": "yunxiuqiao_base",
    "远星大桥": "farstar_bridge",
    "岚心城": "lanxin_city",
    "栖羽站": "qiyu_station",
    "塔图站": "tatu_station",
    "黑月游乐城": "black_moon_amusement_park",
    "贡露城": "gronru_city",
    "格罗努城": "gronru_city",
    "维蒂林场": "vitilin_forest",
    "海角城": "cape_city",
    "汇流塔": "confluence_tower",
    "沃德镇": "confluence_tower",
    "武林源": "wulin_source",
}

_CITY_OCR_TEXT_REPLACEMENTS: Dict[str, str] = {
    # Temporary compatibility for a recurring intercity-map OCR confusion.
    "图站": "塔图站",
    "云帅桥基地": "云岫桥基地",
    "云崎桥基地": "云岫桥基地",
    "云帕桥基地": "云岫桥基地",
}


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    raise IntercityDestinationError(code=code, message=message, detail=detail)


def _normalize_text(text: Any) -> str:
    normalized = re.sub(r"[\s\u3000\|:：,，。!?！？()（）\[\]【】<>《》\"'`~\-]+", "", str(text))
    return normalized.strip().lower()


def _normalize_city_ocr_text(text: Any) -> str:
    normalized = _normalize_text(text)
    for observed_text, canonical_text in _CITY_OCR_TEXT_REPLACEMENTS.items():
        normalized = normalized.replace(observed_text, canonical_text)
    return normalized




def _coerce_region(region: Any, default_region: List[int]) -> List[int]:
    value = default_region if region is None else region
    if not isinstance(value, list) or len(value) != 4:
        _raise_error(
            code="invalid_region",
            message="city_search_region must be [x, y, w, h].",
            detail={"city_search_region": value},
        )
    try:
        x, y, w, h = [int(v) for v in value]
    except (TypeError, ValueError):
        _raise_error(
            code="invalid_region",
            message="city_search_region values must be integers.",
            detail={"city_search_region": value},
        )
    if w <= 0 or h <= 0:
        _raise_error(
            code="invalid_region",
            message="city_search_region width/height must be positive.",
            detail={"city_search_region": [x, y, w, h]},
        )
    return [x, y, w, h]


def _coerce_point(point: Any, default_point: List[int]) -> List[int]:
    value = default_point if point is None else point
    if not isinstance(value, list) or len(value) != 2:
        _raise_error(
            code="invalid_drag_center",
            message="drag_center must be [x, y].",
            detail={"drag_center": value},
        )
    try:
        x, y = [int(v) for v in value]
    except (TypeError, ValueError):
        _raise_error(
            code="invalid_drag_center",
            message="drag_center values must be integers.",
            detail={"drag_center": value},
        )
    return [x, y]


def _build_alias_lookup(city_table: Dict[str, Any]) -> Dict[str, str]:
    alias_lookup: Dict[str, str] = {}
    for city_key in city_table.keys():
        normalized_key = _normalize_text(city_key)
        if normalized_key:
            alias_lookup[normalized_key] = city_key
        display_name = _CITY_KEY_DISPLAY_NAME.get(city_key)
        if display_name:
            normalized_display = _normalize_text(display_name)
            if normalized_display:
                alias_lookup[normalized_display] = city_key
    for alias, city_key in _CITY_ALIAS_TO_KEY.items():
        if city_key not in city_table:
            continue
        normalized_alias = _normalize_text(alias)
        if normalized_alias:
            alias_lookup[normalized_alias] = city_key
    return alias_lookup


def _resolve_city_key_from_name(city_name: str, city_table: Dict[str, Any], alias_lookup: Dict[str, str]) -> str:
    raw = str(city_name or "").strip()
    if not raw:
        _raise_error(code="to_city_not_resolved", message="to_city_name is required.")
    if raw in city_table:
        return raw
    normalized = _normalize_text(raw)
    if normalized in alias_lookup:
        return alias_lookup[normalized]
    for alias_norm in sorted(alias_lookup.keys(), key=len, reverse=True):
        if alias_norm and alias_norm in normalized:
            return alias_lookup[alias_norm]
    _raise_error(
        code="to_city_not_resolved",
        message=f"Unable to resolve target city '{raw}'.",
        detail={"to_city_name": raw, "available_city_keys": sorted(city_table.keys())},
    )
    return ""


def _resolve_city_key_from_ocr_norm(text_norm: str, alias_lookup: Dict[str, str]) -> Optional[str]:
    if not text_norm:
        return None
    if text_norm in alias_lookup:
        return alias_lookup[text_norm]
    for alias_norm in sorted(alias_lookup.keys(), key=len, reverse=True):
        if alias_norm and alias_norm in text_norm:
            return alias_lookup[alias_norm]
    return None


def _build_target_alias_set(target_city_key: str) -> set[str]:
    aliases = {target_city_key}
    display = _CITY_KEY_DISPLAY_NAME.get(target_city_key)
    if display:
        aliases.add(display)
    for alias, city_key in _CITY_ALIAS_TO_KEY.items():
        if city_key == target_city_key:
            aliases.add(alias)
    return {n for n in (_normalize_text(v) for v in aliases) if n}


def _capture_and_ocr_city_labels(
    app: Any,
    ocr: Any,
    city_search_region: List[int],
) -> List[Dict[str, Any]]:
    _check_intercity_cancelled()
    capture = app.capture(rect=tuple(city_search_region))
    if not capture.success:
        _raise_error(
            code="capture_failed",
            message="Failed to capture intercity map region.",
            detail={"city_search_region": city_search_region},
        )
    multi = ocr.recognize_all(source_image=capture.image)
    observed: List[Dict[str, Any]] = []
    for item in getattr(multi, "results", []) or []:
        text = str(getattr(item, "text", "") or "")
        if not text.strip():
            continue
        center = getattr(item, "center_point", None)
        if not center or len(center) != 2:
            continue
        try:
            local_x, local_y = float(center[0]), float(center[1])
            confidence = float(getattr(item, "confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if (not all(math.isfinite(value) for value in (local_x, local_y, confidence))
                or not 0 <= confidence <= 1
                or not 0 <= local_x < city_search_region[2]
                or not 0 <= local_y < city_search_region[3]):
            continue
        abs_x = int(city_search_region[0] + int(local_x))
        abs_y = int(city_search_region[1] + int(local_y))
        observed.append(
            {
                "text": text,
                "norm_text": _normalize_city_ocr_text(text),
                "center": [abs_x, abs_y],
                "confidence": confidence,
            }
        )
    observed.sort(key=lambda x: x["confidence"], reverse=True)
    return observed


def _capture_and_ocr_text_items(
    app: Any,
    ocr: Any,
    region: List[int],
    *,
    diagnostic_label: Optional[str] = None,
    diagnostic_poll: Optional[int] = None,
) -> List[Dict[str, Any]]:
    _check_intercity_cancelled()
    capture_started = time.monotonic()
    if diagnostic_label:
        log_event("vision.capture", detail=True, label=diagnostic_label,
                  poll=diagnostic_poll, phase="started", region=region)
    capture = app.capture(rect=tuple(region))
    if diagnostic_label:
        log_event("vision.capture", detail=True, label=diagnostic_label, poll=diagnostic_poll,
                  phase="finished", elapsed_ms=round((time.monotonic() - capture_started) * 1000, 1),
                  success=bool(capture.success), image_shape=list(getattr(capture.image, "shape", ()) or ()))
    if not capture.success:
        _raise_error(
            code="capture_failed",
            message="Failed to capture screen region.",
            detail={"region": region},
        )

    ocr_started = time.monotonic()
    if diagnostic_label:
        log_event("vision.ocr", detail=True, label=diagnostic_label, poll=diagnostic_poll, phase="started")
    multi = ocr.recognize_all(source_image=capture.image)
    observed: List[Dict[str, Any]] = []
    for item in getattr(multi, "results", []) or []:
        text = str(getattr(item, "text", "") or "")
        if not text.strip():
            continue
        center = getattr(item, "center_point", None)
        if not center or len(center) != 2:
            continue
        observed.append(
            {
                "text": text,
                "norm_text": _normalize_text(text),
                "center": [int(region[0] + int(center[0])), int(region[1] + int(center[1]))],
                "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
            }
        )
    observed.sort(key=lambda x: x["confidence"], reverse=True)
    if diagnostic_label:
        log_event("vision.ocr", detail=True, label=diagnostic_label, poll=diagnostic_poll,
                  phase="finished", elapsed_ms=round((time.monotonic() - ocr_started) * 1000, 1),
                  count=len(observed), observed=observed)
    return observed


def _find_marker_hit(items: List[Dict[str, Any]], markers: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    normalized_markers = [(_normalize_text(marker), marker) for marker in markers]
    for item in items:
        norm = str(item.get("norm_text") or "")
        for normalized_marker, marker in normalized_markers:
            if normalized_marker and normalized_marker in norm:
                return {**item, "marker": marker}
    return None


def _find_target_hit(
    observed: List[Dict[str, Any]],
    target_alias_norms: set[str],
    match_mode: str,
) -> Optional[Dict[str, Any]]:
    mode = str(match_mode or "contains").strip().lower()
    for item in observed:
        norm = item.get("norm_text", "")
        if not norm:
            continue
        if mode == "exact":
            if norm in target_alias_norms:
                return item
            continue
        if mode == "contains":
            if any(alias in norm for alias in target_alias_norms):
                return item
            continue
        if mode == "regex":
            for alias in target_alias_norms:
                try:
                    if re.search(alias, norm):
                        return item
                except re.error:
                    if alias in norm:
                        return item
            continue
        if any(alias in norm for alias in target_alias_norms):
            return item
    return None




def _resolve_plan_template_path(template: str) -> str:
    raw = Path(str(template or "").strip())
    if raw.is_absolute():
        return str(raw)
    return str((_PLAN_ROOT / raw).resolve())


def _match_template_in_region(
    app: Any,
    vision: Any,
    template: str,
    region: List[int],
    threshold: float,
    use_grayscale: bool = True,
) -> Dict[str, Any]:
    _check_intercity_cancelled()
    capture = app.capture(rect=tuple(region))
    if not capture.success:
        return {"found": False, "template": template, "region": list(region), "reason": "capture_failed"}
    match = vision.find_template(
        source_image=capture.image,
        template_image=_resolve_plan_template_path(template),
        threshold=float(threshold),
        use_grayscale=bool(use_grayscale),
    )
    found = bool(getattr(match, "found", False))
    center = getattr(match, "center_point", None)
    rect = getattr(match, "rect", None)
    result: Dict[str, Any] = {
        "found": found,
        "template": template,
        "region": list(region),
        "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
    }
    if center and len(center) == 2:
        result["center"] = [int(region[0] + int(center[0])), int(region[1] + int(center[1]))]
    if rect and len(rect) == 4:
        result["rect"] = [
            int(region[0] + int(rect[0])),
            int(region[1] + int(rect[1])),
            int(rect[2]),
            int(rect[3]),
        ]
    return result


def _click_template_match(app: Any, match: Dict[str, Any]) -> bool:
    center = match.get("center")
    if not match.get("found") or not isinstance(center, list) or len(center) != 2:
        return False
    _check_intercity_cancelled()
    app.click(x=int(center[0]), y=int(center[1]))
    return True


def _click_arrival_template_until_absent(
    app: Any,
    vision: Any,
    *,
    poll_count: int,
    template: str,
    region: List[int],
    threshold: float,
    max_click_attempts: int,
    verify_interval_sec: float,
) -> Dict[str, Any]:
    _check_intercity_cancelled()
    match = _match_template_in_region(
        app=app,
        vision=vision,
        template=template,
        region=region,
        threshold=threshold,
    )
    log_poll("travel.arrival_template", bool(match.get("found")), poll=poll_count,
             confidence=float(match.get("confidence") or 0.0), center=match.get("center"), region=region)
    if not match.get("found"):
        return {
            "confirmed": False,
            "initial_match": match,
            "last_match": match,
            "click_attempts": 0,
            "clicks": [],
        }

    click_limit = max(int(max_click_attempts), 1)
    verify_interval = max(float(verify_interval_sec), 0.0)
    clicks: List[Dict[str, Any]] = []
    initial_match = dict(match)

    for attempt in range(1, click_limit + 1):
        _check_intercity_cancelled()
        center = match.get("center")
        if not isinstance(center, list) or len(center) != 2:
            _raise_error(
                code="arrival_template_center_missing",
                message="Arrival template matched without a usable center point.",
                detail={"poll_count": poll_count, "attempt": attempt, "match": match},
            )

        x, y = int(center[0]), int(center[1])
        _check_intercity_cancelled()
        app.click(x=x, y=y)
        click_record = {
            "attempt": attempt,
            "point": {"x": x, "y": y},
            "confidence": float(match.get("confidence") or 0.0),
        }
        clicks.append(click_record)
        logger.info(
            "[IntercityArrivalTemplate] poll=%s phase=click attempt=%s/%s at=(%s,%s) confidence=%.4f",
            poll_count,
            attempt,
            click_limit,
            x,
            y,
            click_record["confidence"],
        )

        if verify_interval > 0:
            _sleep_with_cancel(verify_interval)
        _check_intercity_cancelled()
        match = _match_template_in_region(
            app=app,
            vision=vision,
            template=template,
            region=region,
            threshold=threshold,
        )
        logger.info(
            "[IntercityArrivalTemplate] poll=%s phase=verify attempt=%s found=%s confidence=%.4f center=%s",
            poll_count,
            attempt,
            bool(match.get("found")),
            float(match.get("confidence") or 0.0),
            match.get("center"),
        )
        if not match.get("found"):
            if verify_interval > 0:
                _sleep_with_cancel(verify_interval)
            _check_intercity_cancelled()
            confirmation = _match_template_in_region(
                app=app,
                vision=vision,
                template=template,
                region=region,
                threshold=threshold,
            )
            logger.info(
                "[IntercityArrivalTemplate] poll=%s phase=confirm_absent attempt=%s found=%s confidence=%.4f center=%s",
                poll_count,
                attempt,
                bool(confirmation.get("found")),
                float(confirmation.get("confidence") or 0.0),
                confirmation.get("center"),
            )
            if confirmation.get("found"):
                match = confirmation
                continue
            return {
                "confirmed": True,
                "click_attempts": attempt,
                "arrival_point": {"x": x, "y": y},
                "clicks": clicks,
                "initial_match": initial_match,
                "last_match": confirmation,
            }

    _raise_error(
        code="enter_station_click_not_effective",
        message=f"Arrival button remained visible after {click_limit} click attempts.",
        detail={
            "poll_count": poll_count,
            "template": template,
            "region": region,
            "threshold": threshold,
            "clicks": clicks,
            "last_match": match,
        },
    )


def _detect_city_main_entry(app: Any, vision: Any, *, poll_count: int) -> Dict[str, Any]:
    _check_intercity_cancelled()
    try:
        match = probe_visit_entry(app=app, vision=vision, check_cancelled=_check_intercity_cancelled)
    except CityPanelVisionError as exc:
        _raise_error(exc.code, exc.message, exc.detail)
    found = bool(match.get("found"))
    evidence = {
        **match,
        "method": "visit_entry_template",
        "hit": dict(match) if found else None,
    }
    log_poll("travel.arrival_city", found, poll=poll_count,
             method="visit_entry_template", confidence=match.get("confidence"),
             center=match.get("center"), region=match.get("region"))
    return evidence


def _check_intercity_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Resonance PC intercity operation was cancelled")


def _open_intercity_map(app: Any, vision: Any) -> Dict[str, Any]:
    """Click only a current departure hit, retrying only a still-visible button."""
    try:
        ready = wait_depart_button(app=app, vision=vision, check_cancelled=_check_intercity_cancelled,
                                  timeout_sec=3.0)
        if not ready["confirmed"]:
            _raise_error("depart_button_not_found", "Unable to confirm the departure button", ready)
        match = ready["match"]
        for attempt in (1, 2):
            _check_intercity_cancelled()
            app.click(x=int(match["center"][0]), y=int(match["center"][1]))
            logger.info("[IntercityDeparture] phase=depart_clicked attempt=%s match=%s", attempt, match)
            _sleep_with_cancel(1.0)
            _check_intercity_cancelled()
            present = absent = 0
            for poll in range(5):
                match = probe_depart_button(app=app, vision=vision, check_cancelled=_check_intercity_cancelled)
                present = present+1 if match["found"] else 0
                absent = absent+1 if not match["found"] else 0
                if absent >= 2:
                    # This is only a transition signal; the existing map selector
                    # must still identify and select the actual destination.
                    return {"transitioned": True, "click_attempts": attempt, "last_match": match}
                if present >= 2:
                    break
                if poll < 4:
                    _sleep_with_cancel(POLL_INTERVAL)
                    _check_intercity_cancelled()
            if present < 2:
                break
        _raise_error("depart_transition_unconfirmed", "Departure button did not leave the main screen",
                     {"click_attempts": attempt, "last_match": match})
    except DepartButtonError as exc:
        _raise_error(exc.code, exc.message, exc.detail)


def _wait_for_marker_hit(
    app: Any,
    ocr: Any,
    markers: Tuple[str, ...],
    *,
    timeout_sec: float,
    interval_sec: float,
    region: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    _check_intercity_cancelled()
    screen_region = _coerce_region(region, _DEFAULT_FULL_SCREEN_REGION)
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    interval = max(float(interval_sec), 0.1)
    while time.monotonic() <= deadline:
        _check_intercity_cancelled()
        items = _capture_and_ocr_text_items(app=app, ocr=ocr, region=screen_region)
        hit = _find_marker_hit(items, markers)
        if hit is not None:
            return hit
        _sleep_with_cancel(interval)
    return None


def _click_marker_hit(app: Any, hit: Dict[str, Any]) -> bool:
    center = hit.get("center")
    if not isinstance(center, list) or len(center) != 2:
        return False
    _check_intercity_cancelled()
    app.click(x=int(center[0]), y=int(center[1]))
    return True


def _normalize_allowed_fatigue_medicines(value: Any) -> set[str]:
    if value is None:
        return set()
    raw_items: List[Any]
    if isinstance(value, str):
        raw_items = [item for item in re.split(r"[,，;；\n]+", value) if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    known_by_norm = {_normalize_text(item["name"]): item["name"] for item in _FATIGUE_MEDICINE_ORDER}
    allowed: set[str] = set()
    for raw in raw_items:
        norm = _normalize_text(str(raw or ""))
        if norm in known_by_norm:
            allowed.add(known_by_norm[norm])
    return allowed


def _merge_medicine_usage(usage: Dict[str, int]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in _FATIGUE_MEDICINE_ORDER:
        name = item["name"]
        count = int(usage.get(name) or 0)
        if count > 0:
            result.append({"name": name, "count": count})
    return result


def _find_allowed_fatigue_medicine(
    app: Any,
    vision: Any,
    allowed_names: set[str],
    *,
    threshold: float,
    excluded_names: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    excluded = excluded_names or set()
    for item in _FATIGUE_MEDICINE_ORDER:
        name = str(item["name"])
        if name not in allowed_names or name in excluded:
            continue
        match = _match_template_in_region(
            app=app,
            vision=vision,
            template=str(item["template"]),
            region=list(item["region"]),
            threshold=float(threshold),
            use_grayscale=True,
        )
        if match.get("found"):
            return {**item, "match": match}
    return None


def _click_fatigue_back(app: Any, vision: Any, threshold: float = 0.95) -> Dict[str, Any]:
    match = _match_template_in_region(
        app=app,
        vision=vision,
        template=_FATIGUE_BACK_TEMPLATE,
        region=_FATIGUE_BACK_REGION,
        threshold=threshold,
        use_grayscale=True,
    )
    if _click_template_match(app, match):
        return {"clicked": True, "method": "template", "match": match}
    _check_intercity_cancelled()
    app.click(x=int(_FATIGUE_BACK_FALLBACK_POINT[0]), y=int(_FATIGUE_BACK_FALLBACK_POINT[1]))
    return {"clicked": True, "method": "fallback", "match": match}


def _wait_and_click_fatigue_medicine_confirm(
    app: Any,
    vision: Any,
    ocr: Any,
    *,
    threshold: float,
    timeout_sec: float = 3.0,
    interval_sec: float = 0.3,
) -> Dict[str, Any]:
    _check_intercity_cancelled()
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    interval = max(float(interval_sec), 0.1)
    last_match: Dict[str, Any] = {"found": False}
    while time.monotonic() <= deadline:
        _check_intercity_cancelled()
        match = _match_template_in_region(
            app=app,
            vision=vision,
            template=_FATIGUE_MEDICINE_CONFIRM_TEMPLATE,
            region=_FATIGUE_MEDICINE_CONFIRM_REGION,
            threshold=float(threshold),
            use_grayscale=True,
        )
        last_match = match
        if _click_template_match(app, match):
            return {"clicked": True, "method": "template", "match": match}
        _sleep_with_cancel(interval)

    text_hit = _wait_for_marker_hit(
        app=app,
        ocr=ocr,
        markers=("补充",),
        timeout_sec=1.0,
        interval_sec=0.3,
        region=_FATIGUE_MEDICINE_CONFIRM_REGION,
    )
    if text_hit is not None and _click_marker_hit(app, text_hit):
        return {"clicked": True, "method": "text", "text": text_hit, "match": last_match}
    return {"clicked": False, "method": None, "match": last_match}


def _blocked_departure_result(
    *,
    reason: str,
    selected: Optional[Dict[str, Any]],
    to_city_name: str,
    medicine_usage: Dict[str, int],
    medicine_limit: int,
    back_result: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    selected = selected if isinstance(selected, dict) else {}
    payload: Dict[str, Any] = {
        "success": False,
        "status": "blocked",
        "reason": reason,
        "blocked_at": "departure",
        "to_city_name": selected.get("to_city_name") or to_city_name,
        "to_city_key": selected.get("to_city_key"),
        "selected_point": selected.get("selected_point"),
        "mode": selected.get("mode"),
        "attempts_used": selected.get("attempts_used"),
        "arrival_status": None,
        "encounter_actions": 0,
        "fatigue_medicine_used": _merge_medicine_usage(medicine_usage),
        "fatigue_medicine_use_count": sum(int(v) for v in medicine_usage.values()),
        "fatigue_medicine_limit": int(medicine_limit),
    }
    if back_result is not None:
        payload["fatigue_back"] = back_result
    if extra:
        payload.update(extra)
    observe_operation("travel.fatigue_recovery", "疲劳恢复未完成", "blocked",
                      reason=reason, fatigue_medicine_used=payload["fatigue_medicine_used"])
    gate = payload.get("departure_gate") or {}
    if "trailer_service" in gate:
        payload["trailer_service"] = gate["trailer_service"]
        payload["trailer_attempts"] = gate["trailer_attempts"]
    return payload


def _wait_and_click_go_destination(
    app: Any,
    vision: Any,
    ocr: Any,
    *,
    template_timeout_sec: float,
    interval_sec: float,
) -> Dict[str, Any]:
    _check_intercity_cancelled()
    deadline = time.monotonic() + max(float(template_timeout_sec), 0.1)
    template_match: Dict[str, Any] = {"found": False}
    while time.monotonic() <= deadline:
        _check_intercity_cancelled()
        template_match = _match_template_in_region(
            app=app,
            vision=vision,
            template=_GO_DESTINATION_TEMPLATE,
            region=_GO_DESTINATION_REGION,
            threshold=0.82,
            use_grayscale=True,
        )
        if _click_template_match(app, template_match):
            return {"clicked": True, "method": "template", "match": template_match}
        _sleep_with_cancel(max(float(interval_sec), 0.1))

    text_hit = _wait_for_marker_hit(
        app=app,
        ocr=ocr,
        markers=("前往",),
        timeout_sec=1.5,
        interval_sec=0.5,
        region=_GO_DESTINATION_REGION,
    )
    if text_hit is not None and _click_marker_hit(app, text_hit):
        return {"clicked": True, "method": "text", "text": text_hit, "match": template_match}
    return {"clicked": False, "method": None, "match": template_match}


def _wait_departure_gate(
    app: Any,
    ocr: Any,
    vision: Any,
    *,
    timeout_sec: float,
    interval_sec: float,
) -> Dict[str, Any]:
    _check_intercity_cancelled()
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    interval = max(float(interval_sec), 0.1)
    last_panel_match: Dict[str, Any] = {"found": False}
    last_confirm_hit: Optional[Dict[str, Any]] = None
    while time.monotonic() <= deadline:
        _check_intercity_cancelled()
        panel_match = _match_template_in_region(
            app=app,
            vision=vision,
            template=_FATIGUE_PANEL_TEMPLATE,
            region=_FATIGUE_PANEL_REGION,
            threshold=0.90,
            use_grayscale=True,
        )
        last_panel_match = panel_match
        if panel_match.get("found"):
            return {"state": "fatigue_panel", "panel_match": panel_match}

        confirm_hit = _wait_for_marker_hit(
            app=app,
            ocr=ocr,
            markers=_DEPART_CONFIRM_MARKERS,
            timeout_sec=0.1,
            interval_sec=0.1,
            region=_DEPART_CONFIRM_REGION,
        )
        last_confirm_hit = confirm_hit
        if confirm_hit is not None:
            _check_intercity_cancelled()
            app.click(x=int(_WEEKLY_NOTICE_CHECKBOX[0]), y=int(_WEEKLY_NOTICE_CHECKBOX[1]))
            _sleep_with_cancel(0.2)
            _check_intercity_cancelled()
            app.click(x=int(_DEPART_CONFIRM_POINT[0]), y=int(_DEPART_CONFIRM_POINT[1]))
            return {"state": "confirm_clicked", "confirm_hit": confirm_hit}
        _sleep_with_cancel(interval)
    return {
        "state": "assume_traveling",
        "panel_match": last_panel_match,
        "confirm_hit": last_confirm_hit,
    }


def _native_navigation_context(app: Any, to_city_name: str, from_city_name: Optional[str]):
    try:
        require_reference_client(app)
        cities = load_native_cities()
    except MapNavigationError as exc:
        _raise_error(exc.code, exc.message, exc.detail)
    aliases = _build_alias_lookup(cities)
    if not str(from_city_name or "").strip():
        _raise_error("from_city_not_resolved",
                     "A confirmed starting city is required before opening the default intercity map.")
    source = _resolve_city_key_from_name(str(from_city_name), cities, aliases)
    target = _resolve_city_key_from_name(to_city_name, cities, aliases)
    return cities, aliases, source, target


@action_info(
    name="resonance_pc.select_intercity_destination",
    public=True,
    read_only=False,
    description="Coarse-drag the fresh default 1280x720 map using native city coordinates, then OCR and correct.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_select_intercity_destination(
    to_city_name: str,
    location_file_path: str = "data/meta/location_pc.json",
    city_search_region: Optional[List[int]] = None,
    drag_center: Optional[List[int]] = None,
    drag_span_px: int = 900,
    max_search_steps: int = 12,
    fallback_enabled: bool = True,
    target_match_mode: str = "contains",
    click_y_offset: int = -15,
    drag_duration_sec: float = 1.0,
    drag_hold_sec: float = 0.5,
    app: Any = None,
    ocr: Any = None,
    from_city_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Requires a freshly opened, default-zoom map centered on the known start.

    location_file_path remains accepted for existing callers. Map movement uses
    the native coordinate catalog; legacy maploc units are never used here.
    fallback_enabled now controls bounded correction after the coarse batch.
    """
    _check_intercity_cancelled()
    if app is None or ocr is None:
        raise RuntimeError("app/ocr services are required for select_intercity_destination.")
    cities, aliases, source, target = _native_navigation_context(app, to_city_name, from_city_name)
    region = _coerce_region(city_search_region, _DEFAULT_CITY_SEARCH_REGION)
    center = _coerce_point(drag_center, _DEFAULT_DRAG_CENTER)
    if (region[0] < 0 or region[1] < 0 or region[0] + region[2] > REFERENCE_CLIENT[0]
            or region[1] + region[3] > REFERENCE_CLIENT[1]):
        _raise_error("invalid_region", "Map OCR region must stay inside the 1280x720 client.",
                     {"city_search_region": region})
    if not (region[0] <= center[0] < region[0] + region[2]
            and region[1] <= center[1] < region[1] + region[3]):
        _raise_error("invalid_drag_center", "Correction center must be inside the map observation region.",
                     {"drag_center": center, "city_search_region": region})
    span = max(int(drag_span_px), 60)
    drag_limit = max(int(max_search_steps), 1)
    attempts: List[Dict[str, Any]] = []
    total_drag_count = 0
    ocr_count = 0
    last_seen_texts: List[str] = []
    stop_reason = "target_not_confirmed"
    previous_prediction = None

    def move_batch(delta, phase: str, correction_round: int) -> None:
        nonlocal total_drag_count
        try:
            batch = plan_drag_batch(delta, SAFE_DRAG_RECT, span)
            if total_drag_count + len(batch) > drag_limit:
                _raise_error("destination_not_found_after_drag",
                             "Native map movement exceeds the remaining drag budget; batch not started.",
                             {"phase": phase, "required_drags": len(batch),
                              "remaining_drags": drag_limit - total_drag_count,
                              "requested_delta": list(delta), "attempt_trace": attempts})
            logger.info(
                "[IntercityNativeBatch] phase=%s from=%s target=%s native_scale=%s delta=%s drags=%s",
                phase, source, target, SCREEN_SCALE, list(delta), len(batch),
            )
            # There are deliberately no capture or OCR calls inside this batch.
            for requested_delta in batch:
                _check_intercity_cancelled()
                require_reference_client(app)
                gesture = perform_map_drag(
                    app=app, effective_delta=requested_delta, safe_rect=SAFE_DRAG_RECT,
                    check_cancelled=_check_intercity_cancelled,
                    duration=float(drag_duration_sec), hold_sec=float(drag_hold_sec),
                )
                total_drag_count += 1
                record = {"step": total_drag_count, "phase": phase,
                          "correction_round": correction_round, "gesture": gesture}
                attempts.append(record)
                logger.info("[IntercityNativeDrag] target=%s record=%s", target, record)
                _sleep_with_cancel(0.1)
        except MapNavigationError as exc:
            _raise_error(exc.code, exc.message, {**exc.detail, "attempt_trace": attempts})
        except ValueError as exc:
            _raise_error("map_drag_invalid", str(exc), {"attempt_trace": attempts})

    move_batch(coarse_drag(cities, source, target), "coarse", 0)
    _sleep_with_cancel(0.2)
    target_aliases = _build_target_alias_set(target)
    for correction_round in range(MAX_CORRECTION_ROUNDS + 1):
        _check_intercity_cancelled()
        try:
            require_reference_client(app)
        except MapNavigationError as exc:
            _raise_error(exc.code, exc.message, exc.detail)
        # Retry a blank/unreadable frame once without issuing another blind drag.
        for capture_attempt in range(2):
            observed = _capture_and_ocr_city_labels(app=app, ocr=ocr, city_search_region=region)
            ocr_count += 1
            last_seen_texts = [str(item.get("text", "")) for item in observed[:20]]
            usable = [item for item in observed if float(item.get("confidence") or 0) >= 0.8]
            hit = _find_target_hit(observed=usable, target_alias_norms=target_aliases,
                                   match_mode=target_match_mode)
            anchors = {}
            for item in usable:
                key = _resolve_city_key_from_ocr_norm(str(item.get("norm_text") or ""), aliases)
                if key in cities and key not in anchors:
                    anchors[key] = {"city_key": key, "center": list(item["center"]),
                                    "confidence": item["confidence"], "text": item["text"]}
            logger.info(
                "[IntercitySelectOCR] phase=post_batch round=%s observation=%s target=%s observed=%s",
                correction_round, ocr_count, target, json.dumps(observed[:20], ensure_ascii=False),
            )
            if hit is not None or anchors or capture_attempt == 1:
                break
            _sleep_with_cancel(0.25)

        if hit is not None:
            click_x, click_y = int(hit["center"][0]), int(hit["center"][1]) + int(click_y_offset)
            if not (0 <= click_x < REFERENCE_CLIENT[0] and 0 <= click_y < REFERENCE_CLIENT[1]):
                _raise_error("destination_click_out_of_bounds", "Recognized city click is outside the client.",
                             {"hit": hit, "click": [click_x, click_y]})
            _check_intercity_cancelled()
            try:
                require_reference_client(app)
            except MapNavigationError as exc:
                _raise_error(exc.code, exc.message, exc.detail)
            app.click(x=click_x, y=click_y)
            mode = "native_coarse" if correction_round == 0 else "native_corrected"
            logger.info("[IntercitySelectHit] target=%s mode=%s hit=%s click=%s",
                        target, mode, hit, [click_x, click_y])
            return {"success": True, "to_city_key": target, "to_city_name": cities[target]["name"],
                    "selected_point": {"x": click_x, "y": click_y}, "mode": mode,
                    "attempts_used": ocr_count, "ocr_count": ocr_count,
                    "correction_rounds": correction_round, "total_drag_count": total_drag_count,
                    "screen_scale": SCREEN_SCALE, "attempt_trace": attempts}

        if not anchors:
            stop_reason = "no_supported_city_labels"
            break
        if not fallback_enabled:
            stop_reason = "correction_disabled"
            break
        if correction_round >= MAX_CORRECTION_ROUNDS:
            stop_reason = "correction_budget_exhausted"
            break
        try:
            pose = predict_target_from_labels(list(anchors.values()), cities, target)
        except MapNavigationError as exc:
            _raise_error(exc.code, exc.message, {**exc.detail, "attempt_trace": attempts})
        predicted = pose["predicted_target"]
        if previous_prediction is not None and max(
                abs(predicted[axis] - previous_prediction[axis]) for axis in (0, 1)) < 3:
            stop_reason = "map_movement_stalled"
            break
        delta = (center[0] - predicted[0], center[1] - predicted[1])
        if max(abs(value) for value in delta) < 12:
            stop_reason = "target_label_not_confirmed_near_prediction"
            break
        attempts.append({"phase": "correction_pose", "round": correction_round + 1, **pose})
        previous_prediction = predicted
        move_batch(delta, "correction", correction_round + 1)
        _sleep_with_cancel(0.2)

    _raise_error(
        "destination_not_found_after_drag",
        f"Destination '{to_city_name}' was not confirmed after native map movement ({stop_reason}).",
        {"to_city_name": to_city_name, "from_city_name": from_city_name, "to_city_key": target,
         "total_drag_count": total_drag_count, "ocr_count": ocr_count, "stop_reason": stop_reason,
         "last_seen_texts": last_seen_texts, "attempt_trace": attempts},
    )


@action_info(
    name="resonance_pc.intercity_depart_and_wait",
    public=True,
    read_only=False,
    description="Enter intercity view, select destination, handle fatigue recovery, depart and wait for arrival.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
)
def resonance_pc_intercity_depart_and_wait(
    to_city_name: str,
    enter_station_timeout_seconds: float = 1800.0,
    location_file_path: str = "data/meta/location_pc.json",
    city_search_region: Optional[List[int]] = None,
    drag_center: Optional[List[int]] = None,
    drag_span_px: int = 900,
    max_search_steps: int = 12,
    fallback_enabled: bool = True,
    target_match_mode: str = "contains",
    click_y_offset: int = -15,
    drag_duration_sec: float = 1.0,
    drag_hold_sec: float = 0.5,
    use_fatigue_medicine: bool = False,
    allowed_fatigue_medicines: Optional[List[str]] = None,
    fatigue_medicine_max_uses: int = 4,
    medicine_button_threshold: float = 0.95,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    from_city_name: Optional[str] = None,
    auto_pickup: bool = False,
    auto_pickup_interval_sec: float = 0.5,
    use_trailer: bool = False,
    trailer_daily_first_n: int = 1,
    trailer_payment: str = "gold",
) -> Dict[str, Any]:
    _check_intercity_cancelled()
    validate_trailer_options(use_trailer, trailer_daily_first_n, trailer_payment)
    if app is None or ocr is None or vision is None:
        raise RuntimeError("app/ocr/vision services are required for intercity_depart_and_wait.")

    # Reject unknown starts or an unsupported viewport before opening the map.
    _native_navigation_context(app, to_city_name, from_city_name)

    pickup_interval = _normalize_pickup_interval(auto_pickup_interval_sec)
    allowed_names = _normalize_allowed_fatigue_medicines(allowed_fatigue_medicines)
    if auto_pickup and not Path(_resolve_plan_template_path(_TRAVEL_HORN_TEMPLATE)).is_file():
        _raise_error(code="pickup_template_missing", message="Travel horn template is missing.")
    medicine_limit = max(int(fatigue_medicine_max_uses), 0)
    medicine_usage: Dict[str, int] = {}
    selected: Optional[Dict[str, Any]] = None
    ineffective_medicines: set[str] = set()
    departure_attempts = 0
    trailer_enabled = use_trailer
    trailer_attempts: List[Dict[str, Any]] = []

    while True:
        _check_intercity_cancelled()
        observe_operation("travel.destination", "进入地图并选择目的地", "started", to_city_name=to_city_name)
        _open_intercity_map(app, vision)

        selected = resonance_pc_select_intercity_destination(
            to_city_name=to_city_name,
            from_city_name=from_city_name,
            location_file_path=location_file_path,
            city_search_region=city_search_region,
            drag_center=drag_center,
            drag_span_px=drag_span_px,
            max_search_steps=max_search_steps,
            fallback_enabled=fallback_enabled,
            target_match_mode=target_match_mode,
            click_y_offset=click_y_offset,
            drag_duration_sec=drag_duration_sec,
            drag_hold_sec=drag_hold_sec,
            app=app,
            ocr=ocr,
        )

        observe_operation("travel.destination", "进入地图并选择目的地", "completed",
                          to_city_name=to_city_name, mode=selected.get("mode"))
        observe_operation("travel.departure_confirming", "前往目的地并等待出发判定", "started")

        gate = None
        if trailer_enabled:
            # Import locally: the standalone operation reuses navigation observation constants.
            from .trailer_service_pc_actions import resonance_pc_use_trailer_service_from_map
            trailer = resonance_pc_use_trailer_service_from_map(
                daily_first_n=trailer_daily_first_n, payment=trailer_payment,
                app=app, ocr=ocr, vision=vision,
            )
            trailer_attempts.append(trailer)
            if verified_quota_skip(trailer):
                trailer_enabled = False
                logger.info("[IntercityDeparture] towing quota ended; ordinary departure reason=%s", trailer["reason"])
            elif (trailer.get("success") is True and trailer.get("status") == "departed"
                  and trailer.get("submitted") is True and trailer.get("departed") is True
                  and trailer.get("page_state") in {"traveling", "arrival_ready"}):
                gate = {"state": "trailer_departed"}
            elif (trailer.get("status") == "blocked" and trailer.get("reason") == "fatigue_recovery_required"
                  and trailer.get("submitted") is True and trailer.get("departed") is False
                  and trailer.get("page_state") == "fatigue_panel"):
                # A confirmed fatigue panel does not prove that payment was rejected.
                # Leave this submitted attempt for the user instead of spending again.
                return _blocked_departure_result(
                    reason="trailer_submission_requires_review", selected=selected,
                    to_city_name=to_city_name, medicine_usage=medicine_usage, medicine_limit=medicine_limit,
                    extra={"departure_attempts": departure_attempts + 1, "page_state": "fatigue_panel",
                           "departure_gate": {"state": "fatigue_panel", "trailer_service": trailer,
                                              "trailer_attempts": trailer_attempts}},
                )
            else:
                return _blocked_departure_result(
                    reason=str(trailer.get("reason") or "trailer_departure_unconfirmed"), selected=selected,
                    to_city_name=to_city_name, medicine_usage=medicine_usage, medicine_limit=medicine_limit,
                    extra={"departure_attempts": departure_attempts,
                           "page_state": trailer.get("page_state") or "unknown",
                           "departure_gate": {"state": "trailer_blocked", "trailer_service": trailer,
                                              "trailer_attempts": trailer_attempts}},
                )

        if gate is None:
            go_result = _wait_and_click_go_destination(
                app=app, vision=vision, ocr=ocr, template_timeout_sec=3.0, interval_sec=0.5,
            )
            if not go_result.get("clicked"):
                _raise_error(
                    code="go_destination_button_not_found",
                    message="Unable to click 前往目的地 after selecting destination.",
                    detail={"to_city_name": to_city_name, "selected": selected, "go_destination": go_result},
                )
            gate = _wait_departure_gate(app=app, ocr=ocr, vision=vision, timeout_sec=8.0, interval_sec=0.5)
        departure_attempts += 1
        if trailer_attempts:
            gate.update(trailer_service=trailer_attempts[-1], trailer_attempts=list(trailer_attempts))
        gate_state = str(gate.get("state") or "")
        observe_operation("travel.departure_confirming", "出发判定结果", "progress",
                          gate_state=gate_state, departure_attempts=departure_attempts,
                          confirmed=gate_state == "trailer_departed")
        if gate_state in {"confirm_clicked", "assume_traveling", "trailer_departed"}:
            observe_operation("travel.wait_arrival",
                              "拖车出发已确认，等待到站" if gate_state == "trailer_departed" else "等待到站（出发未单独核实）",
                              "started", gate_state=gate_state, departure_confirmed=gate_state == "trailer_departed")
            arrival = resonance_pc_wait_intercity_arrival(
                timeout_sec=enter_station_timeout_seconds,
                interval_sec=3.0,
                app=app,
                ocr=ocr,
                vision=vision,
                **({"auto_pickup": True, "auto_pickup_interval_sec": pickup_interval}
                   if auto_pickup and gate_state != "trailer_departed" else {}),
            )
            observe_operation("travel.wait_arrival", "到站等待返回", "completed",
                              arrival_status=arrival.get("status"),
                              arrival_confirmed=arrival.get("success") is True,
                              arrival_mode=arrival.get("arrival_mode"))
            return {
                "success": True,
                "status": "ok",
                "reason": None,
                "to_city_name": selected.get("to_city_name"),
                "to_city_key": selected.get("to_city_key"),
                "selected_point": selected.get("selected_point"),
                "mode": selected.get("mode"),
                "attempts_used": selected.get("attempts_used"),
                "departure_attempts": departure_attempts,
                "departure_gate": gate,
                "arrival_status": arrival.get("status"),
                "encounter_actions": int(arrival.get("encounter_actions") or 0),
                "arrival": arrival,
                "fatigue_medicine_used": _merge_medicine_usage(medicine_usage),
                "fatigue_medicine_use_count": sum(int(v) for v in medicine_usage.values()),
                "fatigue_medicine_limit": medicine_limit,
                **({"trailer_service": trailer_attempts[-1], "trailer_attempts": trailer_attempts,
                    "travel_method": "trailer" if gate_state == "trailer_departed" else "normal"}
                   if trailer_attempts else {}),
            }

        if gate_state != "fatigue_panel":
            _raise_error(
                code="departure_gate_unknown",
                message="Unable to classify intercity departure state.",
                detail={"to_city_name": to_city_name, "selected": selected, "gate": gate},
            )

        observe_operation("travel.fatigue_recovery", "处理疲劳恢复面板", "started")
        if not bool(use_fatigue_medicine):
            back = _click_fatigue_back(app=app, vision=vision, threshold=medicine_button_threshold)
            _sleep_with_cancel(1.0)
            return _blocked_departure_result(
                reason="fatigue_recovery_required",
                selected=selected,
                to_city_name=to_city_name,
                medicine_usage=medicine_usage,
                medicine_limit=medicine_limit,
                back_result=back,
                extra={"departure_attempts": departure_attempts, "departure_gate": gate},
            )

        if not allowed_names:
            back = _click_fatigue_back(app=app, vision=vision, threshold=medicine_button_threshold)
            _sleep_with_cancel(1.0)
            return _blocked_departure_result(
                reason="fatigue_medicine_not_allowed",
                selected=selected,
                to_city_name=to_city_name,
                medicine_usage=medicine_usage,
                medicine_limit=medicine_limit,
                back_result=back,
                extra={"departure_attempts": departure_attempts, "departure_gate": gate},
            )

        if sum(int(v) for v in medicine_usage.values()) >= medicine_limit:
            back = _click_fatigue_back(app=app, vision=vision, threshold=medicine_button_threshold)
            _sleep_with_cancel(1.0)
            return _blocked_departure_result(
                reason="fatigue_medicine_limit_reached",
                selected=selected,
                to_city_name=to_city_name,
                medicine_usage=medicine_usage,
                medicine_limit=medicine_limit,
                back_result=back,
                extra={"departure_attempts": departure_attempts, "departure_gate": gate},
            )

        medicine = _find_allowed_fatigue_medicine(
            app=app,
            vision=vision,
            allowed_names=allowed_names,
            threshold=medicine_button_threshold,
            excluded_names=ineffective_medicines,
        )
        if medicine is None:
            back = _click_fatigue_back(app=app, vision=vision, threshold=medicine_button_threshold)
            _sleep_with_cancel(1.0)
            return _blocked_departure_result(
                reason="fatigue_medicine_unavailable",
                selected=selected,
                to_city_name=to_city_name,
                medicine_usage=medicine_usage,
                medicine_limit=medicine_limit,
                back_result=back,
                extra={"departure_attempts": departure_attempts, "departure_gate": gate},
            )

        medicine_name = str(medicine["name"])
        match = medicine.get("match") if isinstance(medicine.get("match"), dict) else {}
        if not _click_template_match(app, match):
            ineffective_medicines.add(medicine_name)
            continue
        logger.info("[IntercityDeparture] selected fatigue medicine: %s", medicine_name)

        _sleep_with_cancel(0.5)
        confirm = _wait_and_click_fatigue_medicine_confirm(
            app=app,
            vision=vision,
            ocr=ocr,
            threshold=medicine_button_threshold,
        )
        if not confirm.get("clicked"):
            back = _click_fatigue_back(app=app, vision=vision, threshold=medicine_button_threshold)
            _sleep_with_cancel(1.0)
            return _blocked_departure_result(
                reason="fatigue_medicine_confirm_not_found",
                selected=selected,
                to_city_name=to_city_name,
                medicine_usage=medicine_usage,
                medicine_limit=medicine_limit,
                back_result=back,
                extra={
                    "departure_attempts": departure_attempts,
                    "departure_gate": gate,
                    "fatigue_medicine_name": medicine_name,
                    "fatigue_medicine_confirm": confirm,
                },
            )
        logger.info("[IntercityDeparture] confirmed fatigue medicine: %s", medicine_name)

        # Confirming the default 1x use returns to the city main screen.
        _sleep_with_cancel(1.5)
        try:
            main_ready = wait_depart_button(app=app, vision=vision, check_cancelled=_check_intercity_cancelled,
                                            timeout_sec=5.0)
        except DepartButtonError as exc:
            _raise_error(exc.code, exc.message, exc.detail)
        if not main_ready["confirmed"]:
            return _blocked_departure_result(
                reason="fatigue_medicine_return_to_city_failed",
                selected=selected,
                to_city_name=to_city_name,
                medicine_usage=medicine_usage,
                medicine_limit=medicine_limit,
                extra={
                    "departure_attempts": departure_attempts,
                    "departure_gate": gate,
                    "fatigue_medicine_name": medicine_name,
                    "fatigue_medicine_confirm": confirm,
                },
            )
        medicine_usage[medicine_name] = int(medicine_usage.get(medicine_name) or 0) + 1
        ineffective_medicines.clear()
        observe_operation("travel.fatigue_recovery", "恢复道具使用后已返回城市", "completed",
                          medicine_name=medicine_name, main_confirmed=True,
                          fatigue_medicine_used=_merge_medicine_usage(medicine_usage))


class _TravelPickup:
    """Own one leg's pickup state; no clicks can outlive its arrival loop."""

    def __init__(self, app: Any, vision: Any, *, enabled: bool, interval_sec: float = _AUTO_PICKUP_INTERVAL) -> None:
        self.app = app
        self.vision = vision
        self.enabled = bool(enabled)
        self.interval_sec = _normalize_pickup_interval(interval_sec)
        self.click_count = 0
        self.next_click_at = 0.0
        self.horn_present: Optional[bool] = None
        self.end_reason = "finished"

    def __enter__(self) -> "_TravelPickup":
        if self.enabled:
            if not Path(_resolve_plan_template_path(_TRAVEL_HORN_TEMPLATE)).is_file():
                _raise_error(code="pickup_template_missing", message="Travel horn template is missing.")
            logger.info("[AutoPickup] started point=%s interval=%.2f", _AUTO_PICKUP_POINT, self.interval_sec)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.enabled:
            reason = self.end_reason
            if exc_type is not None and reason == "finished":
                reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            logger.info("[AutoPickup] stopped reason=%s clicks=%s", reason, self.click_count)

    def tick(self, deadline: float) -> None:
        _check_intercity_cancelled()
        now = time.monotonic()
        if now >= deadline or now < self.next_click_at:
            return
        match = _match_template_in_region(
            app=self.app, vision=self.vision, template=_TRAVEL_HORN_TEMPLATE,
            region=_TRAVEL_HORN_REGION, threshold=_TRAVEL_HORN_THRESHOLD,
            use_grayscale=False,
        )
        if match.get("reason") == "capture_failed":
            _raise_error(code="pickup_capture_failed", message="Could not capture travel state for pickup.")
        present = bool(match.get("found"))
        if present != self.horn_present:
            logger.info("[AutoPickup] %s confidence=%.4f", "resumed" if present else "paused", match.get("confidence", 0.0))
            self.horn_present = present
        _check_intercity_cancelled()
        if time.monotonic() >= deadline:
            return
        if present:
            _check_intercity_cancelled()
            self.app.click(x=_AUTO_PICKUP_POINT[0], y=_AUTO_PICKUP_POINT[1])
            self.click_count += 1
        self.next_click_at = time.monotonic() + self.interval_sec

    def wait(self, deadline: float, max_wait_sec: float = 3.0) -> None:
        # A long pickup cooldown must not postpone the next arrival check.
        until = min(deadline, self.next_click_at, time.monotonic() + max_wait_sec)
        while time.monotonic() < until:
            _check_intercity_cancelled()
            _sleep_with_cancel(min(0.05, max(0.0, until - time.monotonic())))


@action_info(
    name="resonance_pc.wait_intercity_arrival",
    public=True,
    read_only=False,
    description="Wait for station or city entry; optionally pick up cargo while the travel horn is visible.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
)
def resonance_pc_wait_intercity_arrival(
    timeout_sec: float = 1800.0,
    interval_sec: float = 3.0,
    arrival_template: str = _ARRIVAL_BUTTON_TEMPLATE,
    arrival_template_region: Optional[List[int]] = None,
    arrival_template_threshold: float = 0.85,
    arrival_click_max_attempts: int = 5,
    arrival_click_verify_interval_sec: float = 0.8,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    auto_pickup: bool = False,
    auto_pickup_interval_sec: float = 0.5,
) -> Dict[str, Any]:
    """Wait for station or city-entry templates; retain ocr for existing callers."""
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required for wait_intercity_arrival.")

    with _TravelPickup(app, vision, enabled=auto_pickup, interval_sec=auto_pickup_interval_sec) as pickup:
        return _wait_intercity_arrival_loop(
            timeout_sec, interval_sec, arrival_template, arrival_template_region,
            arrival_template_threshold, arrival_click_max_attempts,
            arrival_click_verify_interval_sec, app, vision, pickup,
        )


def _wait_intercity_arrival_loop(
    timeout_sec: float, interval_sec: float, arrival_template: str,
    arrival_template_region: Optional[List[int]], arrival_template_threshold: float,
    arrival_click_max_attempts: int, arrival_click_verify_interval_sec: float,
    app: Any, vision: Any, pickup: "_TravelPickup",
) -> Dict[str, Any]:

    arrival_region = _coerce_region(arrival_template_region, _ARRIVAL_BUTTON_REGION)
    raw_timeout = float(timeout_sec)
    timeout = _DEFAULT_ARRIVAL_TIMEOUT_SECONDS if raw_timeout <= 0 else max(raw_timeout, 0.1)
    interval = max(float(interval_sec), 0.1)
    started = time.monotonic()
    deadline = started + timeout
    poll_count = 0
    trace: List[Dict[str, Any]] = []
    last_station: Dict[str, Any] = {}
    last_city_main: Dict[str, Any] = {}

    while time.monotonic() <= deadline:
        _check_intercity_cancelled()
        poll_count += 1
        station = _click_arrival_template_until_absent(
            app=app,
            vision=vision,
            poll_count=poll_count,
            template=str(arrival_template or _ARRIVAL_BUTTON_TEMPLATE),
            region=arrival_region,
            threshold=float(arrival_template_threshold),
            max_click_attempts=arrival_click_max_attempts,
            verify_interval_sec=arrival_click_verify_interval_sec,
        )
        last_station = dict(station)
        if station.get("confirmed"):
            pickup.end_reason = "station_button"
            record = {
                "poll": poll_count,
                "state": "arrived",
                "arrival_mode": "station_button_clicked",
                "click_attempts": station["click_attempts"],
                "point": station["arrival_point"],
            }
            trace.append(record)
            logger.info(
                "[IntercityArrival] arrived mode=station_button_clicked poll=%s click_attempts=%s elapsed_sec=%.3f",
                poll_count,
                station["click_attempts"],
                time.monotonic() - started,
            )
            return {
                "success": True,
                "status": "arrived",
                "arrival_mode": "station_button_clicked",
                "poll_count": poll_count,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "arrival_point": station["arrival_point"],
                "arrival_click_attempts": station["click_attempts"],
                "station_template": station,
                "pickup_click_count": pickup.click_count,
                "trace": trace[-20:],
            }

        city_main = _detect_city_main_entry(app=app, vision=vision, poll_count=poll_count)
        last_city_main = dict(city_main)
        if city_main.get("found"):
            pickup.end_reason = "city_main"
            record = {
                "poll": poll_count,
                "state": "arrived",
                "arrival_mode": "city_main_detected",
                "city_main": city_main,
                "pickup_click_count": pickup.click_count,
            }
            trace.append(record)
            logger.info(
                "[IntercityArrival] arrived mode=city_main_detected poll=%s elapsed_sec=%.3f hit=%s",
                poll_count,
                time.monotonic() - started,
                city_main.get("hit"),
            )
            return {
                "success": True,
                "status": "arrived",
                "arrival_mode": "city_main_detected",
                "poll_count": poll_count,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "arrival_point": None,
                "arrival_click_attempts": 0,
                "station_template": station,
                "city_main": city_main,
                "pickup_click_count": pickup.click_count,
                "trace": trace[-20:],
            }

        record = {
            "poll": poll_count,
            "state": "traveling",
            "station_confidence": float(
                (station.get("initial_match") or {}).get("confidence") or 0.0
            ),
            "city_main_found": bool(city_main.get("found")),
        }
        trace.append(record)
        trace = trace[-20:]

        _check_intercity_cancelled()
        if pickup.enabled:
            pickup.tick(deadline)
            pickup.wait(deadline, max_wait_sec=interval)
        else:
            _sleep_with_cancel(interval)

    pickup.end_reason = "timeout"
    _raise_error(
        code="arrival_timeout",
        message=(
            f"The intercity station button was not confirmed within {timeout:.1f}s."
        ),
        detail={
            "poll_count": poll_count,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "timeout_sec": timeout,
            "last_station_template": last_station,
            "last_city_main": last_city_main,
            "trace": trace[-20:],
        },
    )
