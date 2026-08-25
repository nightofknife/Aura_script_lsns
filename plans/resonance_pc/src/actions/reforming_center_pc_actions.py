"""Reforming Center entry and navigation actions for Resonance PC."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

class ReformingCenterNavigationError(RuntimeError):
    """Structured failure raised by Reforming Center navigation actions."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_PROFILE_AVATAR_POINT = (155, 660)
_MAIN_MARKER_REGION = (1000, 420, 280, 120)
_PROFILE_MENU_REGION = (500, 540, 200, 180)
_REFORMING_CENTER_OVERVIEW_REGION = (540, 70, 740, 650)
_OVERVIEW_PRISONER_COUNT_REGION = (780, 0, 250, 80)
_OVERVIEW_PRISONER_ENTRY_POINT = (850, 40)
_PAGE_TITLE_REGION = (0, 0, 520, 100)
_ROSTER_ADMISSION_BUTTON_REGION = (1000, 630, 280, 90)
_ROSTER_PRISONER_COUNT_REGION = (0, 650, 320, 70)
_ADMISSION_AVAILABLE_COUNT_REGION = (0, 650, 300, 70)
_ADMISSION_CENTER_COUNT_REGION = (290, 650, 350, 70)
_ONE_CLICK_SELECT_REGION = (820, 650, 240, 70)
_CONFIRM_ADMISSION_REGION = (1030, 650, 250, 70)
_ADMISSION_SUCCESS_REGION = (250, 0, 790, 200)
_ADMISSION_SUCCESS_DISMISS_POINT = (100, 360)
_BACK_POINT = (82, 37)
_PLAN_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ROOM_LAYOUT_PATH = "data/meta/reforming_center_rooms_pc.json"
_DEFAULT_ROOM_SEARCH_REGION = (40, 90, 1040, 500)
_DEFAULT_ROOM_DRAG_REGION = (420, 170, 520, 360)
_DEFAULT_ROOM_CLICK_REGION = (250, 140, 730, 420)
_DEFAULT_ROOM_TARGET_TITLE_POINT = (520, 300)

_MAIN_MARKERS = ("访问城市", "访问地区")
_PROFILE_MENU_TARGETS = ("整顿中心",)
_OVERVIEW_PRIMARY_MARKERS = ("事项一览",)
_OVERVIEW_SECONDARY_MARKERS = (
    "稳定度",
    "服从度",
    "安全度",
    "卫生度",
    "整顿宿舍",
    "生产车间",
    "原料车间",
)


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    raise ReformingCenterNavigationError(code=code, message=message, detail=detail)


def _normalize_text(text: Any) -> str:
    return re.sub(
        r"[\s\u3000\|:：,，。.!！?？（）()\[\]【】<>《》'\"`~\-]+",
        "",
        str(text or ""),
    ).lower()


def _coerce_region(region: Sequence[int]) -> Tuple[int, int, int, int]:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        _raise_error("invalid_region", "region must be [x, y, width, height]", {"region": region})
    return tuple(int(value) for value in region)


def _capture_text_items(
    app: Any,
    ocr: Any,
    region: Sequence[int],
) -> List[Dict[str, Any]]:
    region_tuple = _coerce_region(region)
    capture = app.capture(rect=region_tuple)
    if not bool(getattr(capture, "success", False)):
        _raise_error(
            "capture_failed",
            "failed to capture a Reforming Center navigation region",
            {"region": list(region_tuple)},
        )

    result = ocr.recognize_all(source_image=capture.image)
    items: List[Dict[str, Any]] = []
    for row in getattr(result, "results", []) or []:
        text = str(getattr(row, "text", "") or "").strip()
        center = getattr(row, "center_point", None)
        if not text or not center or len(center) != 2:
            continue
        items.append(
            {
                "text": text,
                "normalized": _normalize_text(text),
                "confidence": float(getattr(row, "confidence", 0.0) or 0.0),
                "center": [
                    int(region_tuple[0] + int(center[0])),
                    int(region_tuple[1] + int(center[1])),
                ],
            }
        )
    items.sort(key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
    return items


def _find_marker(items: Iterable[Dict[str, Any]], markers: Sequence[str]) -> Optional[Dict[str, Any]]:
    normalized_markers = [(_normalize_text(marker), marker) for marker in markers]
    for item in items:
        normalized = str(item.get("normalized") or "")
        for marker_normalized, marker in normalized_markers:
            if marker_normalized and (marker_normalized in normalized or normalized in marker_normalized):
                hit = dict(item)
                hit["marker"] = marker
                return hit
    return None


def _wait_for_marker(
    app: Any,
    ocr: Any,
    *,
    markers: Sequence[str],
    region: Sequence[int],
    timeout_sec: float,
    interval_sec: float,
) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    while True:
        items = _capture_text_items(app, ocr, region)
        hit = _find_marker(items, markers)
        if hit is not None:
            return hit
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(float(interval_sec), 0.05))


def _click_waited_marker(
    app: Any,
    ocr: Any,
    *,
    markers: Sequence[str],
    region: Sequence[int],
    timeout_sec: float,
    interval_sec: float,
    error_code: str,
    error_message: str,
) -> Dict[str, Any]:
    hit = _wait_for_marker(
        app,
        ocr,
        markers=markers,
        region=region,
        timeout_sec=timeout_sec,
        interval_sec=interval_sec,
    )
    if hit is None:
        _raise_error(
            error_code,
            error_message,
            {"markers": list(markers), "region": list(region)},
        )
    x, y = [int(value) for value in hit["center"]]
    app.click(x=x, y=y)
    return {"clicked": True, "x": x, "y": y, "hit": hit}


def _extract_fraction(items: Sequence[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    pattern = re.compile(r"(\d+)\s*/\s*(\d+)")
    for item in items:
        match = pattern.search(str(item.get("text") or ""))
        if match:
            return int(match.group(1)), int(match.group(2))

    ordered = sorted(
        items,
        key=lambda item: (
            int((item.get("center") or [0, 0])[1]),
            int((item.get("center") or [0, 0])[0]),
        ),
    )
    joined = "".join(str(item.get("text") or "") for item in ordered)
    match = pattern.search(joined)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _read_fraction(
    app: Any,
    ocr: Any,
    *,
    region: Sequence[int],
    label: str,
) -> Dict[str, Any]:
    items = _capture_text_items(app, ocr, region)
    fraction = _extract_fraction(items)
    if fraction is None:
        _raise_error(
            "prisoner_count_not_found",
            f"failed to read {label}",
            {
                "label": label,
                "region": list(region),
                "recognized_texts": [str(item.get("text") or "") for item in items],
            },
        )
    current, capacity = fraction
    return {
        "current": current,
        "capacity": capacity,
        "region": list(region),
        "recognized_texts": [str(item.get("text") or "") for item in items],
    }


def _extract_success_count(items: Sequence[Dict[str, Any]]) -> Optional[int]:
    pattern = re.compile(r"成功办理\s*(\d+)\s*名囚犯入狱")
    for item in items:
        match = pattern.search(str(item.get("text") or ""))
        if match:
            return int(match.group(1))
    joined = "".join(
        str(item.get("text") or "")
        for item in sorted(items, key=lambda item: int((item.get("center") or [0, 0])[0]))
    )
    match = pattern.search(joined)
    return int(match.group(1)) if match else None


def _wait_for_success_count(
    app: Any,
    ocr: Any,
    *,
    timeout_sec: float,
    interval_sec: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last_texts: List[str] = []
    while True:
        items = _capture_text_items(app, ocr, _ADMISSION_SUCCESS_REGION)
        last_texts = [str(item.get("text") or "") for item in items]
        admitted_count = _extract_success_count(items)
        if admitted_count is not None:
            return {
                "found": True,
                "admitted_count": admitted_count,
                "recognized_texts": last_texts,
            }
        if time.monotonic() >= deadline:
            return {
                "found": False,
                "admitted_count": None,
                "recognized_texts": last_texts,
            }
        time.sleep(max(float(interval_sec), 0.05))


def _detect_overview_once(app: Any, ocr: Any) -> Dict[str, Any]:
    items = _capture_text_items(app, ocr, _REFORMING_CENTER_OVERVIEW_REGION)
    primary = _find_marker(items, _OVERVIEW_PRIMARY_MARKERS)
    secondary = _find_marker(items, _OVERVIEW_SECONDARY_MARKERS)
    return {
        "found": primary is not None and secondary is not None,
        "primary": primary,
        "secondary": secondary,
        "recognized_texts": [str(item.get("text") or "") for item in items],
    }


def _wait_for_overview(
    app: Any,
    ocr: Any,
    *,
    timeout_sec: float,
    interval_sec: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last_result: Dict[str, Any] = {
        "found": False,
        "primary": None,
        "secondary": None,
        "recognized_texts": [],
    }
    while True:
        last_result = _detect_overview_once(app, ocr)
        if last_result["found"]:
            return last_result
        if time.monotonic() >= deadline:
            return last_result
        time.sleep(max(float(interval_sec), 0.05))


@action_info(
    name="resonance_pc.enter_reforming_center",
    public=True,
    read_only=False,
    description="Enter the Reforming Center from the game main screen via the profile menu.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_enter_reforming_center(
    main_timeout_sec: float = 5.0,
    menu_timeout_sec: float = 10.0,
    entry_timeout_sec: float = 20.0,
    interval_sec: float = 0.5,
    after_profile_click_sec: float = 0.8,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr services are required")

    initial_overview = _detect_overview_once(app, ocr)
    if initial_overview["found"]:
        return {
            "success": True,
            "status": "already_there",
            "page_state": "reforming_center_overview",
            "overview": initial_overview,
        }

    main_hit = _wait_for_marker(
        app,
        ocr,
        markers=_MAIN_MARKERS,
        region=_MAIN_MARKER_REGION,
        timeout_sec=main_timeout_sec,
        interval_sec=interval_sec,
    )
    if main_hit is None:
        _raise_error(
            "main_screen_not_ready",
            "failed to confirm the game main screen before opening the profile menu",
            {"markers": list(_MAIN_MARKERS), "region": list(_MAIN_MARKER_REGION)},
        )

    app.click(x=_PROFILE_AVATAR_POINT[0], y=_PROFILE_AVATAR_POINT[1])
    time.sleep(max(float(after_profile_click_sec), 0.0))

    menu_hit = _wait_for_marker(
        app,
        ocr,
        markers=_PROFILE_MENU_TARGETS,
        region=_PROFILE_MENU_REGION,
        timeout_sec=menu_timeout_sec,
        interval_sec=interval_sec,
    )
    if menu_hit is None:
        _raise_error(
            "profile_menu_target_not_found",
            "profile menu opened but 整顿中心 could not be located",
            {"markers": list(_PROFILE_MENU_TARGETS), "region": list(_PROFILE_MENU_REGION)},
        )

    menu_x, menu_y = [int(value) for value in menu_hit["center"]]
    app.click(x=menu_x, y=menu_y)
    logger.info(
        "[ReformingCenterEntry] clicked profile=(%s,%s) target=(%s,%s) text=%s",
        _PROFILE_AVATAR_POINT[0],
        _PROFILE_AVATAR_POINT[1],
        menu_x,
        menu_y,
        menu_hit.get("text"),
    )

    overview = _wait_for_overview(
        app,
        ocr,
        timeout_sec=entry_timeout_sec,
        interval_sec=interval_sec,
    )
    if not overview["found"]:
        _raise_error(
            "reforming_center_overview_not_ready",
            "clicked 整顿中心 but the overview screen was not confirmed",
            {
                "primary_markers": list(_OVERVIEW_PRIMARY_MARKERS),
                "secondary_markers": list(_OVERVIEW_SECONDARY_MARKERS),
                "region": list(_REFORMING_CENTER_OVERVIEW_REGION),
                "recognized_texts": overview.get("recognized_texts", []),
            },
        )

    return {
        "success": True,
        "status": "entered",
        "page_state": "reforming_center_overview",
        "main_marker": main_hit,
        "menu_target": menu_hit,
        "overview": overview,
    }


@action_info(
    name="resonance_pc.admit_all_available_prisoners",
    public=True,
    read_only=False,
    description="Admit all available prisoners up to the Reforming Center capacity and return to its overview.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_admit_all_available_prisoners(
    page_timeout_sec: float = 15.0,
    success_timeout_sec: float = 20.0,
    interval_sec: float = 0.5,
    after_click_sec: float = 0.5,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr services are required")

    overview = _detect_overview_once(app, ocr)
    if not overview["found"]:
        _raise_error(
            "reforming_center_overview_required",
            "prisoner admission must start from the Reforming Center overview",
            {"overview": overview},
        )

    initial = _read_fraction(
        app,
        ocr,
        region=_OVERVIEW_PRISONER_COUNT_REGION,
        label="overview prisoner count",
    )
    initial_count = int(initial["current"])
    capacity = int(initial["capacity"])
    if initial_count >= capacity:
        return {
            "success": True,
            "status": "already_full",
            "page_state": "reforming_center_overview",
            "initial_count": initial_count,
            "final_count": initial_count,
            "capacity": capacity,
            "admitted_count": 0,
        }

    app.click(x=_OVERVIEW_PRISONER_ENTRY_POINT[0], y=_OVERVIEW_PRISONER_ENTRY_POINT[1])
    roster_hit = _wait_for_marker(
        app,
        ocr,
        markers=("囚犯名册",),
        region=_PAGE_TITLE_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
    )
    if roster_hit is None:
        _raise_error(
            "prisoner_roster_not_ready",
            "clicked the overview prisoner counter but 囚犯名册 was not confirmed",
            {"region": list(_PAGE_TITLE_REGION)},
        )

    admission_button = _click_waited_marker(
        app,
        ocr,
        markers=("办理囚犯入住",),
        region=_ROSTER_ADMISSION_BUTTON_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        error_code="admission_button_not_found",
        error_message="failed to locate 办理囚犯入住 on the prisoner roster",
    )
    admission_hit = _wait_for_marker(
        app,
        ocr,
        markers=("办理入住",),
        region=_PAGE_TITLE_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
    )
    if admission_hit is None:
        _raise_error(
            "admission_page_not_ready",
            "clicked 办理囚犯入住 but the admission page was not confirmed",
            {"region": list(_PAGE_TITLE_REGION)},
        )

    available = _read_fraction(
        app,
        ocr,
        region=_ADMISSION_AVAILABLE_COUNT_REGION,
        label="unprocessed prisoner count",
    )
    center_count = _read_fraction(
        app,
        ocr,
        region=_ADMISSION_CENTER_COUNT_REGION,
        label="admission page Reforming Center count",
    )
    if int(center_count["current"]) != initial_count or int(center_count["capacity"]) != capacity:
        _raise_error(
            "admission_count_mismatch",
            "admission page prisoner count did not match the overview",
            {"overview": initial, "admission_page": center_count},
        )

    expected_admissions = min(
        max(capacity - initial_count, 0),
        max(int(available["current"]), 0),
    )
    if expected_admissions <= 0:
        app.click(x=_BACK_POINT[0], y=_BACK_POINT[1])
        if _wait_for_marker(
            app,
            ocr,
            markers=("囚犯名册",),
            region=_PAGE_TITLE_REGION,
            timeout_sec=page_timeout_sec,
            interval_sec=interval_sec,
        ) is None:
            _raise_error("prisoner_roster_not_ready", "failed to return to 囚犯名册")
        app.click(x=_BACK_POINT[0], y=_BACK_POINT[1])
        final_overview = _wait_for_overview(
            app,
            ocr,
            timeout_sec=page_timeout_sec,
            interval_sec=interval_sec,
        )
        if not final_overview["found"]:
            _raise_error("reforming_center_overview_not_ready", "failed to return to the Reforming Center overview")
        return {
            "success": True,
            "status": "no_available_prisoners",
            "page_state": "reforming_center_overview",
            "initial_count": initial_count,
            "final_count": initial_count,
            "capacity": capacity,
            "available_count": int(available["current"]),
            "admitted_count": 0,
        }

    select_all = _click_waited_marker(
        app,
        ocr,
        markers=("一键全选",),
        region=_ONE_CLICK_SELECT_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        error_code="select_all_button_not_found",
        error_message="failed to locate the safe 一键全选 admission button",
    )
    time.sleep(max(float(after_click_sec), 0.0))
    confirm = _click_waited_marker(
        app,
        ocr,
        markers=("确认办理",),
        region=_CONFIRM_ADMISSION_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
        error_code="confirm_admission_button_not_found",
        error_message="failed to locate 确认办理 after selecting prisoners",
    )

    success = _wait_for_success_count(
        app,
        ocr,
        timeout_sec=success_timeout_sec,
        interval_sec=interval_sec,
    )
    if not success["found"]:
        _raise_error(
            "admission_success_not_confirmed",
            "确认办理 was clicked but the success document was not confirmed",
            {
                "expected_admissions": expected_admissions,
                "recognized_texts": success.get("recognized_texts", []),
            },
        )

    admitted_count = int(success["admitted_count"])
    app.click(x=_ADMISSION_SUCCESS_DISMISS_POINT[0], y=_ADMISSION_SUCCESS_DISMISS_POINT[1])
    if _wait_for_marker(
        app,
        ocr,
        markers=("囚犯名册",),
        region=_PAGE_TITLE_REGION,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
    ) is None:
        _raise_error(
            "prisoner_roster_not_ready",
            "dismissed the success document but 囚犯名册 was not confirmed",
        )

    roster_count = _read_fraction(
        app,
        ocr,
        region=_ROSTER_PRISONER_COUNT_REGION,
        label="post-admission roster prisoner count",
    )
    app.click(x=_BACK_POINT[0], y=_BACK_POINT[1])
    final_overview = _wait_for_overview(
        app,
        ocr,
        timeout_sec=page_timeout_sec,
        interval_sec=interval_sec,
    )
    if not final_overview["found"]:
        _raise_error(
            "reforming_center_overview_not_ready",
            "clicked roster back but the Reforming Center overview was not confirmed",
        )

    final = _read_fraction(
        app,
        ocr,
        region=_OVERVIEW_PRISONER_COUNT_REGION,
        label="final overview prisoner count",
    )
    final_count = int(final["current"])
    expected_final_count = initial_count + admitted_count
    validation_detail = {
        "initial_count": initial_count,
        "capacity": capacity,
        "available_count": int(available["current"]),
        "expected_admissions": expected_admissions,
        "admitted_count": admitted_count,
        "expected_final_count": expected_final_count,
        "roster_count": roster_count,
        "final_overview_count": final,
    }
    if admitted_count != expected_admissions:
        _raise_error(
            "unexpected_admission_count",
            "the game admitted a different number of prisoners than expected",
            validation_detail,
        )
    if final_count != expected_final_count:
        _raise_error(
            "admission_result_count_mismatch",
            "the final overview prisoner count did not match the success document",
            validation_detail,
        )
    roster_count_matches = int(roster_count["current"]) == expected_final_count
    if not roster_count_matches:
        logger.warning(
            "[ReformingCenterAdmission] roster count OCR disagreed with the confirmed overview: "
            "roster=%s overview=%s expected=%s",
            roster_count,
            final,
            expected_final_count,
        )

    logger.info(
        "[ReformingCenterAdmission] initial=%s capacity=%s available=%s admitted=%s final=%s",
        initial_count,
        capacity,
        int(available["current"]),
        admitted_count,
        final_count,
    )
    return {
        "success": True,
        "status": "admitted",
        "page_state": "reforming_center_overview",
        "initial_count": initial_count,
        "final_count": final_count,
        "capacity": capacity,
        "available_count": int(available["current"]),
        "expected_admissions": expected_admissions,
        "admitted_count": admitted_count,
        "roster_count": roster_count,
        "roster_count_matches": roster_count_matches,
        "final_overview_count": final,
        "entry_click": {
            "x": _OVERVIEW_PRISONER_ENTRY_POINT[0],
            "y": _OVERVIEW_PRISONER_ENTRY_POINT[1],
        },
        "admission_button": admission_button,
        "select_all": select_all,
        "confirm": confirm,
        "success_document": success,
    }


def _resolve_plan_data_path(path: str) -> Path:
    raw_path = str(path or "").strip()
    if not raw_path:
        _raise_error("invalid_room_layout_path", "room layout path must not be empty")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = _PLAN_ROOT / candidate
    return candidate.resolve()


def _load_reforming_center_room_layout(path: str = _DEFAULT_ROOM_LAYOUT_PATH) -> Dict[str, Dict[str, Any]]:
    layout_path = _resolve_plan_data_path(path)
    if not layout_path.is_file():
        _raise_error(
            "room_layout_not_found",
            "Reforming Center room layout file was not found",
            {"path": str(layout_path)},
        )
    try:
        payload = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _raise_error(
            "room_layout_invalid",
            "failed to read Reforming Center room layout",
            {"path": str(layout_path), "error": str(exc)},
        )

    raw_rooms = payload.get("rooms") if isinstance(payload, dict) else None
    if not isinstance(raw_rooms, dict) or not raw_rooms:
        _raise_error(
            "room_layout_invalid",
            "Reforming Center room layout must contain a non-empty rooms mapping",
            {"path": str(layout_path)},
        )

    rooms: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_room in raw_rooms.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_room, dict):
            _raise_error(
                "room_layout_invalid",
                "each Reforming Center room must be an object with a non-empty key",
                {"path": str(layout_path), "room_key": raw_key},
            )
        display_name = str(raw_room.get("display_name") or key).strip()
        aliases = [display_name, key, *(raw_room.get("aliases") or [])]
        map_point = raw_room.get("map_point")
        click_offset = raw_room.get("click_offset", [120, 55])
        if not isinstance(map_point, list) or len(map_point) != 2:
            _raise_error(
                "room_layout_invalid",
                "room map_point must be [x, y]",
                {"path": str(layout_path), "room_key": key, "map_point": map_point},
            )
        if not isinstance(click_offset, list) or len(click_offset) != 2:
            _raise_error(
                "room_layout_invalid",
                "room click_offset must be [x, y]",
                {"path": str(layout_path), "room_key": key, "click_offset": click_offset},
            )
        rooms[key] = {
            **raw_room,
            "key": key,
            "display_name": display_name,
            "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
            "map_point": [int(map_point[0]), int(map_point[1])],
            "click_offset": [int(click_offset[0]), int(click_offset[1])],
        }
    return rooms


def _build_room_alias_lookup(rooms: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for room_key, room in rooms.items():
        for alias in room.get("aliases", []):
            normalized = _normalize_text(alias)
            if normalized:
                lookup[normalized] = room_key
    return lookup


def _resolve_room_key(
    room_name: str,
    rooms: Dict[str, Dict[str, Any]],
    alias_lookup: Dict[str, str],
) -> str:
    normalized = _normalize_text(room_name)
    if normalized in alias_lookup:
        return alias_lookup[normalized]
    _raise_error(
        "unknown_reforming_center_room",
        f"unknown Reforming Center room: {room_name}",
        {
            "room_name": room_name,
            "available_rooms": [room["display_name"] for room in rooms.values()],
        },
    )
    return ""


def _resolve_room_key_from_ocr(normalized_text: str, alias_lookup: Dict[str, str]) -> Optional[str]:
    if not normalized_text:
        return None
    if normalized_text in alias_lookup:
        return alias_lookup[normalized_text]
    for alias in sorted(alias_lookup, key=len, reverse=True):
        if alias and alias in normalized_text:
            return alias_lookup[alias]
    return None


def _find_room_hit(
    items: Sequence[Dict[str, Any]],
    room_key: str,
    alias_lookup: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for item in items:
        resolved = _resolve_room_key_from_ocr(str(item.get("normalized") or ""), alias_lookup)
        if resolved == room_key:
            matches.append(dict(item))
    if not matches:
        return None
    matches.sort(key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
    return matches[0]


def _build_room_anchor_points(
    items: Sequence[Dict[str, Any]],
    rooms: Dict[str, Dict[str, Any]],
    alias_lookup: Dict[str, str],
) -> List[Dict[str, Any]]:
    best_by_room: Dict[str, Dict[str, Any]] = {}
    for item in items:
        room_key = _resolve_room_key_from_ocr(str(item.get("normalized") or ""), alias_lookup)
        if room_key is None:
            continue
        current = best_by_room.get(room_key)
        if current is not None and float(current["confidence"]) >= float(item.get("confidence") or 0.0):
            continue
        room = rooms[room_key]
        best_by_room[room_key] = {
            "room_key": room_key,
            "text": str(item.get("text") or ""),
            "confidence": float(item.get("confidence") or 0.0),
            "screen_point": [int(item["center"][0]), int(item["center"][1])],
            "map_point": [int(room["map_point"][0]), int(room["map_point"][1])],
        }
    return list(best_by_room.values())


def _median_int(values: Sequence[int]) -> int:
    ordered = sorted(int(value) for value in values)
    if not ordered:
        return 0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return int(round((ordered[middle - 1] + ordered[middle]) / 2))


def _estimate_room_translation(anchors: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    if not anchors:
        _raise_error("room_anchor_not_found", "no known Reforming Center room anchors were recognized")
    return (
        _median_int(
            [
                int(anchor["screen_point"][0]) - int(anchor["map_point"][0])
                for anchor in anchors
            ]
        ),
        _median_int(
            [
                int(anchor["screen_point"][1]) - int(anchor["map_point"][1])
                for anchor in anchors
            ]
        ),
    )


def _validate_room_region(
    value: Optional[Sequence[int]],
    fallback: Sequence[int],
    *,
    label: str,
    window_size: Tuple[int, int],
) -> Tuple[int, int, int, int]:
    region = _coerce_region(value if value is not None else fallback)
    x, y, width, height = region
    window_width, window_height = [int(component) for component in window_size]
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        _raise_error(
            "invalid_room_navigation_region",
            f"{label} must have non-negative origin and positive size",
            {label: list(region), "window_size": list(window_size)},
        )
    if x + width > window_width or y + height > window_height:
        _raise_error(
            "invalid_room_navigation_region",
            f"{label} must stay inside the game client area",
            {label: list(region), "window_size": list(window_size)},
        )
    return region


def _point_in_region(
    point: Sequence[int],
    region: Sequence[int],
    *,
    margin: int = 0,
) -> bool:
    x, y = int(point[0]), int(point[1])
    left, top, width, height = [int(value) for value in region]
    inset = max(int(margin), 0)
    return (
        left + inset <= x <= left + width - inset
        and top + inset <= y <= top + height - inset
    )


def _plan_room_drag(
    *,
    predicted_target_title: Sequence[int],
    desired_target_title: Sequence[int],
    drag_region: Sequence[int],
    padding: int = 20,
) -> Dict[str, Any]:
    left, top, width, height = [int(value) for value in drag_region]
    inset = max(int(padding), 0)
    max_dx = max(width - inset * 2, 1)
    max_dy = max(height - inset * 2, 1)
    requested_dx = int(desired_target_title[0]) - int(predicted_target_title[0])
    requested_dy = int(desired_target_title[1]) - int(predicted_target_title[1])
    applied_dx = max(-max_dx, min(max_dx, requested_dx))
    applied_dy = max(-max_dy, min(max_dy, requested_dy))
    if applied_dx == 0 and applied_dy == 0:
        _raise_error(
            "room_drag_not_plannable",
            "target room is not click-ready but no corrective drag could be planned",
            {
                "predicted_target_title": list(predicted_target_title),
                "desired_target_title": list(desired_target_title),
                "drag_region": list(drag_region),
            },
        )

    center_x = left + width // 2
    center_y = top + height // 2
    start = [
        int(round(center_x - applied_dx / 2.0)),
        int(round(center_y - applied_dy / 2.0)),
    ]
    end = [
        int(round(center_x + applied_dx / 2.0)),
        int(round(center_y + applied_dy / 2.0)),
    ]
    if not _point_in_region(start, drag_region, margin=inset) or not _point_in_region(
        end,
        drag_region,
        margin=inset,
    ):
        _raise_error(
            "room_drag_outside_safe_region",
            "planned room-navigation drag escaped the configured safe region",
            {"start": start, "end": end, "drag_region": list(drag_region)},
        )
    return {
        "start": start,
        "end": end,
        "requested_delta": [requested_dx, requested_dy],
        "applied_delta": [applied_dx, applied_dy],
        "drag_region": list(drag_region),
    }


def _perform_room_drag(
    app: Any,
    *,
    start: Sequence[int],
    end: Sequence[int],
    duration_sec: float,
    hold_sec: float,
) -> None:
    app.move_to(x=int(start[0]), y=int(start[1]), duration=0.1)
    app.drag(
        start_x=int(start[0]),
        start_y=int(start[1]),
        end_x=int(end[0]),
        end_y=int(end[1]),
        duration=max(float(duration_sec), 0.05),
        hold_before_release_sec=max(float(hold_sec), 0.0),
    )


@action_info(
    name="resonance_pc.navigate_reforming_center_room",
    public=True,
    read_only=False,
    description="Find a Reforming Center room by OCR, safely pan it into the interaction area, and click it.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_navigate_reforming_center_room(
    room_name: str,
    room_layout_path: str = _DEFAULT_ROOM_LAYOUT_PATH,
    search_region: Optional[List[int]] = None,
    drag_region: Optional[List[int]] = None,
    click_region: Optional[List[int]] = None,
    target_title_point: Optional[List[int]] = None,
    max_search_steps: int = 12,
    max_no_anchor_polls: int = 2,
    drag_duration_sec: float = 0.8,
    drag_hold_sec: float = 0.2,
    settle_sec: float = 0.8,
    after_click_sec: float = 0.5,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr services are required")

    window_size = app.get_window_size() or (1280, 720)
    if not isinstance(window_size, tuple) or len(window_size) != 2:
        window_size = (1280, 720)
    normalized_window_size = (max(int(window_size[0]), 1), max(int(window_size[1]), 1))
    room_search_region = _validate_room_region(
        search_region,
        _DEFAULT_ROOM_SEARCH_REGION,
        label="search_region",
        window_size=normalized_window_size,
    )
    room_drag_region = _validate_room_region(
        drag_region,
        _DEFAULT_ROOM_DRAG_REGION,
        label="drag_region",
        window_size=normalized_window_size,
    )
    room_click_region = _validate_room_region(
        click_region,
        _DEFAULT_ROOM_CLICK_REGION,
        label="click_region",
        window_size=normalized_window_size,
    )
    raw_target_title = target_title_point or list(_DEFAULT_ROOM_TARGET_TITLE_POINT)
    if not isinstance(raw_target_title, (list, tuple)) or len(raw_target_title) != 2:
        _raise_error(
            "invalid_room_target_point",
            "target_title_point must be [x, y]",
            {"target_title_point": raw_target_title},
        )
    desired_title = [int(raw_target_title[0]), int(raw_target_title[1])]
    if not _point_in_region(desired_title, room_click_region, margin=10):
        _raise_error(
            "invalid_room_target_point",
            "target_title_point must be safely inside click_region",
            {
                "target_title_point": desired_title,
                "click_region": list(room_click_region),
            },
        )

    overview = _detect_overview_once(app, ocr)
    if not overview["found"]:
        _raise_error(
            "reforming_center_overview_required",
            "room navigation must start from the Reforming Center overview",
            {"overview": overview},
        )

    rooms = _load_reforming_center_room_layout(room_layout_path)
    alias_lookup = _build_room_alias_lookup(rooms)
    room_key = _resolve_room_key(room_name, rooms, alias_lookup)
    target_room = rooms[room_key]
    target_map_point = [int(value) for value in target_room["map_point"]]
    click_offset = [int(value) for value in target_room["click_offset"]]
    attempts: List[Dict[str, Any]] = []
    last_seen_texts: List[str] = []
    no_anchor_polls = 0

    for step_index in range(max(int(max_search_steps), 1)):
        items = _capture_text_items(app, ocr, room_search_region)
        last_seen_texts = [str(item.get("text") or "") for item in items[:30]]
        target_hit = _find_room_hit(items, room_key, alias_lookup)
        anchors = _build_room_anchor_points(items, rooms, alias_lookup)
        target_title = (
            [int(target_hit["center"][0]), int(target_hit["center"][1])]
            if target_hit is not None
            else None
        )
        click_point = (
            [
                int(target_title[0] + click_offset[0]),
                int(target_title[1] + click_offset[1]),
            ]
            if target_title is not None
            else None
        )
        click_ready = bool(
            target_title is not None
            and click_point is not None
            and _point_in_region(target_title, room_click_region, margin=10)
            and _point_in_region(click_point, room_click_region, margin=10)
        )
        logger.info(
            "[ReformingCenterRoomSearch] step=%s target=%s hit=%s click_point=%s "
            "click_ready=%s anchors=%s",
            step_index + 1,
            target_room["display_name"],
            target_hit,
            click_point,
            click_ready,
            anchors,
        )

        if click_ready and target_hit is not None and click_point is not None:
            app.click(x=int(click_point[0]), y=int(click_point[1]))
            if float(after_click_sec) > 0:
                time.sleep(float(after_click_sec))
            return {
                "success": True,
                "status": "room_clicked",
                "page_state": "reforming_center_room_transition_requested",
                "room_key": room_key,
                "room_name": target_room["display_name"],
                "room_title_hit": target_hit,
                "clicked_point": {"x": int(click_point[0]), "y": int(click_point[1])},
                "drag_count": len(attempts),
                "attempts_used": step_index + 1,
                "attempt_trace": attempts,
                "search_region": list(room_search_region),
                "drag_region": list(room_drag_region),
                "click_region": list(room_click_region),
            }

        if target_title is not None:
            predicted_target = target_title
            translation = None
            prediction_source = "visible_target"
        elif anchors:
            translation = _estimate_room_translation(anchors)
            predicted_target = [
                int(target_map_point[0] + translation[0]),
                int(target_map_point[1] + translation[1]),
            ]
            prediction_source = "room_anchors"
        else:
            no_anchor_polls += 1
            attempts.append(
                {
                    "step": step_index + 1,
                    "mode": "no_anchor_retry",
                    "recognized_texts": last_seen_texts,
                }
            )
            if no_anchor_polls >= max(int(max_no_anchor_polls), 1):
                _raise_error(
                    "room_anchor_not_found",
                    "no known Reforming Center room labels were recognized; refusing a blind drag",
                    {
                        "room_name": target_room["display_name"],
                        "polls": no_anchor_polls,
                        "recognized_texts": last_seen_texts,
                        "search_region": list(room_search_region),
                    },
                )
            time.sleep(max(float(settle_sec), 0.05))
            continue

        no_anchor_polls = 0
        drag_plan = _plan_room_drag(
            predicted_target_title=predicted_target,
            desired_target_title=desired_title,
            drag_region=room_drag_region,
        )
        attempt = {
            "step": step_index + 1,
            "mode": "safe_recenter",
            "prediction_source": prediction_source,
            "predicted_target_title": list(predicted_target),
            "translation": list(translation) if translation is not None else None,
            "target_visible": target_title is not None,
            "target_click_ready": click_ready,
            "drag": drag_plan,
            "anchors": anchors,
        }
        attempts.append(attempt)
        logger.info(
            "[ReformingCenterRoomDrag] step=%s target=%s plan=%s",
            step_index + 1,
            target_room["display_name"],
            drag_plan,
        )
        _perform_room_drag(
            app,
            start=drag_plan["start"],
            end=drag_plan["end"],
            duration_sec=drag_duration_sec,
            hold_sec=drag_hold_sec,
        )
        time.sleep(max(float(settle_sec), 0.05))

    _raise_error(
        "reforming_center_room_not_found",
        f"failed to move {target_room['display_name']} into the safe click region",
        {
            "room_key": room_key,
            "room_name": target_room["display_name"],
            "max_search_steps": max(int(max_search_steps), 1),
            "last_seen_texts": last_seen_texts,
            "attempt_trace": attempts,
        },
    )
    return {}
