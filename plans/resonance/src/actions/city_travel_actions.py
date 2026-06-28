"""Actions for Resonance intercity destination selection."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services


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


@action_info(
    name="resonance.select_intercity_destination",
    public=True,
    read_only=False,
    description="Select destination city in intercity view via OCR + directional drag.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    controller="plans/aura_base/controller",
)
def resonance_select_intercity_destination(
    to_city_name: str,
    location_file_path: str = "data/meta/location.json",
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

        hit = _find_target_hit(observed=observed, target_alias_norms=target_alias_norms, match_mode=target_match_mode)
        if hit is not None:
            click_x = int(hit["center"][0])
            click_y = int(hit["center"][1] + int(click_y_offset))
            click_x, click_y = _clamp_point((click_x, click_y), width, height)
            app.click(x=click_x, y=click_y)
            selected_point = (click_x, click_y)
            selected_mode = "direct" if step == 0 else (selected_mode or "directional")
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
            if not bool(fallback_enabled):
                break
            start, end, plan_debug = _plan_fallback_drag(
                drag_center=center,
                drag_span_px=span,
                step_index=step,
                window_size=(width, height),
            )
            mode = "fallback"

        selected_mode = mode
        attempts.append(
            {
                "step": step + 1,
                "mode": mode,
                "start": {"x": int(start[0]), "y": int(start[1])},
                "end": {"x": int(end[0]), "y": int(end[1])},
                "observed_city_count": len(mappable_points),
                "observed_text_count": len(observed),
                "plan": plan_debug,
            }
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
