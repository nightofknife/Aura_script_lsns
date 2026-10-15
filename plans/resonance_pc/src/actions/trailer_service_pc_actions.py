"""One state-confirmed towing submission from an already selected destination."""

from __future__ import annotations

import asyncio
import time

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ._trailer_service_vision import PAYMENT_POINTS, TrailerRecognitionError, TrailerVision, validate_frame
from . import city_travel_pc_actions as travel


_POLL_SECONDS = .2
_STATE_TIMEOUT = 2.
_DEPARTURE_TIMEOUT = 12.
_MAX_SUPPLEMENTAL_CLICKS = 3


class TrailerServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _cancel_check():
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Towing service was cancelled")


def _pause(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _cancel_check()
        time.sleep(min(.1, max(0., deadline - time.monotonic())))
    _cancel_check()


def _capture(app):
    _cancel_check()
    capture = app.capture()
    _cancel_check()
    if not getattr(capture, "success", False):
        raise TrailerServiceError("trailer_capture_failed", "Could not capture the towing page")
    return validate_frame(getattr(capture, "image", None))


def _wait(app, observe, *, timeout=_STATE_TIMEOUT):
    deadline = time.monotonic() + timeout
    previous, consecutive = None, 0
    while True:
        frame = _capture(app)
        value = observe(frame)
        if value is not None:
            key = (value.get("state"), value.get("payment"), value.get("quota"))
            consecutive = consecutive + 1 if key == previous else 1
            previous = key
            if consecutive >= 2:
                return value
        else:
            previous, consecutive = None, 0
        if time.monotonic() >= deadline:
            raise TrailerServiceError("trailer_state_timeout", "Towing page state did not stabilize")
        _pause(min(_POLL_SECONDS, max(0., deadline - time.monotonic())))


def _click(app, point):
    _cancel_check()
    app.click(x=int(point[0]), y=int(point[1]))


def quota_reason(quota: dict, daily_first_n: int) -> str | None:
    if quota["remaining"] == 0:
        return "quota_exhausted"
    if quota["used_today"] >= daily_first_n:
        return "daily_limit_reached"
    return None


def _map_observation(reader, ocr, frame):
    if reader.match(frame, "trailer_prompt")["found"]:
        return None
    entry = reader.match(frame, "trailer_entry")
    if not entry["found"]:
        return None
    try:
        quota = reader.read_quota(frame, ocr)
    except TrailerRecognitionError as exc:
        if exc.code not in {"trailer_quota_unreadable", "trailer_quota_invalid"}:
            raise
        return None
    return {"state": "map", "entry": entry, "quota": quota}


def _prompt_observation(reader, frame):
    prompt = reader.prompt(frame)
    return {"state": "prompt", **prompt} if prompt else None


def _open_prompt(app, ocr, reader, daily_first_n, result):
    for attempt in range(_MAX_SUPPLEMENTAL_CLICKS + 1):
        def source(frame):
            return _prompt_observation(reader, frame) or _map_observation(reader, ocr, frame)
        observed = _wait(app, source)
        if observed["state"] == "prompt":
            return observed
        result["quota"] = observed["quota"]
        reason = quota_reason(observed["quota"], daily_first_n)
        if reason:
            result.update(success=True, status="not_used", reason=reason, page_state="map")
            return None
        _click(app, observed["entry"]["center"])
        result["open_clicks"] += 1
        logger.info("[TrailerService] phase=open attempt=%s quota=%s", attempt + 1, observed["quota"])
        try:
            return _wait(app, lambda frame: _prompt_observation(reader, frame))
        except TrailerServiceError as exc:
            if exc.code != "trailer_state_timeout":
                raise
    raise TrailerServiceError("trailer_open_unconfirmed", "Towing window did not open after three supplemental clicks")


def _select_payment(app, reader, payment, result):
    for attempt in range(_MAX_SUPPLEMENTAL_CLICKS + 1):
        observed = _wait(app, lambda frame: _prompt_observation(reader, frame))
        if observed["payment"] == payment:
            return observed
        _click(app, PAYMENT_POINTS[payment])
        result["payment_clicks"] += 1
        logger.info("[TrailerService] phase=select_payment payment=%s attempt=%s", payment, attempt + 1)
        def selected(frame):
            value = _prompt_observation(reader, frame)
            return value if value and value["payment"] == payment else None
        try:
            return _wait(app, selected)
        except TrailerServiceError as exc:
            if exc.code != "trailer_state_timeout":
                raise
    raise TrailerServiceError("trailer_payment_unconfirmed", "Requested towing payment was not confirmed")


def _departure_observation(reader, frame):
    if reader.match(frame, "trailer_prompt")["found"]:
        return None
    fatigue = reader.match_existing(frame, travel._FATIGUE_PANEL_TEMPLATE,
                                    travel._FATIGUE_PANEL_REGION, .90)
    if fatigue["found"]:
        return {"state": "fatigue_panel"}
    horn = reader.match_existing(frame, travel._TRAVEL_HORN_TEMPLATE,
                                travel._TRAVEL_HORN_REGION, travel._TRAVEL_HORN_THRESHOLD)
    if horn["found"]:
        return {"state": "traveling"}
    station = reader.match_existing(frame, travel._ARRIVAL_BUTTON_TEMPLATE,
                                   travel._ARRIVAL_BUTTON_REGION, .90)
    if station["found"]:
        return {"state": "arrival_ready"}
    return None


@action_info(
    name="resonance_pc.use_trailer_service_from_map",
    public=True,
    read_only=False,
    description="Submit one towing service from the selected destination map; never drive normally or retry payment.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
)
def resonance_pc_use_trailer_service_from_map(daily_first_n: int = 1, payment: str = "gold", *,
                                             app=None, ocr=None, vision=None) -> dict:
    if type(daily_first_n) is not int or daily_first_n < 1:
        raise ValueError("daily_first_n must be a positive integer")
    if payment not in ("gold", "mileage"):
        raise ValueError("payment must be gold or mileage")
    started = time.monotonic()
    result = {"success": False, "status": "blocked", "reason": None, "page_state": "unknown",
              "submitted": False, "departed": False, "daily_first_n": daily_first_n,
              "payment": payment, "quota": None, "open_clicks": 0, "payment_clicks": 0}
    try:
        reader = TrailerVision(vision=vision)
        # A pre-existing modal is not sufficient: the game quota must first be read on the map.
        try:
            initial = _wait(app, lambda frame: _map_observation(reader, ocr, frame))
        except TrailerServiceError as exc:
            if exc.code == "trailer_state_timeout":
                raise TrailerServiceError("trailer_map_or_quota_unconfirmed",
                                          "Selected destination map and quota could not be confirmed") from exc
            raise
        result["quota"] = initial["quota"]
        result["page_state"] = "map"
        reason = quota_reason(initial["quota"], daily_first_n)
        if reason:
            result.update(success=True, status="not_used", reason=reason)
            return result
        prompt = _open_prompt(app, ocr, reader, daily_first_n, result)
        if prompt is None:
            return result
        result["page_state"] = "prompt"
        _select_payment(app, reader, payment, result)
        # Fresh, stable evidence immediately before the only irreversible click.
        def ready(frame):
            value = _prompt_observation(reader, frame)
            return value if value and value["payment"] == payment else None
        ready_prompt = _wait(app, ready)
        _cancel_check()
        result["submitted"] = True
        logger.info("[TrailerService] phase=confirm_once payment=%s quota=%s", payment, result["quota"])
        try:
            _click(app, ready_prompt["confirm"]["center"])
        except Exception as exc:
            raise TrailerServiceError("trailer_submit_uncertain", "Confirmation input outcome is unknown") from exc
        result["page_state"] = "unknown"
        try:
            departure = _wait(app, lambda frame: _departure_observation(reader, frame), timeout=_DEPARTURE_TIMEOUT)
        except TrailerServiceError as exc:
            if exc.code == "trailer_state_timeout":
                raise TrailerServiceError("trailer_departure_unconfirmed",
                                          "Confirmation submitted, but departure was not positively observed") from exc
            raise
        state = departure["state"]
        result["page_state"] = state
        if state == "fatigue_panel":
            result["reason"] = "fatigue_recovery_required"
        else:
            result.update(success=True, status="departed", departed=True, reason="departure_confirmed")
        return result
    except (TrailerRecognitionError, TrailerServiceError) as exc:
        result.update(success=False, status="blocked", reason=exc.code, page_state="unknown",
                      error={"code": exc.code, "message": str(exc)})
        return result
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        logger.info("[TrailerService] phase=finished status=%s reason=%s submitted=%s page=%s elapsed_ms=%s",
                    result["status"], result["reason"], result["submitted"], result["page_state"], result["elapsed_ms"])
