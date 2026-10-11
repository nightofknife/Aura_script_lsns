"""Confirm a shop menu, retrying entry only on the positively identified original city."""
import math
import time
from collections.abc import Mapping

import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from ._city_panel_vision import CityPanelVisionError, probe_city

TIMEOUT_SEC = 15.0
POLL_INTERVAL_SEC = 0.3
RETRY_INTERVAL_SEC = 3.0
MAX_CLICKS = 3
STABLE_MATCHES = 2


class CityShopEntryError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


def valid_probe(hit):
    if not isinstance(hit, Mapping) or type(hit.get("found")) is not bool:
        return False
    try:
        return (hit.get("valid") is not False and not hit.get("error") and not hit.get("reason")
                and math.isfinite(float(hit["confidence"])))
    except (KeyError, TypeError, ValueError):
        return False


def probe_menu_template(*, app, vision, template, region, threshold, check_cancelled):
    """Keep technical failures distinct from a valid target-not-present observation."""
    check_cancelled()
    capture = app.capture(rect=tuple(region))
    check_cancelled()
    image = getattr(capture, "image", None)
    if (not getattr(capture, "success", False) or not isinstance(image, np.ndarray)
            or image.shape != (region[3], region[2], 3) or image.dtype != np.uint8):
        raise CityShopEntryError("city_shop_target_capture_failed", "Unable to capture shop menu region",
                                 {"template": str(template), "region": list(region)})
    hit = vision.find_template(source_image=image, template_image=str(template),
                              threshold=threshold, use_grayscale=True)
    check_cancelled()
    error = (getattr(hit, "debug_info", None) or {}).get("error")
    try:
        score = float(hit.confidence)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CityShopEntryError("city_shop_target_match_failed", "Invalid shop menu match result") from exc
    if error or type(getattr(hit, "found", None)) not in (bool, np.bool_):
        raise CityShopEntryError("city_shop_target_match_failed", "Shop menu matching failed",
                                 {"error": str(error), "template": str(template)})
    return {"valid": math.isfinite(score), "found": bool(hit.found) and math.isfinite(score),
            "confidence": score if math.isfinite(score) else None,
            "template": str(template), "region": list(region)}


def enter_city_shop(*, app, vision, point, entry_type, page_state, target_probe, click, check_cancelled):
    city_key = point.get("city_key")
    if not isinstance(city_key, str) or not city_key:
        raise CityShopEntryError("city_shop_entry_invalid", "Shop entry requires an original city key")
    started = time.monotonic()
    deadline = started + TIMEOUT_SEC
    last_click = -math.inf
    clicks, polls, target_streak, city_streak = 0, 0, 0, 0
    target, city = {}, {}

    def details():
        return {"city_key": city_key, "city_name": point.get("city_name"), "entry_type": entry_type,
                "point": dict(point), "click_attempts": clicks, "polls": polls,
                "stable_matches": target_streak, "city_stable_matches": city_streak,
                "last_match": target, "last_city_match": city,
                "elapsed_sec": round(time.monotonic() - started, 3), "timeout_sec": TIMEOUT_SEC}

    def target_observation():
        check_cancelled()
        result = target_probe()
        check_cancelled()
        return result

    while time.monotonic() < deadline:
        check_cancelled()
        polls += 1
        target = target_observation()
        target_valid = valid_probe(target)
        target_present = target_valid and target["found"]
        target_streak = target_streak + 1 if target_present else 0
        if time.monotonic() >= deadline:
            break
        if target_present:
            city_streak = 0
            reason = "confirming_menu"
            if target_streak >= STABLE_MATCHES:
                result = {**details(), "success": True, "page_state": page_state, "match": target}
                logger.info("[CityShopEntry] phase=confirmed result=%s", result)
                return result
        elif not target_valid:
            city_streak = 0
            reason = "target_unknown"
        else:
            try:
                city = probe_city(app=app, vision=vision, check_cancelled=check_cancelled)
            except CityPanelVisionError as exc:
                raise CityShopEntryError(exc.code, exc.message, {**details(), **exc.detail}) from exc
            check_cancelled()
            original_city = valid_probe(city) and city["found"] and city.get("city_key") == city_key
            city_streak = city_streak + 1 if original_city else 0
            reason = "confirming_original_city" if original_city else "city_unknown_or_different"
            if clicks >= MAX_CLICKS:
                reason = "max_clicks_waiting"
            elif time.monotonic() - last_click < RETRY_INTERVAL_SEC:
                reason = "click_cooldown"
            elif city_streak >= STABLE_MATCHES and time.monotonic() < deadline:
                # City recognition may take time: do not click using a stale menu-absence probe.
                target = target_observation()
                if valid_probe(target) and target["found"]:
                    target_streak, city_streak = 1, 0
                    reason = "confirming_menu"
                elif not valid_probe(target):
                    city_streak = 0
                    reason = "target_unknown"
                elif time.monotonic() < deadline:
                    check_cancelled()
                    click()
                    check_cancelled()
                    clicks += 1
                    last_click = time.monotonic()
                    city_streak = 0
                    reason = "entry_clicked"
        logger.info("[CityShopEntry] phase=poll reason=%s cooldown_remaining=%.3f detail=%s",
                    reason, max(0.0, RETRY_INTERVAL_SEC - (time.monotonic() - last_click)), details())
        check_cancelled()
        time.sleep(min(POLL_INTERVAL_SEC, max(0.0, deadline - time.monotonic())))
        check_cancelled()
    detail = details()
    logger.warning("[CityShopEntry] phase=timeout detail=%s", detail)
    raise CityShopEntryError("city_shop_entry_timeout", "Shop menu was not confirmed before entry timeout", detail)
