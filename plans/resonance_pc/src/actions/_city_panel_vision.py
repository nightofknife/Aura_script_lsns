"""Small-region city-entry and masked city-badge recognition, without OCR."""
from functools import lru_cache
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger


CITY_REGION = (25, 490, 175, 170)
VISIT_REGION = (1000, 450, 250, 70)
CITY_THRESHOLD = 0.55
VISIT_THRESHOLD = 0.9
POLL_INTERVAL = 0.15
TRANSITION_SEC = 0.3
RETRY_INTERVAL = 2.0
MAX_CLICKS = 4
_PLAN_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = "data/meta/city_identity_templates.json"


class CityPanelVisionError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


def _image(path, *, mask=False):
    encoded = np.frombuffer((_PLAN_ROOT / path).read_bytes(), np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE if mask else cv2.IMREAD_UNCHANGED)
    if image is None or (not mask and (image.ndim != 3 or image.shape[2] != 3)):
        raise ValueError(f"Invalid city identity image: {path}")
    if not mask:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image.setflags(write=False)
    return image


@lru_cache(maxsize=1)
def load_templates():
    try:
        catalog = json.loads((_PLAN_ROOT / _CATALOG).read_text(encoding="utf-8"))
        cities = catalog["cities"]
        if len(cities) != 21 or len({row["city_key"] for row in cities}) != 21:
            raise ValueError("City identity catalog must contain 21 distinct cities")
        templates = [_image(row["template"]) for row in cities]
        masks = [_image(row["mask"], mask=True) for row in cities]
        if any(template.shape != (133, 133, 3) or mask.shape != (133, 133)
               or not np.any(mask) for template, mask in zip(templates, masks)):
            raise ValueError("Invalid city badge dimensions or empty mask")
        visit = _image(catalog["visit_entry"]["template"])
        if visit.shape != (29, 35, 3):
            raise ValueError("City entry template must be an opaque 35x29 RGB image")
        return cities, templates, masks, visit
    except (OSError, ValueError, KeyError, TypeError, cv2.error) as exc:
        raise CityPanelVisionError("city_identity_templates_invalid", str(exc)) from exc


def _capture(app, region, check_cancelled):
    check_cancelled()
    capture = app.capture(rect=region)
    check_cancelled()
    image = getattr(capture, "image", None)
    if (not getattr(capture, "success", False) or not isinstance(image, np.ndarray)
            or image.shape != (region[3], region[2], 3) or image.dtype != np.uint8):
        raise CityPanelVisionError("city_identity_capture_failed", "Unable to capture city identity region",
                                   {"region": list(region)})
    return image


def _score(hit):
    try:
        score = float(hit.confidence)
        return score if math.isfinite(score) and not (getattr(hit, "debug_info", None) or {}).get("error") else None
    except (AttributeError, TypeError, ValueError):
        return None


def probe_city(*, app, vision, check_cancelled):
    check_cancelled()
    cities, templates, masks, _ = load_templates()
    started = time.monotonic()
    image = _capture(app, CITY_REGION, check_cancelled)
    hits = vision.find_templates_batch(
        source_image=image, template_images=templates, mask_images=masks,
        threshold=CITY_THRESHOLD, use_grayscale=True,
        match_method=cv2.TM_CCORR_NORMED, preprocess="edge",
    )
    check_cancelled()
    if not isinstance(hits, (tuple, list)) or len(hits) != len(cities):
        raise CityPanelVisionError("city_identity_match_failed", "Incomplete city template batch")
    valid = [(score, row, hit) for row, hit in zip(cities, hits) if (score := _score(hit)) is not None]
    # Masked normalized correlation can yield NaN/Inf in empty regions. Never rank those.
    score, row, hit = max(valid, key=lambda item: item[0]) if valid else (None, {}, None)
    found = score is not None and score >= CITY_THRESHOLD and bool(hit.found)
    result = {"found": found, "confidence": score,
              "city_key": row.get("city_key") if found else None,
              "city_name": row.get("city_name") if found else None,
              "candidate_city_key": row.get("city_key"), "invalid_candidates": len(cities)-len(valid),
              "method": "masked_city_badge", "region": list(CITY_REGION),
              "elapsed_ms": round((time.monotonic()-started)*1000, 1)}
    logger.debug("[CityIdentity] phase=probe result=%s", result)
    return result


def probe_visit_entry(*, app, vision, check_cancelled):
    check_cancelled()
    template = load_templates()[3]
    image = _capture(app, VISIT_REGION, check_cancelled)
    hit = vision.find_template(source_image=image, template_image=template,
        threshold=VISIT_THRESHOLD, use_grayscale=False,
        match_method=cv2.TM_SQDIFF_NORMED, preprocess="none")
    check_cancelled()
    score = _score(hit)
    found = score is not None and score >= VISIT_THRESHOLD and bool(hit.found)
    center = None
    if found:
        point = getattr(hit, "center_point", None)
        if (not isinstance(point, (tuple, list)) or len(point) != 2
                or not all(math.isfinite(float(value)) for value in point)
                or not (0 <= point[0] < VISIT_REGION[2] and 0 <= point[1] < VISIT_REGION[3])):
            raise CityPanelVisionError("city_entry_match_failed", "Invalid city entry coordinates")
        center = [VISIT_REGION[0]+int(point[0]), VISIT_REGION[1]+int(point[1])]
    return {"found": found, "confidence": score, "center": center, "region": list(VISIT_REGION)}


def _sleep(seconds, check_cancelled):
    check_cancelled()
    time.sleep(max(seconds, 0.0))
    check_cancelled()


def _deadline(started, timeout_sec):
    timeout = float(timeout_sec)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_sec must be finite and non-negative")
    return started + timeout


def _confirmed(match, started):
    result = {**match, "success": True, "page_state": "city_panel", "confirmations": 2,
              "elapsed_ms": round((time.monotonic()-started)*1000, 1)}
    logger.info("[CityIdentity] phase=confirmed city=%s confidence=%s elapsed_ms=%s",
                result["city_name"], result["confidence"], result["elapsed_ms"])
    return result


def wait_city(*, app, vision, check_cancelled, timeout_sec=3.0):
    started = time.monotonic()
    deadline = _deadline(started, timeout_sec)
    previous = None
    while True:
        match = probe_city(app=app, vision=vision, check_cancelled=check_cancelled)
        if match["found"] and match["city_key"] == previous:
            return _confirmed(match, started)
        previous = match["city_key"] if match["found"] else None
        if time.monotonic() >= deadline:
            raise CityPanelVisionError("city_panel_not_confirmed", "City badge did not stabilize", {"match": match})
        _sleep(min(POLL_INTERVAL, max(deadline-time.monotonic(), 0.0)), check_cancelled)


def open_city_panel(*, app, vision, check_cancelled, timeout_sec=12.0):
    started = time.monotonic()
    deadline = _deadline(started, timeout_sec)
    previous = None
    clicks = []
    last_click = -math.inf
    while True:
        match = probe_city(app=app, vision=vision, check_cancelled=check_cancelled)
        if match["found"] and match["city_key"] == previous:
            return {"success": True, "page_state": "city_panel", "city": _confirmed(match, started),
                    "skipped": not clicks, "click_attempts": len(clicks), "clicks": clicks,
                    "click": clicks[0] if clicks else None}
        previous = match["city_key"] if match["found"] else None
        if time.monotonic() >= deadline:
            raise CityPanelVisionError("open_city_panel_failed", "City panel was not confirmed",
                                       {"match": match, "click_attempts": len(clicks), "clicks": clicks})
        if not match["found"] and len(clicks) < MAX_CLICKS and time.monotonic()-last_click >= RETRY_INTERVAL:
            entry = probe_visit_entry(app=app, vision=vision, check_cancelled=check_cancelled)
            if entry["found"]:
                check_cancelled()
                app.click(x=entry["center"][0], y=entry["center"][1])
                check_cancelled()
                last_click = time.monotonic()
                clicks.append({"clicked": True, "center": entry["center"], "match": entry})
                logger.info("[CityIdentity] phase=open_click attempt=%s match=%s", len(clicks), entry)
                _sleep(min(TRANSITION_SEC, max(deadline-time.monotonic(), 0.0)), check_cancelled)
                continue
        _sleep(min(POLL_INTERVAL, max(deadline-time.monotonic(), 0.0)), check_cancelled)
