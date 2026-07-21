"""Actions for ResonancePc intercity destination selection."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger


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

_DEFAULT_CITY_SEARCH_REGION = [120, 80, 1100, 600]  # x,y,w,h
_DEFAULT_DRAG_CENTER = [640, 360]  # x,y
_DEFAULT_FULL_SCREEN_REGION = [0, 0, 1280, 720]

_DEPART_MARKERS = ("启程",)
_DEPART_CONFIRM_MARKERS = ("立即出发",)

_GO_DESTINATION_TEMPLATE = "templates/go_destination_button.png"
_ARRIVAL_BUTTON_TEMPLATE = "templates/enter_station_button.png"
_FATIGUE_PANEL_TEMPLATE = "templates/fatigue_recovery_panel_title.png"
_FATIGUE_BACK_TEMPLATE = "templates/fatigue_recovery_back_button.png"
_FATIGUE_MEDICINE_CONFIRM_TEMPLATE = "templates/fatigue_medicine_confirm_button.png"

_GO_DESTINATION_REGION = [900, 580, 340, 120]
_ARRIVAL_BUTTON_REGION = [780, 325, 240, 70]
_DEPART_CONFIRM_REGION = [450, 360, 760, 220]
_FATIGUE_PANEL_REGION = [70, 80, 520, 190]
_FATIGUE_BACK_REGION = [0, 0, 260, 90]
_FATIGUE_MEDICINE_CONFIRM_REGION = [620, 520, 660, 120]

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
    "gronru_city": "格罗努城",
    "cape_city": "海角城",
    "confluence_tower": "汇流塔",
}

_CITY_ALIAS_TO_KEY: Dict[str, str] = {
    "阿妮塔能源研究所": "anita_energy_research_institute",
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
    "格罗努城": "gronru_city",
    "海角城": "cape_city",
    "汇流塔": "confluence_tower",
    "云岫桥基地": "confluence_tower",
    "沃德镇": "confluence_tower",
}


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    raise IntercityDestinationError(code=code, message=message, detail=detail)


def _normalize_text(text: Any) -> str:
    normalized = re.sub(r"[\s\u3000\|:：,，。!?！？()（）\[\]【】<>《》\"'`~\-]+", "", str(text))
    return normalized.strip().lower()


def _resolve_location_file_path(location_file_path: str) -> Path:
    raw_path = Path(str(location_file_path or "").strip())
    if raw_path.is_absolute():
        return raw_path
    if raw_path.is_file():
        return raw_path.resolve()
    return (_PLAN_ROOT / raw_path).resolve()


def _load_location_city_table(location_file_path: str) -> Dict[str, Any]:
    file_path = _resolve_location_file_path(location_file_path)
    if not file_path.is_file():
        _raise_error(
            code="location_file_not_found",
            message=f"Location file not found: {file_path}",
            detail={"location_file_path": location_file_path, "resolved": str(file_path)},
        )
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _raise_error(
            code="location_json_invalid",
            message="location.json is not valid JSON.",
            detail={"location_file_path": str(file_path), "cause": str(exc)},
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("city"), dict):
        _raise_error(
            code="location_json_invalid",
            message="location.json must include object field 'city'.",
            detail={"location_file_path": str(file_path)},
        )
    return payload["city"]


def _extract_maploc(city_table: Dict[str, Any], city_key: str) -> Tuple[int, int]:
    city_data = city_table.get(city_key)
    if not isinstance(city_data, dict):
        _raise_error(
            code="city_not_found_in_location",
            message=f"City '{city_key}' not found in location.json.",
            detail={"city_key": city_key},
        )
    maploc = city_data.get("maploc")
    if not isinstance(maploc, list) or len(maploc) != 2:
        _raise_error(
            code="maploc_missing_or_invalid",
            message=f"City '{city_key}' does not have a valid maploc [x, y].",
            detail={"city_key": city_key, "maploc": maploc},
        )
    try:
        return int(maploc[0]), int(maploc[1])
    except (TypeError, ValueError):
        _raise_error(
            code="maploc_missing_or_invalid",
            message=f"City '{city_key}' maploc must be numeric [x, y].",
            detail={"city_key": city_key, "maploc": maploc},
        )
    return (0, 0)


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
        abs_x = int(city_search_region[0] + int(center[0]))
        abs_y = int(city_search_region[1] + int(center[1]))
        observed.append(
            {
                "text": text,
                "norm_text": _normalize_text(text),
                "center": [abs_x, abs_y],
                "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
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
    capture_started = time.monotonic()
    if diagnostic_label:
        logger.info(
            "[%s] poll=%s phase=capture_start region=%s",
            diagnostic_label,
            diagnostic_poll,
            region,
        )
    capture = app.capture(rect=tuple(region))
    if diagnostic_label:
        logger.info(
            "[%s] poll=%s phase=capture_done elapsed_ms=%s success=%s image_shape=%s",
            diagnostic_label,
            diagnostic_poll,
            round((time.monotonic() - capture_started) * 1000.0, 1),
            bool(capture.success),
            list(getattr(capture.image, "shape", ()) or ()),
        )
    if not capture.success:
        _raise_error(
            code="capture_failed",
            message="Failed to capture screen region.",
            detail={"region": region},
        )

    ocr_started = time.monotonic()
    if diagnostic_label:
        logger.info(
            "[%s] poll=%s phase=ocr_start",
            diagnostic_label,
            diagnostic_poll,
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
        logger.info(
            "[%s] poll=%s phase=ocr_result elapsed_ms=%s count=%s observed=%s",
            diagnostic_label,
            diagnostic_poll,
            round((time.monotonic() - ocr_started) * 1000.0, 1),
            len(observed),
            json.dumps(observed, ensure_ascii=False, separators=(",", ":")),
        )
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


def _build_mappable_city_points(
    observed: List[Dict[str, Any]],
    alias_lookup: Dict[str, str],
    city_table: Dict[str, Any],
) -> List[Dict[str, Any]]:
    by_city: Dict[str, Dict[str, Any]] = {}
    for item in observed:
        city_key = _resolve_city_key_from_ocr_norm(item.get("norm_text", ""), alias_lookup)
        if not city_key:
            continue
        try:
            map_x, map_y = _extract_maploc(city_table, city_key)
        except IntercityDestinationError:
            continue
        current = by_city.get(city_key)
        if current is None or item["confidence"] > current["confidence"]:
            by_city[city_key] = {
                "city_key": city_key,
                "screen_x": int(item["center"][0]),
                "screen_y": int(item["center"][1]),
                "map_x": map_x,
                "map_y": map_y,
                "confidence": float(item["confidence"]),
                "text": item["text"],
            }
    return list(by_city.values())


def _median(values: Iterable[int]) -> int:
    arr = sorted(int(v) for v in values)
    if not arr:
        return 0
    n = len(arr)
    mid = n // 2
    if n % 2 == 1:
        return arr[mid]
    return int(round((arr[mid - 1] + arr[mid]) / 2))


def _clamp_point(point: Tuple[int, int], width: int, height: int) -> Tuple[int, int]:
    x = min(max(int(point[0]), 0), max(width - 1, 0))
    y = min(max(int(point[1]), 0), max(height - 1, 0))
    return x, y


def _plan_directional_drag(
    mappable_points: List[Dict[str, Any]],
    target_maploc: Tuple[int, int],
    drag_center: List[int],
    drag_span_px: int,
    window_size: Tuple[int, int],
) -> Tuple[Tuple[int, int], Tuple[int, int], Dict[str, Any]]:
    tx = _median([p["screen_x"] - p["map_x"] for p in mappable_points])
    ty = _median([p["screen_y"] - p["map_y"] for p in mappable_points])

    predicted_x = int(target_maploc[0] + tx)
    predicted_y = int(target_maploc[1] + ty)
    rel_x = int(predicted_x - drag_center[0])
    rel_y = int(predicted_y - drag_center[1])

    max_abs = max(abs(rel_x), abs(rel_y), 1)
    span = max(int(drag_span_px), 60)
    scale = float(span) / float(max_abs)
    dir_x = int(round(rel_x * scale))
    dir_y = int(round(-rel_y * scale))  # keep old behavior on y axis

    start = (int(drag_center[0] + dir_x / 2), int(drag_center[1] - dir_y / 2))
    end = (int(drag_center[0] - dir_x / 2), int(drag_center[1] + dir_y / 2))
    start = _clamp_point(start, window_size[0], window_size[1])
    end = _clamp_point(end, window_size[0], window_size[1])

    return start, end, {
        "translation_estimate": {"x": tx, "y": ty},
        "predicted_target_screen": {"x": predicted_x, "y": predicted_y},
        "relative_to_center": {"x": rel_x, "y": rel_y},
        "drag_vector": {"x": dir_x, "y": dir_y},
    }


def _plan_fallback_drag(
    drag_center: List[int],
    drag_span_px: int,
    step_index: int,
    window_size: Tuple[int, int],
) -> Tuple[Tuple[int, int], Tuple[int, int], Dict[str, Any]]:
    half = max(int(drag_span_px // 2), 30)
    phase = (int(step_index) // 3) % 4
    cx, cy = int(drag_center[0]), int(drag_center[1])
    # Keep same pattern as old script: down -> left -> up -> right
    if phase == 0:
        start, end, direction = (cx, cy - half), (cx, cy + half), "down"
    elif phase == 1:
        start, end, direction = (cx + half, cy), (cx - half, cy), "left"
    elif phase == 2:
        start, end, direction = (cx, cy + half), (cx, cy - half), "up"
    else:
        start, end, direction = (cx - half, cy), (cx + half, cy), "right"
    start = _clamp_point(start, window_size[0], window_size[1])
    end = _clamp_point(end, window_size[0], window_size[1])
    return start, end, {"fallback_phase": phase, "fallback_direction": direction}


def _perform_drag_with_hold(
    app: Any,
    controller: Any,
    start: Tuple[int, int],
    end: Tuple[int, int],
    drag_duration_sec: float,
    drag_hold_sec: float,
) -> None:
    app.move_to(x=int(start[0]), y=int(start[1]), duration=0.1)
    pressed = False
    try:
        controller.mouse_down("left")
        pressed = True
        app.move_to(x=int(end[0]), y=int(end[1]), duration=max(float(drag_duration_sec), 0.01))
        hold = max(float(drag_hold_sec), 0.0)
        if hold > 0:
            time.sleep(hold)
    finally:
        if pressed:
            controller.mouse_up("left")


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
) -> Optional[Dict[str, Any]]:
    match = _match_template_in_region(
        app=app,
        vision=vision,
        template=template,
        region=region,
        threshold=threshold,
    )
    logger.info(
        "[IntercityArrivalTemplate] poll=%s phase=detect found=%s confidence=%.4f center=%s region=%s",
        poll_count,
        bool(match.get("found")),
        float(match.get("confidence") or 0.0),
        match.get("center"),
        region,
    )
    if not match.get("found"):
        return None

    click_limit = max(int(max_click_attempts), 1)
    verify_interval = max(float(verify_interval_sec), 0.0)
    clicks: List[Dict[str, Any]] = []

    for attempt in range(1, click_limit + 1):
        center = match.get("center")
        if not isinstance(center, list) or len(center) != 2:
            _raise_error(
                code="arrival_template_center_missing",
                message="Arrival template matched without a usable center point.",
                detail={"poll_count": poll_count, "attempt": attempt, "match": match},
            )

        x, y = int(center[0]), int(center[1])
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
            time.sleep(verify_interval)
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
            return {
                "click_attempts": attempt,
                "arrival_point": {"x": x, "y": y},
                "clicks": clicks,
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


def _wait_for_marker_hit(
    app: Any,
    ocr: Any,
    markers: Tuple[str, ...],
    *,
    timeout_sec: float,
    interval_sec: float,
    region: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    screen_region = _coerce_region(region, _DEFAULT_FULL_SCREEN_REGION)
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    interval = max(float(interval_sec), 0.1)
    while time.monotonic() <= deadline:
        items = _capture_and_ocr_text_items(app=app, ocr=ocr, region=screen_region)
        hit = _find_marker_hit(items, markers)
        if hit is not None:
            return hit
        time.sleep(interval)
    return None


def _click_marker_hit(app: Any, hit: Dict[str, Any]) -> bool:
    center = hit.get("center")
    if not isinstance(center, list) or len(center) != 2:
        return False
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
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    interval = max(float(interval_sec), 0.1)
    last_match: Dict[str, Any] = {"found": False}
    while time.monotonic() <= deadline:
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
        time.sleep(interval)

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
    return payload


def _wait_and_click_go_destination(
    app: Any,
    vision: Any,
    ocr: Any,
    *,
    template_timeout_sec: float,
    interval_sec: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(template_timeout_sec), 0.1)
    template_match: Dict[str, Any] = {"found": False}
    while time.monotonic() <= deadline:
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
        time.sleep(max(float(interval_sec), 0.1))

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
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    interval = max(float(interval_sec), 0.1)
    last_panel_match: Dict[str, Any] = {"found": False}
    last_confirm_hit: Optional[Dict[str, Any]] = None
    while time.monotonic() <= deadline:
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
            app.click(x=int(_WEEKLY_NOTICE_CHECKBOX[0]), y=int(_WEEKLY_NOTICE_CHECKBOX[1]))
            time.sleep(0.2)
            app.click(x=int(_DEPART_CONFIRM_POINT[0]), y=int(_DEPART_CONFIRM_POINT[1]))
            return {"state": "confirm_clicked", "confirm_hit": confirm_hit}
        time.sleep(interval)
    return {
        "state": "assume_traveling",
        "panel_match": last_panel_match,
        "confirm_hit": last_confirm_hit,
    }


@action_info(
    name="resonance_pc.select_intercity_destination",
    public=True,
    read_only=False,
    description="Select destination city in intercity view via OCR + directional drag.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    controller="plans/aura_base/controller",
)
def resonance_pc_select_intercity_destination(
    to_city_name: str,
    location_file_path: str = "data/meta/location_pc.json",
    city_search_region: Optional[List[int]] = None,
    drag_center: Optional[List[int]] = None,
    drag_span_px: int = 600,
    max_search_steps: int = 12,
    fallback_enabled: bool = True,
    target_match_mode: str = "contains",
    click_y_offset: int = -15,
    drag_duration_sec: float = 1.0,
    drag_hold_sec: float = 0.5,
    app: Any = None,
    ocr: Any = None,
    controller: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or controller is None:
        raise RuntimeError("app/ocr/controller services are required for select_intercity_destination.")

    region = _coerce_region(city_search_region, _DEFAULT_CITY_SEARCH_REGION)
    center = _coerce_point(drag_center, _DEFAULT_DRAG_CENTER)
    max_steps = max(int(max_search_steps), 1)
    span = max(int(drag_span_px), 60)

    city_table = _load_location_city_table(location_file_path)
    alias_lookup = _build_alias_lookup(city_table)
    target_city_key = _resolve_city_key_from_name(to_city_name, city_table, alias_lookup)
    target_alias_norms = _build_target_alias_set(target_city_key)
    target_maploc = _extract_maploc(city_table, target_city_key)

    win_size = app.get_window_size() or (1280, 720)
    if not isinstance(win_size, tuple) or len(win_size) != 2:
        win_size = (1280, 720)
    width = max(int(win_size[0]), 1)
    height = max(int(win_size[1]), 1)

    attempts: List[Dict[str, Any]] = []
    last_seen_texts: List[str] = []
    selected_point: Optional[Tuple[int, int]] = None
    selected_mode: Optional[str] = None

    for step in range(max_steps):
        observed = _capture_and_ocr_city_labels(app=app, ocr=ocr, city_search_region=region)
        last_seen_texts = [str(item.get("text", "")) for item in observed[:20]]
        observed_log = [
            {
                "text": str(item.get("text", "")),
                "norm_text": str(item.get("norm_text", "")),
                "center": item.get("center"),
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
            for item in observed[:20]
        ]
        logger.info(
            "[IntercitySelectOCR] step=%s target=%s target_key=%s observed=%s",
            step + 1,
            to_city_name,
            target_city_key,
            json.dumps(observed_log, ensure_ascii=False),
        )

        hit = _find_target_hit(observed=observed, target_alias_norms=target_alias_norms, match_mode=target_match_mode)
        if hit is not None:
            click_x = int(hit["center"][0])
            click_y = int(hit["center"][1] + int(click_y_offset))
            click_x, click_y = _clamp_point((click_x, click_y), width, height)
            app.click(x=click_x, y=click_y)
            selected_point = (click_x, click_y)
            selected_mode = "direct" if step == 0 else (selected_mode or "directional")
            logger.info(
                "[IntercitySelectHit] step=%s target=%s hit_text=%s click=(%s,%s)",
                step + 1,
                to_city_name,
                hit.get("text"),
                click_x,
                click_y,
            )
            return {
                "success": True,
                "to_city_key": target_city_key,
                "to_city_name": _CITY_KEY_DISPLAY_NAME.get(target_city_key, target_city_key),
                "selected_point": {"x": click_x, "y": click_y},
                "mode": selected_mode,
                "attempts_used": step + 1,
                "attempt_trace": attempts,
            }

        mappable_points = _build_mappable_city_points(
            observed=observed,
            alias_lookup=alias_lookup,
            city_table=city_table,
        )
        mappable_log = [
            {
                "city_key": str(item.get("city_key", "")),
                "text": str(item.get("text", "")),
                "screen": {"x": int(item.get("screen_x", 0)), "y": int(item.get("screen_y", 0))},
                "map": {"x": int(item.get("map_x", 0)), "y": int(item.get("map_y", 0))},
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
            for item in mappable_points
        ]

        if mappable_points:
            start, end, plan_debug = _plan_directional_drag(
                mappable_points=mappable_points,
                target_maploc=target_maploc,
                drag_center=center,
                drag_span_px=span,
                window_size=(width, height),
            )
            mode = "directional"
        else:
            selected_mode = "no_mappable"
            attempts.append(
                {
                    "step": step + 1,
                    "mode": "no_mappable",
                    "observed_city_count": 0,
                    "observed_text_count": len(observed),
                    "observed": observed_log,
                    "mappable": [],
                    "plan": {"reason": "no_mappable_city_points", "fallback_drag_disabled": True},
                }
            )
            logger.info(
                "[IntercitySelectNoDrag] step=%s target=%s observed=%s",
                step + 1,
                to_city_name,
                json.dumps(observed_log, ensure_ascii=False),
            )
            break

        selected_mode = mode
        attempts.append(
            {
                "step": step + 1,
                "mode": mode,
                "start": {"x": int(start[0]), "y": int(start[1])},
                "end": {"x": int(end[0]), "y": int(end[1])},
                "observed_city_count": len(mappable_points),
                "observed_text_count": len(observed),
                "observed": observed_log,
                "mappable": mappable_log,
                "plan": plan_debug,
            }
        )
        logger.info(
            "[IntercitySelectDrag] step=%s target=%s mode=%s start=%s end=%s mappable=%s plan=%s",
            step + 1,
            to_city_name,
            mode,
            {"x": int(start[0]), "y": int(start[1])},
            {"x": int(end[0]), "y": int(end[1])},
            json.dumps(mappable_log, ensure_ascii=False),
            json.dumps(plan_debug, ensure_ascii=False),
        )
        _perform_drag_with_hold(
            app=app,
            controller=controller,
            start=start,
            end=end,
            drag_duration_sec=drag_duration_sec,
            drag_hold_sec=drag_hold_sec,
        )
        time.sleep(0.2)

    _raise_error(
        code="destination_not_found_after_drag",
        message=f"Unable to locate destination '{to_city_name}' after {max_steps} drag attempts.",
        detail={
            "to_city_name": to_city_name,
            "to_city_key": target_city_key,
            "last_seen_texts": last_seen_texts,
            "attempt_trace": attempts,
            "selected_mode": selected_mode,
            "selected_point": selected_point,
        },
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
    controller="plans/aura_base/controller",
)
def resonance_pc_intercity_depart_and_wait(
    to_city_name: str,
    enter_station_timeout_seconds: float = 0,
    location_file_path: str = "data/meta/location_pc.json",
    city_search_region: Optional[List[int]] = None,
    drag_center: Optional[List[int]] = None,
    drag_span_px: int = 600,
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
    controller: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None or controller is None:
        raise RuntimeError("app/ocr/vision/controller services are required for intercity_depart_and_wait.")

    allowed_names = _normalize_allowed_fatigue_medicines(allowed_fatigue_medicines)
    medicine_limit = max(int(fatigue_medicine_max_uses), 0)
    medicine_usage: Dict[str, int] = {}
    selected: Optional[Dict[str, Any]] = None
    ineffective_medicines: set[str] = set()
    departure_attempts = 0

    while True:
        depart_hit = _wait_for_marker_hit(
            app=app,
            ocr=ocr,
            markers=_DEPART_MARKERS,
            timeout_sec=3.0,
            interval_sec=0.5,
            region=_DEFAULT_FULL_SCREEN_REGION,
        )
        if depart_hit is None:
            _raise_error(
                code="depart_button_not_found",
                message="Unable to find 启程 before intercity departure.",
                detail={"to_city_name": to_city_name, "departure_attempts": departure_attempts},
            )
        _click_marker_hit(app, depart_hit)
        time.sleep(1.0)

        selected = resonance_pc_select_intercity_destination(
            to_city_name=to_city_name,
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
            controller=controller,
        )

        go_result = _wait_and_click_go_destination(
            app=app,
            vision=vision,
            ocr=ocr,
            template_timeout_sec=3.0,
            interval_sec=0.5,
        )
        if not go_result.get("clicked"):
            _raise_error(
                code="go_destination_button_not_found",
                message="Unable to click 前往目的地 after selecting destination.",
                detail={"to_city_name": to_city_name, "selected": selected, "go_destination": go_result},
            )
        departure_attempts += 1

        gate = _wait_departure_gate(
            app=app,
            ocr=ocr,
            vision=vision,
            timeout_sec=8.0,
            interval_sec=0.5,
        )
        gate_state = str(gate.get("state") or "")
        if gate_state in {"confirm_clicked", "assume_traveling"}:
            arrival = resonance_pc_wait_intercity_arrival(
                timeout_sec=enter_station_timeout_seconds,
                interval_sec=3.0,
                app=app,
                vision=vision,
            )
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
            }

        if gate_state != "fatigue_panel":
            _raise_error(
                code="departure_gate_unknown",
                message="Unable to classify intercity departure state.",
                detail={"to_city_name": to_city_name, "selected": selected, "gate": gate},
            )

        if not bool(use_fatigue_medicine):
            back = _click_fatigue_back(app=app, vision=vision, threshold=medicine_button_threshold)
            time.sleep(1.0)
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
            time.sleep(1.0)
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
            time.sleep(1.0)
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
            time.sleep(1.0)
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

        time.sleep(0.5)
        confirm = _wait_and_click_fatigue_medicine_confirm(
            app=app,
            vision=vision,
            ocr=ocr,
            threshold=medicine_button_threshold,
        )
        if not confirm.get("clicked"):
            back = _click_fatigue_back(app=app, vision=vision, threshold=medicine_button_threshold)
            time.sleep(1.0)
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
        time.sleep(1.5)
        main_hit = _wait_for_marker_hit(
            app=app,
            ocr=ocr,
            markers=_DEPART_MARKERS,
            timeout_sec=5.0,
            interval_sec=0.5,
            region=_DEFAULT_FULL_SCREEN_REGION,
        )
        if main_hit is None:
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


@action_info(
    name="resonance_pc.wait_intercity_arrival",
    public=True,
    read_only=False,
    description="Wait for intercity arrival using the enter-station button template.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
)
def resonance_pc_wait_intercity_arrival(
    timeout_sec: float = 600.0,
    interval_sec: float = 3.0,
    arrival_template: str = _ARRIVAL_BUTTON_TEMPLATE,
    arrival_template_region: Optional[List[int]] = None,
    arrival_template_threshold: float = 0.85,
    arrival_click_max_attempts: int = 5,
    arrival_click_verify_interval_sec: float = 0.8,
    app: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    """Poll the enter-station template until arrival; travel encounters resolve in-game."""
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required for wait_intercity_arrival.")

    arrival_region = _coerce_region(arrival_template_region, _ARRIVAL_BUTTON_REGION)
    raw_timeout = float(timeout_sec)
    timeout = None if raw_timeout <= 0 else max(raw_timeout, 0.1)
    interval = max(float(interval_sec), 0.1)
    started = time.monotonic()
    deadline = None if timeout is None else started + timeout
    poll_count = 0
    trace: List[Dict[str, Any]] = []

    while deadline is None or time.monotonic() <= deadline:
        poll_count += 1
        arrival = _click_arrival_template_until_absent(
            app=app,
            vision=vision,
            poll_count=poll_count,
            template=str(arrival_template or _ARRIVAL_BUTTON_TEMPLATE),
            region=arrival_region,
            threshold=float(arrival_template_threshold),
            max_click_attempts=arrival_click_max_attempts,
            verify_interval_sec=arrival_click_verify_interval_sec,
        )
        if arrival is not None:
            trace.append(
                {
                    "poll": poll_count,
                    "action": "click_arrival_template",
                    "click_attempts": arrival["click_attempts"],
                    "point": arrival["arrival_point"],
                }
            )
            logger.info(
                "[IntercityArrival] template disappeared after %s click attempt(s); arrival confirmed",
                arrival["click_attempts"],
            )
            return {
                "success": True,
                "status": "arrived",
                "poll_count": poll_count,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "arrival_point": arrival["arrival_point"],
                "arrival_click_attempts": arrival["click_attempts"],
                "trace": trace[-20:],
            }

        time.sleep(interval)

    _raise_error(
        code="arrival_timeout",
        message=f"Intercity arrival template was not found within {timeout:.1f}s.",
        detail={
            "poll_count": poll_count,
            "trace": trace[-20:],
        },
    )
