"""Template-only rubbish recycling flow for the Resonance Windows client."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

from ..services.city_shop_data_pc_service import ResonancePcCityShopDataService


class RubbishRecyclingError(RuntimeError):
    """Structured failure raised by the rubbish recycling flow."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_PLAN_ROOT = Path(__file__).resolve().parents[2]
_CITY_CONFIG_PATH = "data/meta/rubbish_recycling_pc.json"
_LOCATION_PATH = "data/meta/location_pc.json"

_ENTRY_TEMPLATE = "templates/rubbish_recycle_entry.png"
_RECYCLE_ALL_TEMPLATE = "templates/rubbish_recycle_all_enabled.png"
_NO_RUBBISH_TEMPLATE = "templates/rubbish_no_rubbish_disabled.png"
_BACK_TEMPLATE = "templates/nav_back_button.png"

_ENTRY_REGION = (700, 260, 550, 140)
_STATE_REGION = (970, 85, 290, 105)
_BACK_REGION = (0, 0, 170, 80)
_REWARD_DISMISS_POINT = (500, 500)

_MATCH_TIMEOUT_SEC = 3.0
_MATCH_INTERVAL_SEC = 0.3
_MATCH_THRESHOLD = 0.86
_AMBIGUOUS_CONFIDENCE_GAP = 0.03
_STEP_INTERVAL_SEC = 0.5
_RECYCLE_RECHECK_DELAY_SEC = 0.2
_REWARD_DISMISS_INTERVAL_SEC = 1.0
_ENTRY_MAX_ROUNDS = 3
_RECYCLE_MAX_ROUNDS = 5


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    logger.error("Rubbish recycling failed code=%s message=%s detail=%s", code, message, detail or {})
    raise RubbishRecyclingError(code=code, message=message, detail=detail)


def _resolve_plan_path(path: str) -> Path:
    raw = Path(str(path or "").strip())
    return raw if raw.is_absolute() else _PLAN_ROOT / raw


def _load_city_config(config_file_path: str = _CITY_CONFIG_PATH) -> Dict[str, Dict[str, str]]:
    path = _resolve_plan_path(config_file_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _raise_error("rubbish_city_config_not_found", "rubbish city configuration was not found", {"path": str(path)})
    except json.JSONDecodeError as exc:
        _raise_error(
            "rubbish_city_config_invalid",
            "rubbish city configuration is not valid JSON",
            {"path": str(path), "cause": str(exc)},
        )
    cities = payload.get("eligible_cities") if isinstance(payload, dict) else None
    if not isinstance(cities, dict):
        _raise_error(
            "rubbish_city_config_invalid",
            "rubbish city configuration must contain eligible_cities",
            {"path": str(path)},
        )
    normalized: Dict[str, Dict[str, str]] = {}
    for city_id, raw_city in cities.items():
        if not isinstance(raw_city, dict):
            continue
        normalized[str(city_id)] = {
            "city_name": str(raw_city.get("city_name") or "").strip(),
            "city_key": str(raw_city.get("city_key") or "").strip(),
            "shop_name": str(raw_city.get("shop_name") or "垃圾处理中心").strip(),
        }
    return normalized


def resolve_rubbish_recycling_city(
    *,
    city_id: Any = None,
    city_name: Any = None,
    city_key: Any = None,
    config_file_path: str = _CITY_CONFIG_PATH,
) -> Optional[Dict[str, str]]:
    cities = _load_city_config(config_file_path)
    normalized_id = str(city_id or "").strip()
    if normalized_id and normalized_id in cities:
        return {"city_id": normalized_id, **cities[normalized_id]}
    normalized_name = str(city_name or "").strip()
    normalized_key = str(city_key or "").strip().lower()
    for candidate_id, candidate in cities.items():
        if normalized_name and normalized_name == candidate.get("city_name"):
            return {"city_id": candidate_id, **candidate}
        if normalized_key and normalized_key == str(candidate.get("city_key") or "").lower():
            return {"city_id": candidate_id, **candidate}
    return None


def is_rubbish_recycling_arrival(leg: Mapping[str, Any]) -> bool:
    return resolve_rubbish_recycling_city(
        city_id=leg.get("to_city_id"),
        city_name=leg.get("to_city"),
        city_key=leg.get("to_city_key"),
    ) is not None


def _coerce_region(region: Sequence[int]) -> Tuple[int, int, int, int]:
    if len(region) != 4:
        _raise_error("rubbish_region_invalid", "template region must contain x, y, width and height")
    return tuple(int(value) for value in region)  # type: ignore[return-value]


def _match_template(
    app: Any,
    vision: Any,
    *,
    template: str,
    region: Sequence[int],
    source_image: Any = None,
    threshold: float = _MATCH_THRESHOLD,
) -> Dict[str, Any]:
    rect = _coerce_region(region)
    if source_image is None:
        capture = app.capture(rect=rect)
        if not bool(getattr(capture, "success", False)):
            return {
                "found": False,
                "template": template,
                "region": list(rect),
                "confidence": 0.0,
                "reason": "capture_failed",
            }
        source_image = capture.image
    match = vision.find_template(
        source_image=source_image,
        template_image=str(_resolve_plan_path(template)),
        threshold=float(threshold),
        use_grayscale=True,
    )
    center = getattr(match, "center_point", None)
    result: Dict[str, Any] = {
        "found": bool(getattr(match, "found", False)),
        "template": template,
        "region": list(rect),
        "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
    }
    if center and len(center) == 2:
        result["center"] = [int(rect[0] + int(center[0])), int(rect[1] + int(center[1]))]
    return result


def _wait_template(
    app: Any,
    vision: Any,
    *,
    template: str,
    region: Sequence[int],
    timeout_sec: float = _MATCH_TIMEOUT_SEC,
    interval_sec: float = _MATCH_INTERVAL_SEC,
) -> Dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + max(float(timeout_sec), 0.0)
    polls = 0
    last: Dict[str, Any] = {"found": False, "template": template, "region": list(region)}
    while True:
        polls += 1
        last = _match_template(app, vision, template=template, region=region)
        logger.debug(
            "Rubbish template poll template=%s poll=%s found=%s confidence=%.4f elapsed_ms=%s",
            template,
            polls,
            bool(last.get("found")),
            float(last.get("confidence") or 0.0),
            int((time.monotonic() - started_at) * 1000),
        )
        if last.get("found"):
            return {
                **last,
                "polls": polls,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }
        if time.monotonic() >= deadline:
            return {
                **last,
                "polls": polls,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }
        remaining_sec = max(deadline - time.monotonic(), 0.0)
        time.sleep(min(max(float(interval_sec), 0.05), remaining_sec))


def _probe_rubbish_state(app: Any, vision: Any) -> Dict[str, Any]:
    enabled: Dict[str, Any] = {"found": False, "confidence": 0.0}
    empty: Dict[str, Any] = {"found": False, "confidence": 0.0}
    rect = _coerce_region(_STATE_REGION)
    capture = app.capture(rect=rect)
    if bool(getattr(capture, "success", False)):
        enabled = _match_template(
            app,
            vision,
            template=_RECYCLE_ALL_TEMPLATE,
            region=rect,
            source_image=capture.image,
        )
        empty = _match_template(
            app,
            vision,
            template=_NO_RUBBISH_TEMPLATE,
            region=rect,
            source_image=capture.image,
        )
    enabled_found = bool(enabled.get("found"))
    empty_found = bool(empty.get("found"))
    if enabled_found and empty_found:
        gap = abs(
            float(enabled.get("confidence") or 0.0)
            - float(empty.get("confidence") or 0.0)
        )
        if gap < _AMBIGUOUS_CONFIDENCE_GAP:
            _raise_error(
                "rubbish_state_ambiguous",
                "both rubbish state templates matched with similar confidence",
                {"enabled": enabled, "empty": empty},
            )
    selected_state: Optional[str] = None
    selected: Optional[Dict[str, Any]] = None
    if enabled_found or empty_found:
        selected_state = (
            "has_rubbish"
            if float(enabled.get("confidence") or 0.0)
            >= float(empty.get("confidence") or 0.0)
            else "empty"
        )
        selected = enabled if selected_state == "has_rubbish" else empty
    return {
        "state": selected_state,
        "match": selected,
        "enabled_match": enabled,
        "empty_match": empty,
    }


def _wait_rubbish_state(app: Any, vision: Any) -> Dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + _MATCH_TIMEOUT_SEC
    polls = 0
    last_probe: Dict[str, Any] = {
        "state": None,
        "match": None,
        "enabled_match": {"found": False, "confidence": 0.0},
        "empty_match": {"found": False, "confidence": 0.0},
    }
    while True:
        polls += 1
        last_probe = _probe_rubbish_state(app, vision)
        current_state = last_probe.get("state")
        logger.debug(
            "Rubbish state poll=%s enabled=%s/%.4f empty=%s/%.4f elapsed_ms=%s",
            polls,
            bool(last_probe["enabled_match"].get("found")),
            float(last_probe["enabled_match"].get("confidence") or 0.0),
            bool(last_probe["empty_match"].get("found")),
            float(last_probe["empty_match"].get("confidence") or 0.0),
            int((time.monotonic() - started_at) * 1000),
        )
        if current_state is not None:
            return {
                **last_probe,
                "state": str(current_state),
                "polls": polls,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }
        if time.monotonic() >= deadline:
            _raise_error(
                "rubbish_state_detection_timeout",
                "no rubbish state was found within three seconds",
                {"last_probe": last_probe, "polls": polls},
            )
        remaining_sec = max(deadline - time.monotonic(), 0.0)
        time.sleep(min(_MATCH_INTERVAL_SEC, remaining_sec))


def _click_match(app: Any, match: Mapping[str, Any], *, error_code: str) -> Dict[str, Any]:
    center = match.get("center")
    if not isinstance(center, list) or len(center) != 2:
        _raise_error(error_code, "matched template did not contain a click center", {"match": dict(match)})
    x, y = int(center[0]), int(center[1])
    app.click(x=x, y=y)
    return {"clicked": True, "x": x, "y": y, "match": dict(match)}


def _enter_rubbish_page(app: Any, vision: Any) -> Dict[str, Any]:
    started_at = time.monotonic()
    entry_clicks: list[Dict[str, Any]] = []
    rounds: list[Dict[str, Any]] = []
    for round_index in range(1, _ENTRY_MAX_ROUNDS + 1):
        entry = _wait_template(
            app,
            vision,
            template=_ENTRY_TEMPLATE,
            region=_ENTRY_REGION,
            timeout_sec=_MATCH_TIMEOUT_SEC,
        )
        if not bool(entry.get("found")):
            _raise_error(
                "rubbish_recycle_entry_not_found",
                "the rubbish recycle entry was not found within three seconds",
                {"round": round_index, "last_match": entry, "rounds": rounds},
            )
        click = _click_match(
            app,
            entry,
            error_code="rubbish_recycle_entry_not_clickable",
        )
        entry_clicks.append(click)
        recheck = _match_template(
            app,
            vision,
            template=_ENTRY_TEMPLATE,
            region=_ENTRY_REGION,
        )
        round_result = {
            "round": round_index,
            "entry": entry,
            "click": click,
            "recheck": recheck,
        }
        rounds.append(round_result)
        logger.info(
            "Rubbish recycle entry round=%s/%s x=%s y=%s before=%.4f remains=%s after=%.4f",
            round_index,
            _ENTRY_MAX_ROUNDS,
            click["x"],
            click["y"],
            float(entry.get("confidence") or 0.0),
            bool(recheck.get("found")),
            float(recheck.get("confidence") or 0.0),
        )
        if not bool(recheck.get("found")):
            return {
                "success": True,
                "entry": entry,
                "entry_clicks": entry_clicks,
                "rounds": rounds,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }
    _raise_error(
        "rubbish_recycle_entry_click_not_effective",
        "the rubbish recycle entry remained visible after three rounds",
        {"rounds": rounds, "entry_clicks": entry_clicks},
    )


def _click_back(app: Any, vision: Any, *, from_page: str) -> Dict[str, Any]:
    match = _wait_template(
        app,
        vision,
        template=_BACK_TEMPLATE,
        region=_BACK_REGION,
        timeout_sec=_MATCH_TIMEOUT_SEC,
    )
    if not bool(match.get("found")):
        _raise_error(
            "rubbish_back_button_not_found",
            "the back button was not found within three seconds",
            {"from_page": from_page, "last_match": match},
        )
    return {
        **_click_match(app, match, error_code="rubbish_back_button_not_clickable"),
        "from_page": from_page,
    }


def _click_recycle_all_until_absent(
    app: Any,
    vision: Any,
    *,
    initial_match: Mapping[str, Any],
) -> Dict[str, Any]:
    attempts: list[Dict[str, Any]] = []
    current_match = dict(initial_match)
    for round_index in range(1, _RECYCLE_MAX_ROUNDS + 1):
        click = _click_match(
            app,
            current_match,
            error_code="rubbish_recycle_button_not_clickable",
        )
        time.sleep(_RECYCLE_RECHECK_DELAY_SEC)
        recheck = _match_template(
            app,
            vision,
            template=_RECYCLE_ALL_TEMPLATE,
            region=_STATE_REGION,
        )
        attempts.append(
            {
                "round": round_index,
                "click": click,
                "recheck": recheck,
            }
        )
        logger.info(
            "Rubbish recycle-all round=%s/%s x=%s y=%s remains=%s confidence=%.4f",
            round_index,
            _RECYCLE_MAX_ROUNDS,
            click["x"],
            click["y"],
            bool(recheck.get("found")),
            float(recheck.get("confidence") or 0.0),
        )
        if not bool(recheck.get("found")):
            return {"success": True, "attempts": attempts}
        current_match = recheck
    _raise_error(
        "rubbish_recycle_all_click_not_effective",
        "the recycle-all button remained visible after five rounds",
        {"attempts": attempts},
    )


def _dismiss_reward_until_empty(app: Any, vision: Any) -> Dict[str, Any]:
    attempts: list[Dict[str, Any]] = []
    while True:
        time.sleep(_REWARD_DISMISS_INTERVAL_SEC)
        app.click(x=_REWARD_DISMISS_POINT[0], y=_REWARD_DISMISS_POINT[1])
        empty_match = _match_template(
            app,
            vision,
            template=_NO_RUBBISH_TEMPLATE,
            region=_STATE_REGION,
        )
        attempts.append(
            {
                "x": _REWARD_DISMISS_POINT[0],
                "y": _REWARD_DISMISS_POINT[1],
                "empty_match": empty_match,
            }
        )
        logger.info(
            "Rubbish reward dismiss attempt=%s x=%s y=%s empty=%s confidence=%.4f",
            len(attempts),
            _REWARD_DISMISS_POINT[0],
            _REWARD_DISMISS_POINT[1],
            bool(empty_match.get("found")),
            float(empty_match.get("confidence") or 0.0),
        )
        if bool(empty_match.get("found")):
            return {"success": True, "attempts": attempts, "empty_match": empty_match}


@action_info(
    name="resonance_pc.execute_rubbish_recycling_from_city_panel",
    public=True,
    read_only=False,
    description="Enter an eligible city's rubbish station, recycle all rubbish once, and return to the city panel.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
)
def resonance_pc_execute_rubbish_recycling_from_city_panel(
    city_name: str,
    location_file_path: str = _LOCATION_PATH,
    app: Any = None,
    vision: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
) -> Dict[str, Any]:
    if app is None or vision is None or resonance_pc_city_shop_data is None:
        raise RuntimeError("app/vision/resonance_pc_city_shop_data services are required")
    city = resolve_rubbish_recycling_city(city_name=city_name)
    if city is None:
        _raise_error(
            "rubbish_city_not_eligible",
            "the current city is not configured for rubbish recycling",
            {"city_name": city_name},
        )
    started_at = time.monotonic()
    point = resonance_pc_city_shop_data.resolve_shop_point(
        city_name=city["city_name"],
        shop_name=city["shop_name"],
        location_file_path=location_file_path,
    )
    logger.info(
        "Rubbish recycling started city_id=%s city=%s station=(%s,%s)",
        city["city_id"],
        city["city_name"],
        point["x"],
        point["y"],
    )
    app.click(x=int(point["x"]), y=int(point["y"]))

    entry_result = _enter_rubbish_page(app, vision)
    entry = dict(entry_result.get("entry") or {})
    entry_clicks = list(entry_result.get("entry_clicks") or [])
    entry_click = entry_clicks[-1] if entry_clicks else None
    time.sleep(_STEP_INTERVAL_SEC)

    initial_state = _wait_rubbish_state(app, vision)
    reward: Optional[Dict[str, Any]] = None
    recycle_click: Optional[Dict[str, Any]] = None
    final_state = initial_state
    status = "empty"
    reason: Optional[str] = "no_rubbish"

    if initial_state["state"] == "has_rubbish":
        recycle_result = _click_recycle_all_until_absent(
            app,
            vision,
            initial_match=initial_state["match"],
        )
        recycle_attempts = list(recycle_result.get("attempts") or [])
        recycle_click = dict((recycle_attempts[-1] or {}).get("click") or {}) if recycle_attempts else None
        reward = _dismiss_reward_until_empty(app, vision)
        final_state = {
            "state": "empty",
            "match": reward.get("empty_match"),
        }
        status = "sold"
        reason = None

    recycle_page_back = _click_back(app, vision, from_page="recycle_page")
    time.sleep(_STEP_INTERVAL_SEC)
    station_menu_back = _click_back(app, vision, from_page="station_menu")
    back_clicks = [recycle_page_back, station_menu_back]
    back_click = station_menu_back
    returned = {
        "success": True,
        "page_state": "city_panel",
        "confirmation": "not_requested",
    }

    result = {
        "success": True,
        "status": status,
        "reason": reason,
        "city_id": city["city_id"],
        "city_name": city["city_name"],
        "initial_state": initial_state["state"],
        "final_state": final_state["state"],
        "reward_overlay_seen": status == "sold",
        "page_state": "city_panel",
        "station": point,
        "entry": entry,
        "entry_click": entry_click,
        "entry_clicks": entry_clicks,
        "entry_rounds": entry_result.get("rounds"),
        "recycle_click": recycle_click,
        "reward": reward,
        "back_click": back_click,
        "back_clicks": back_clicks,
        "returned_city_panel": returned,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }
    logger.info(
        "Rubbish recycling completed city=%s status=%s initial_state=%s final_state=%s elapsed_ms=%s",
        city["city_name"],
        status,
        result["initial_state"],
        result["final_state"],
        result["elapsed_ms"],
    )
    return result
