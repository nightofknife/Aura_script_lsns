"""Passenger recruitment and settlement UI actions for Resonance PC."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested


class PassengerPcError(RuntimeError):
    """Structured expected failure raised by passenger UI actions."""

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
_FULL_SCREEN = [0, 0, 1280, 720]

_MAIN_BUTTON_TEMPLATE = "templates/passenger_management_button.png"
_SCORE_MARKER_TEMPLATE = "templates/passenger_score_marker.png"
_RECRUIT_BUTTON_TEMPLATE = "templates/passenger_recruit_button.png"
_FLYER_MODE_TEMPLATE = "templates/passenger_flyer_mode.png"
_DESTINATION_MARKER_TEMPLATE = "templates/passenger_destination_marker.png"
_DESTINATION_NEXT_TEMPLATE = "templates/passenger_destination_next.png"
_DISPATCH_MARKER_TEMPLATE = "templates/passenger_dispatch_marker.png"
_DISPATCH_LOCATION_TEMPLATE = "templates/passenger_dispatch_location_marker.png"
_DISPATCH_LOCK_TEMPLATE = "templates/passenger_dispatch_lock.png"
_DISPATCH_CONFIRM_TEMPLATE = "templates/passenger_dispatch_confirm.png"
_AMOUNT_MAX_TEMPLATE = "templates/passenger_amount_max.png"
_AMOUNT_CONFIRM_TEMPLATE = "templates/passenger_amount_confirm.png"
_RECRUIT_SUCCESS_TEMPLATE = "templates/passenger_recruit_success.png"
_SETTLEMENT_TEMPLATE = "templates/passenger_revenue_settlement.png"

_MAIN_BUTTON_REGION = [950, 600, 220, 120]
_SCORE_MARKER_REGION = [120, 70, 330, 130]
_RECRUIT_BUTTON_REGION = [1040, 350, 220, 280]
_FLYER_MODE_REGION = [0, 500, 470, 150]
_DESTINATION_MARKER_REGION = [560, 560, 680, 120]
_DESTINATION_NEXT_REGION = [980, 590, 280, 120]
_DESTINATION_CITY_REGION = [540, 75, 710, 500]
_DISPATCH_MARKER_REGION = [430, 20, 430, 110]
_DISPATCH_CARD_REGION = [15, 115, 1250, 500]
_DISPATCH_CONFIRM_REGION = [490, 585, 320, 110]
_AMOUNT_MAX_REGION = [900, 310, 130, 110]
_AMOUNT_CONFIRM_REGION = [620, 445, 660, 140]
_RECRUIT_SUCCESS_REGION = [100, 505, 520, 180]
_SETTLEMENT_REGION = [30, 470, 540, 180]
_VISIT_CITY_REGION = [980, 430, 290, 110]

_DESTINATION_DRAG_UP = ((1160, 525), (1160, 180))
_DESTINATION_DRAG_DOWN = ((1160, 180), (1160, 525))
_SAFE_EXIT_POINT = (640, 690)

_TEMPLATE_THRESHOLD = 0.80
_DISPATCH_TEMPLATE_THRESHOLD = 0.88
_DISPATCH_CLUSTER_DISTANCE = 40
_DISPATCH_LOCK_MAX_DX = 120
_PAGE_TIMEOUT = 12.0
_POLL_INTERVAL = 0.35
_OPEN_MANAGEMENT_MAX_ATTEMPTS = 3
_OPEN_MANAGEMENT_SCORE_TIMEOUT = 4.0
_OPEN_MANAGEMENT_RETRY_DELAY = 1.0

_CITY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "cape_city": ("海角城",),
    "lanxin_city": ("岚心城", "岚心域"),
}

def _apply_location_config() -> None:
    """Load passenger UI regions while retaining safe built-in defaults."""

    global _MAIN_BUTTON_REGION, _SCORE_MARKER_REGION, _RECRUIT_BUTTON_REGION
    global _FLYER_MODE_REGION, _DESTINATION_MARKER_REGION, _DESTINATION_NEXT_REGION
    global _DESTINATION_CITY_REGION, _DISPATCH_MARKER_REGION, _DISPATCH_CARD_REGION
    global _DISPATCH_CONFIRM_REGION, _AMOUNT_MAX_REGION, _AMOUNT_CONFIRM_REGION
    global _RECRUIT_SUCCESS_REGION, _SETTLEMENT_REGION, _VISIT_CITY_REGION
    global _DESTINATION_DRAG_UP, _DESTINATION_DRAG_DOWN, _SAFE_EXIT_POINT

    config_path = _PLAN_ROOT / "data" / "meta" / "location_pc.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        config = payload.get("passenger_ui") if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        config = None
    if not isinstance(config, dict):
        return

    region_targets = {
        "main_button_region": "_MAIN_BUTTON_REGION",
        "score_marker_region": "_SCORE_MARKER_REGION",
        "recruit_button_region": "_RECRUIT_BUTTON_REGION",
        "flyer_mode_region": "_FLYER_MODE_REGION",
        "destination_marker_region": "_DESTINATION_MARKER_REGION",
        "destination_next_region": "_DESTINATION_NEXT_REGION",
        "destination_city_region": "_DESTINATION_CITY_REGION",
        "dispatch_marker_region": "_DISPATCH_MARKER_REGION",
        "dispatch_card_region": "_DISPATCH_CARD_REGION",
        "dispatch_confirm_region": "_DISPATCH_CONFIRM_REGION",
        "amount_max_region": "_AMOUNT_MAX_REGION",
        "amount_confirm_region": "_AMOUNT_CONFIRM_REGION",
        "recruit_success_region": "_RECRUIT_SUCCESS_REGION",
        "settlement_region": "_SETTLEMENT_REGION",
        "visit_city_region": "_VISIT_CITY_REGION",
    }
    for key, target in region_targets.items():
        value = config.get(key)
        if isinstance(value, list) and len(value) == 4:
            globals()[target] = [int(part) for part in value]
    for key, target in (
        ("destination_drag_up", "_DESTINATION_DRAG_UP"),
        ("destination_drag_down", "_DESTINATION_DRAG_DOWN"),
    ):
        value = config.get(key)
        if (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(point, list) and len(point) == 2 for point in value)
        ):
            globals()[target] = (
                (int(value[0][0]), int(value[0][1])),
                (int(value[1][0]), int(value[1][1])),
            )
    safe_exit = config.get("safe_exit_point")
    if isinstance(safe_exit, list) and len(safe_exit) == 2:
        _SAFE_EXIT_POINT = (int(safe_exit[0]), int(safe_exit[1]))


_apply_location_config()


def _raise(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    raise PassengerPcError(code, message, detail)


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Resonance PC passenger task was cancelled")


def _normalize_text(value: Any) -> str:
    return re.sub(r"[\s\u3000\|:：,，。.!！?？（）()\[\]【】<>《》'\"`~\-]+", "", str(value)).lower()


def _coerce_region(value: Sequence[int]) -> Tuple[int, int, int, int]:
    if len(value) != 4:
        _raise("invalid_region", "region must be [x, y, w, h]", {"region": list(value)})
    return tuple(int(part) for part in value)  # type: ignore[return-value]


def _template_path(template: str) -> str:
    raw = Path(str(template or ""))
    return str(raw if raw.is_absolute() else (_PLAN_ROOT / raw).resolve())


def _capture_text_items(app: Any, ocr: Any, region: Sequence[int]) -> List[Dict[str, Any]]:
    _check_cancelled()
    rect = _coerce_region(region)
    capture = app.capture(rect=rect)
    if not capture.success:
        _raise("capture_failed", "failed to capture passenger UI region", {"region": list(rect)})
    recognized = ocr.recognize_all(source_image=capture.image)
    rows: List[Dict[str, Any]] = []
    for item in getattr(recognized, "results", []) or []:
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        center = getattr(item, "center_point", None)
        rect_value = getattr(item, "rect", None)
        row: Dict[str, Any] = {
            "text": text,
            "norm_text": _normalize_text(text),
            "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
        }
        if center and len(center) == 2:
            row["center"] = [int(rect[0] + int(center[0])), int(rect[1] + int(center[1]))]
        if rect_value and len(rect_value) == 4:
            row["rect"] = [
                int(rect[0] + int(rect_value[0])),
                int(rect[1] + int(rect_value[1])),
                int(rect_value[2]),
                int(rect_value[3]),
            ]
        rows.append(row)
    rows.sort(key=lambda row: float(row.get("confidence") or 0.0), reverse=True)
    return rows


def _match_template(
    app: Any,
    vision: Any,
    template: str,
    region: Sequence[int],
    threshold: float = _TEMPLATE_THRESHOLD,
) -> Dict[str, Any]:
    _check_cancelled()
    rect = _coerce_region(region)
    capture = app.capture(rect=rect)
    if not capture.success:
        return {"found": False, "reason": "capture_failed", "template": template, "region": list(rect)}
    result = vision.find_template(
        source_image=capture.image,
        template_image=_template_path(template),
        threshold=float(threshold),
        use_grayscale=True,
    )
    center = getattr(result, "center_point", None)
    payload: Dict[str, Any] = {
        "found": bool(getattr(result, "found", False)),
        "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
        "template": template,
        "region": list(rect),
    }
    if center and len(center) == 2:
        payload["center"] = [int(rect[0] + int(center[0])), int(rect[1] + int(center[1]))]
    return payload


def _wait_template(
    app: Any,
    vision: Any,
    template: str,
    region: Sequence[int],
    *,
    timeout_sec: float = _PAGE_TIMEOUT,
    threshold: float = _TEMPLATE_THRESHOLD,
    interval_sec: float = _POLL_INTERVAL,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last: Dict[str, Any] = {"found": False, "template": template, "region": list(region)}
    while True:
        last = _match_template(app, vision, template, region, threshold)
        if last.get("found") or time.monotonic() >= deadline:
            return last
        time.sleep(max(float(interval_sec), 0.05))


def _click_template_required(
    app: Any,
    vision: Any,
    template: str,
    region: Sequence[int],
    *,
    error_code: str,
    timeout_sec: float = _PAGE_TIMEOUT,
) -> Dict[str, Any]:
    match = _wait_template(app, vision, template, region, timeout_sec=timeout_sec)
    center = match.get("center")
    if not match.get("found") or not isinstance(center, list) or len(center) != 2:
        _raise(error_code, f"required passenger template was not found: {template}", {"match": match})
    _check_cancelled()
    app.click(x=int(center[0]), y=int(center[1]))
    return match


def _wait_template_absent(
    app: Any,
    vision: Any,
    template: str,
    region: Sequence[int],
    *,
    timeout_sec: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last: Dict[str, Any] = {}
    while True:
        last = _match_template(app, vision, template, region)
        if not last.get("found"):
            return {"absent": True, "last_match": last}
        if time.monotonic() >= deadline:
            return {"absent": False, "last_match": last}
        time.sleep(_POLL_INTERVAL)


def _wait_main_stable(app: Any, vision: Any, *, timeout_sec: float = 12.0) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    confirmations = 0
    last: Dict[str, Any] = {}
    while True:
        last = _match_template(app, vision, _MAIN_BUTTON_TEMPLATE, _MAIN_BUTTON_REGION)
        confirmations = confirmations + 1 if last.get("found") else 0
        if confirmations >= 2:
            return {"confirmed": True, "confirmations": confirmations, "match": last}
        if time.monotonic() >= deadline:
            return {"confirmed": False, "confirmations": confirmations, "match": last}
        time.sleep(_POLL_INTERVAL)


def _click_blank_and_confirm_main(app: Any, vision: Any, *, error_code: str) -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = []
    for attempt in range(1, 4):
        _check_cancelled()
        app.click(x=_SAFE_EXIT_POINT[0], y=_SAFE_EXIT_POINT[1])
        confirmed = _wait_main_stable(app, vision, timeout_sec=3.0)
        attempts.append({"attempt": attempt, "main": confirmed})
        if confirmed.get("confirmed"):
            return {"success": True, "attempts": attempts, "exit_point": list(_SAFE_EXIT_POINT)}
    _raise(error_code, "passenger page did not return to the city main screen", {"attempts": attempts})
    return {}


def _parse_ratios(items: Iterable[Dict[str, Any]]) -> List[Dict[str, int]]:
    ratios: List[Dict[str, int]] = []
    for item in items:
        text = str(item.get("text") or "")
        for match in re.finditer(r"(\d{1,6})\s*/\s*(\d{1,6})", text):
            current, total = int(match.group(1)), int(match.group(2))
            if total > 0 and current <= total:
                ratios.append({"current": current, "total": total})
    return ratios


def _passenger_ratio(items: Iterable[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    ratios = [ratio for ratio in _parse_ratios(items) if ratio["total"] <= 300]
    return min(ratios, key=lambda ratio: ratio["total"]) if ratios else None


def _flyer_ratio(items: Iterable[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    ratios = [ratio for ratio in _parse_ratios(items) if ratio["total"] > 300]
    return max(ratios, key=lambda ratio: ratio["total"]) if ratios else None


def _numeric_value_near_label(items: Sequence[Dict[str, Any]], labels: Sequence[str]) -> Optional[int]:
    normalized_labels = tuple(_normalize_text(label) for label in labels)
    label_rows = [
        row
        for row in items
        if any(label and label in str(row.get("norm_text") or "") for label in normalized_labels)
    ]
    for row in label_rows:
        text = str(row.get("text") or "")
        inline = re.findall(r"\d[\d,，]*", text)
        if inline:
            return int(re.sub(r"\D", "", inline[-1]))
        center = row.get("center")
        if not isinstance(center, list) or len(center) != 2:
            continue
        candidates: List[Tuple[int, int]] = []
        for other in items:
            other_center = other.get("center")
            if not isinstance(other_center, list) or len(other_center) != 2:
                continue
            numeric = re.fullmatch(r"[\d,，]+", str(other.get("text") or "").strip())
            if numeric is None:
                continue
            dx = int(other_center[0]) - int(center[0])
            dy = abs(int(other_center[1]) - int(center[1]))
            if -40 <= dx <= 500 and dy <= 80:
                candidates.append((abs(dx) + dy, int(re.sub(r"\D", "", numeric.group(0)))))
        if candidates:
            return min(candidates, key=lambda pair: pair[0])[1]
    return None


def _resolve_city_aliases(to_city_name: str) -> Tuple[str, ...]:
    raw = str(to_city_name or "").strip()
    if not raw:
        _raise("destination_not_found", "target passenger city is required")
    normalized = _normalize_text(raw)
    for city_key, aliases in _CITY_ALIASES.items():
        if normalized == _normalize_text(city_key) or any(_normalize_text(alias) in normalized for alias in aliases):
            return aliases
    return (raw,)


def _find_alias_hit(items: Sequence[Dict[str, Any]], aliases: Sequence[str]) -> Optional[Dict[str, Any]]:
    wanted = tuple(_normalize_text(alias) for alias in aliases)
    for row in items:
        center = row.get("center")
        norm = str(row.get("norm_text") or "")
        if not isinstance(center, list) or len(center) != 2:
            continue
        if any(alias and alias in norm for alias in wanted):
            return row
    return None


def _visit_city_entry_evidence(app: Any, ocr: Any) -> Dict[str, Any]:
    items = _capture_text_items(app, ocr, _VISIT_CITY_REGION)
    wanted = {
        _normalize_text(alias)
        for alias in ("访问城市", "访问地区", "进入城市")
    }
    hit = next(
        (
            row
            for row in items
            if str(row.get("norm_text") or "") in wanted
            and isinstance(row.get("center"), list)
            and len(row["center"]) == 2
        ),
        None,
    )
    return {"found": hit is not None, "hit": hit, "ocr_items": items}


def _drag(app: Any, controller: Any, start: Tuple[int, int], end: Tuple[int, int]) -> None:
    _check_cancelled()
    app.move_to(x=start[0], y=start[1], duration=0.1)
    pressed = False
    try:
        controller.mouse_down("left")
        pressed = True
        app.move_to(x=end[0], y=end[1], duration=0.55)
        time.sleep(0.25)
    finally:
        if pressed:
            controller.mouse_up("left")
    time.sleep(0.5)


def _select_destination_city(
    to_city_name: str,
    app: Any,
    ocr: Any,
    controller: Any,
    *,
    max_search_steps: int,
) -> Dict[str, Any]:
    aliases = _resolve_city_aliases(to_city_name)
    steps = max(int(max_search_steps), 1)
    direction = "down"
    previous_signature: Tuple[str, ...] = ()
    repeated = 0
    trace: List[Dict[str, Any]] = []
    for step in range(steps):
        items = _capture_text_items(app, ocr, _DESTINATION_CITY_REGION)
        signature = tuple(sorted({str(row.get("norm_text") or "") for row in items if row.get("norm_text")}))
        hit = _find_alias_hit(items, aliases)
        trace.append(
            {
                "step": step + 1,
                "direction": direction,
                "visible_texts": [str(row.get("text") or "") for row in items],
                "signature": list(signature),
            }
        )
        if hit is not None:
            center = hit["center"]
            app.click(x=int(center[0]), y=int(center[1]))
            return {"success": True, "hit": hit, "steps": step + 1, "trace": trace}
        repeated = repeated + 1 if signature and signature == previous_signature else 0
        previous_signature = signature
        if direction == "down" and (repeated >= 2 or step + 1 >= max(1, steps // 2)):
            direction = "up"
            repeated = 0
        drag_path = _DESTINATION_DRAG_UP if direction == "down" else _DESTINATION_DRAG_DOWN
        _drag(app, controller, drag_path[0], drag_path[1])
    _raise(
        "destination_not_found",
        f"passenger destination '{to_city_name}' was not found after vertical search",
        {"target": to_city_name, "max_search_steps": steps, "trace": trace},
    )
    return {}


def _multi_match_rows(result: Any, template: str, region: Sequence[int]) -> List[Dict[str, Any]]:
    rect = _coerce_region(region)
    rows: List[Dict[str, Any]] = []
    for match in getattr(result, "matches", []) or []:
        center = getattr(match, "center_point", None)
        match_rect = getattr(match, "rect", None)
        if not center or len(center) != 2:
            continue
        row: Dict[str, Any] = {
            "center": [int(rect[0] + int(center[0])), int(rect[1] + int(center[1]))],
            "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
            "template": template,
        }
        if match_rect and len(match_rect) == 4:
            row["rect"] = [
                int(rect[0] + int(match_rect[0])),
                int(rect[1] + int(match_rect[1])),
                int(match_rect[2]),
                int(match_rect[3]),
            ]
        rows.append(row)

    clustered: List[Dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: float(item["confidence"]), reverse=True):
        x, y = (int(part) for part in row["center"])
        duplicate = any(
            abs(x - int(existing["center"][0])) <= _DISPATCH_CLUSTER_DISTANCE
            and abs(y - int(existing["center"][1])) <= _DISPATCH_CLUSTER_DISTANCE
            for existing in clustered
        )
        if not duplicate:
            clustered.append(row)
    return sorted(clustered, key=lambda row: int(row["center"][0]))


def _detect_dispatch_locations(app: Any, vision: Any) -> Dict[str, Any]:
    """Detect dispatch cards and locked cards from one frame without OCR."""

    _check_cancelled()
    rect = _coerce_region(_DISPATCH_CARD_REGION)
    capture = app.capture(rect=rect)
    if not capture.success:
        _raise("capture_failed", "failed to capture passenger dispatch cards", {"region": list(rect)})
    results = vision.find_all_templates_batch(
        source_image=capture.image,
        template_images=[
            _template_path(_DISPATCH_LOCATION_TEMPLATE),
            _template_path(_DISPATCH_LOCK_TEMPLATE),
        ],
        threshold=_DISPATCH_TEMPLATE_THRESHOLD,
        # The shared matcher emits every high-confidence candidate here. Passenger-local
        # spatial clustering below keeps one best hit per card without global NMS merging
        # separate cards.
        nms_threshold=1.0,
        use_grayscale=True,
    )
    if not isinstance(results, list) or len(results) != 2:
        raise RuntimeError("dispatch template batch must return location and lock results")

    locations = _multi_match_rows(results[0], _DISPATCH_LOCATION_TEMPLATE, rect)
    locks = _multi_match_rows(results[1], _DISPATCH_LOCK_TEMPLATE, rect)
    locked_indexes: set[int] = set()
    lock_links: List[Dict[str, Any]] = []
    unmatched_locks: List[Dict[str, Any]] = []
    for lock in locks:
        if not locations:
            unmatched_locks.append(lock)
            continue
        lock_x = int(lock["center"][0])
        index = min(
            range(len(locations)),
            key=lambda candidate_index: abs(int(locations[candidate_index]["center"][0]) - lock_x),
        )
        distance = abs(int(locations[index]["center"][0]) - lock_x)
        if distance <= _DISPATCH_LOCK_MAX_DX:
            locked_indexes.add(index)
            lock_links.append(
                {
                    "location_index": index,
                    "location": locations[index],
                    "lock": lock,
                    "horizontal_distance": distance,
                }
            )
        else:
            unmatched_locks.append(lock)

    available = [
        location for index, location in enumerate(locations) if index not in locked_indexes
    ]
    return {
        "locations": locations,
        "locks": locks,
        "locked_location_indexes": sorted(locked_indexes),
        "lock_links": lock_links,
        "unmatched_locks": unmatched_locks,
        "available_locations": available,
        "region": list(rect),
        "threshold": _DISPATCH_TEMPLATE_THRESHOLD,
    }


def _select_rightmost_dispatch(app: Any, vision: Any) -> Dict[str, Any]:
    detection = _detect_dispatch_locations(app, vision)
    available = list(detection["available_locations"])
    if not available:
        _raise(
            "dispatch_location_not_found",
            "no unlocked passenger flyer dispatch location was detected",
            detection,
        )

    selected = max(available, key=lambda row: int(row["center"][0]))
    center = selected["center"]
    _check_cancelled()
    app.click(x=int(center[0]), y=int(center[1]))
    confirm = _wait_template(
        app,
        vision,
        _DISPATCH_CONFIRM_TEMPLATE,
        _DISPATCH_CONFIRM_REGION,
        timeout_sec=1.2,
    )
    if not confirm.get("found"):
        _raise(
            "dispatch_selection_not_confirmed",
            "the rightmost unlocked passenger dispatch location could not be selected",
            {"selected": selected, "detection": detection, "confirm_match": confirm},
        )
    return {
        "success": True,
        "selected": selected,
        "confirm_match": confirm,
        "detection": detection,
    }


def _open_recruitment_hub_from_score(app: Any, ocr: Any, vision: Any) -> Dict[str, Any]:
    _click_template_required(
        app,
        vision,
        _RECRUIT_BUTTON_TEMPLATE,
        _RECRUIT_BUTTON_REGION,
        error_code="passenger_recruit_button_not_found",
    )
    marker = _wait_template(app, vision, _FLYER_MODE_TEMPLATE, _FLYER_MODE_REGION)
    if not marker.get("found"):
        _raise("passenger_recruit_hub_not_found", "passenger recruitment hub did not appear", {"match": marker})
    items = _capture_text_items(app, ocr, _FULL_SCREEN)
    return {
        "success": True,
        "page_state": "recruitment_hub",
        "flyer_ratio": _flyer_ratio(items),
        "ocr_items": items,
    }


@action_info(
    name="resonance_pc.open_passenger_management",
    public=True,
    read_only=False,
    description="Open passenger management from the stable Resonance PC city-main screen.",
)
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_open_passenger_management(app: Any = None, vision: Any = None) -> Dict[str, Any]:
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    attempts: List[Dict[str, Any]] = []
    last_main: Dict[str, Any] = {}
    last_score: Dict[str, Any] = {}

    for attempt in range(1, _OPEN_MANAGEMENT_MAX_ATTEMPTS + 1):
        stable = _wait_main_stable(
            app,
            vision,
            timeout_sec=12.0 if attempt == 1 else 4.0,
        )
        last_main = dict(stable.get("match") or {})
        center = last_main.get("center")
        record: Dict[str, Any] = {
            "attempt": attempt,
            "main_stable": stable,
            "clicked": False,
        }
        logger.info(
            "[PassengerManagement] phase=main_stable attempt=%s/%s confirmed=%s "
            "confidence=%.4f center=%s",
            attempt,
            _OPEN_MANAGEMENT_MAX_ATTEMPTS,
            bool(stable.get("confirmed")),
            float(last_main.get("confidence") or 0.0),
            last_main.get("center"),
        )

        if not stable.get("confirmed") or not isinstance(center, list) or len(center) != 2:
            last_score = _wait_template(
                app,
                vision,
                _SCORE_MARKER_TEMPLATE,
                _SCORE_MARKER_REGION,
                timeout_sec=_OPEN_MANAGEMENT_SCORE_TIMEOUT,
            )
            record["score_marker"] = last_score
            attempts.append(record)
            if last_score.get("found"):
                return {
                    "success": True,
                    "page_state": "passenger_score",
                    "main_button": last_main,
                    "score_marker": last_score,
                    "attempts": attempts,
                }
            continue

        _check_cancelled()
        app.click(x=int(center[0]), y=int(center[1]))
        record["clicked"] = True
        record["click_point"] = [int(center[0]), int(center[1])]
        last_score = _wait_template(
            app,
            vision,
            _SCORE_MARKER_TEMPLATE,
            _SCORE_MARKER_REGION,
            timeout_sec=_OPEN_MANAGEMENT_SCORE_TIMEOUT,
        )
        record["score_marker"] = last_score
        attempts.append(record)
        logger.info(
            "[PassengerManagement] phase=score attempt=%s/%s found=%s confidence=%.4f",
            attempt,
            _OPEN_MANAGEMENT_MAX_ATTEMPTS,
            bool(last_score.get("found")),
            float(last_score.get("confidence") or 0.0),
        )
        if last_score.get("found"):
            return {
                "success": True,
                "page_state": "passenger_score",
                "main_button": last_main,
                "score_marker": last_score,
                "attempts": attempts,
            }
        if attempt < _OPEN_MANAGEMENT_MAX_ATTEMPTS:
            time.sleep(_OPEN_MANAGEMENT_RETRY_DELAY)

    first_main_confirmed = bool(
        attempts and (attempts[0].get("main_stable") or {}).get("confirmed")
    )
    if not first_main_confirmed:
        _raise(
            "not_on_city_main",
            "passenger management button was not stable on city main",
            {"attempts": attempts, "last_main": last_main, "last_score": last_score},
        )
    _raise(
        "passenger_management_not_found",
        "passenger score page did not appear after retrying the management button",
        {"attempts": attempts, "last_main": last_main, "last_score": last_score},
    )
    return {}


@action_info(
    name="resonance_pc.recruit_passengers_by_flyer",
    public=True,
    read_only=False,
    description="Recruit passengers for one destination with flyers and return to city main.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
    controller="plans/aura_base/controller",
)
def resonance_pc_recruit_passengers_by_flyer(
    to_city_name: str,
    max_search_steps: int = 12,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
    controller: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None or controller is None:
        raise RuntimeError("app/ocr/vision/controller services are required")

    if not _match_template(app, vision, _SCORE_MARKER_TEMPLATE, _SCORE_MARKER_REGION).get("found"):
        resonance_pc_open_passenger_management(app=app, vision=vision)
    hub = _open_recruitment_hub_from_score(app, ocr, vision)
    initial_flyers = int((hub.get("flyer_ratio") or {}).get("current") or 0)
    _click_template_required(
        app,
        vision,
        _FLYER_MODE_TEMPLATE,
        _FLYER_MODE_REGION,
        error_code="passenger_flyer_mode_not_found",
    )
    destination_page = _wait_template(app, vision, _DESTINATION_MARKER_TEMPLATE, _DESTINATION_MARKER_REGION)
    if not destination_page.get("found"):
        _raise("passenger_destination_page_not_found", "flyer destination page did not appear", {"match": destination_page})
    destination = _select_destination_city(
        to_city_name,
        app,
        ocr,
        controller,
        max_search_steps=max_search_steps,
    )
    _click_template_required(
        app,
        vision,
        _DESTINATION_NEXT_TEMPLATE,
        _DESTINATION_NEXT_REGION,
        error_code="passenger_destination_next_not_found",
    )
    dispatch_page = _wait_template(app, vision, _DISPATCH_MARKER_TEMPLATE, _DISPATCH_MARKER_REGION)
    if not dispatch_page.get("found"):
        _raise("passenger_dispatch_page_not_found", "passenger dispatch page did not appear", {"match": dispatch_page})
    dispatch = _select_rightmost_dispatch(app, vision)
    confirm_match = dispatch["confirm_match"]
    confirm_center = confirm_match.get("center")
    app.click(x=int(confirm_center[0]), y=int(confirm_center[1]))

    _click_template_required(
        app,
        vision,
        _AMOUNT_MAX_TEMPLATE,
        _AMOUNT_MAX_REGION,
        error_code="passenger_amount_max_not_found",
    )
    _click_template_required(
        app,
        vision,
        _AMOUNT_CONFIRM_TEMPLATE,
        _AMOUNT_CONFIRM_REGION,
        error_code="flyer_amount_confirm_failed",
    )
    success_marker = _wait_template(
        app,
        vision,
        _RECRUIT_SUCCESS_TEMPLATE,
        _RECRUIT_SUCCESS_REGION,
        timeout_sec=30.0,
    )
    if not success_marker.get("found"):
        _raise("recruitment_result_unreadable", "flyer recruitment result did not appear", {"match": success_marker})

    result_items: List[Dict[str, Any]] = []
    recruited: Optional[int] = None
    for _attempt in range(3):
        result_items = _capture_text_items(app, ocr, _FULL_SCREEN)
        recruited = _numeric_value_near_label(result_items, ("招揽乘客人数", "揽客人数"))
        if recruited is not None:
            break
        time.sleep(0.5)
    if recruited is None:
        _raise("recruitment_result_unreadable", "unable to read recruited passenger count", {"ocr_items": result_items})

    remaining_seats = _passenger_ratio(result_items)
    remaining_flyers = _flyer_ratio(result_items)
    cleanup = _click_blank_and_confirm_main(app, vision, error_code="main_screen_not_restored")
    if recruited <= 0:
        return {
            "success": False,
            "status": "blocked",
            "reason": "no_passengers_recruited",
            "recruited_passengers": 0,
            "cleanup": cleanup,
        }
    remaining_flyer_count = int((remaining_flyers or {}).get("current") or 0)
    return {
        "success": True,
        "status": "completed",
        "reason": None,
        "to_city_name": str(to_city_name),
        "recruited_passengers": int(recruited),
        "seat_capacity": int((remaining_seats or {}).get("total") or 0),
        "remaining_seats": int((remaining_seats or {}).get("current") or 0),
        "flyers_before": initial_flyers,
        "flyers_remaining": remaining_flyer_count,
        "flyers_used": max(initial_flyers - remaining_flyer_count, 0),
        "destination": destination,
        "dispatch": dispatch,
        "cleanup": cleanup,
        "ocr_items": result_items,
    }


@action_info(
    name="resonance_pc.enter_city_and_settle_passengers",
    public=True,
    read_only=False,
    description="Enter the arrived city, require passenger revenue settlement, and return to city main.",
)
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", vision="plans/aura_base/vision")
def resonance_pc_enter_city_and_settle_passengers(
    settlement_timeout_sec: float = 30.0,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None:
        raise RuntimeError("app/ocr/vision services are required")
    # The shared intercity action normally clicks 进入站点 itself.  In that case
    # the settlement is already open when this passenger-specific action starts.
    initial_settlement_timeout = min(max(float(settlement_timeout_sec), 0.1), 12.0)
    settlement = _wait_template(
        app,
        vision,
        _SETTLEMENT_TEMPLATE,
        _SETTLEMENT_REGION,
        timeout_sec=initial_settlement_timeout,
    )
    visit_hit: Optional[Dict[str, Any]] = None
    if not settlement.get("found"):
        deadline = time.monotonic() + 12.0
        while True:
            items = _capture_text_items(app, ocr, _VISIT_CITY_REGION)
            visit_hit = _find_alias_hit(items, ("访问城市", "访问地区", "进入城市"))
            if visit_hit is not None or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL)
        if visit_hit is None:
            _raise(
                "enter_city_button_not_found",
                "arrival city entry button was not found",
                {"region": _VISIT_CITY_REGION},
            )
        center = visit_hit["center"]
        app.click(x=int(center[0]), y=int(center[1]))
        settlement = _wait_template(
            app,
            vision,
            _SETTLEMENT_TEMPLATE,
            _SETTLEMENT_REGION,
            timeout_sec=max(float(settlement_timeout_sec), 0.1),
        )
    if not settlement.get("found"):
        _raise("passenger_settlement_not_found", "passenger revenue settlement did not appear", {"match": settlement})
    items = _capture_text_items(app, ocr, _FULL_SCREEN)
    revenue = {
        "ticket_revenue": _numeric_value_near_label(items, ("车票收益",)),
        "extra_revenue": _numeric_value_near_label(items, ("车票外收益",)),
        "total_revenue": _numeric_value_near_label(items, ("总收益",)),
        "passenger_garbage": _numeric_value_near_label(items, ("乘客垃圾产出",)),
    }

    dismiss_attempts: List[Dict[str, Any]] = []
    for attempt in range(1, 4):
        _check_cancelled()
        app.click(x=_SAFE_EXIT_POINT[0], y=_SAFE_EXIT_POINT[1])
        absent = _wait_template_absent(
            app,
            vision,
            _SETTLEMENT_TEMPLATE,
            _SETTLEMENT_REGION,
            timeout_sec=2.5,
        )
        visit_city = (
            _visit_city_entry_evidence(app, ocr)
            if absent.get("absent")
            else {"found": False, "ocr_items": []}
        )
        main = (
            _wait_main_stable(app, vision, timeout_sec=3.0)
            if visit_city.get("found")
            else {"confirmed": False, "reason": "visit_city_not_found"}
        )
        dismiss_attempts.append(
            {
                "attempt": attempt,
                "absent": absent,
                "visit_city": visit_city,
                "main": main,
            }
        )
        if absent.get("absent") and visit_city.get("found") and main.get("confirmed"):
            return {
                "success": True,
                "status": "completed",
                "reason": None,
                **revenue,
                "ocr_items": items,
                "settlement_match": settlement,
                "dismiss_attempts": dismiss_attempts,
            }
    _raise("settlement_dismiss_failed", "passenger settlement could not be dismissed to city main", {"attempts": dismiss_attempts})
    return {}
