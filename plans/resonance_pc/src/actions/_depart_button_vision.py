"""Shared, small-region main-screen evidence without OCR."""
from functools import lru_cache
import math
from pathlib import Path
import time

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger


REGION = (1120, 620, 160, 100)
THRESHOLD = 0.9
TEMPLATE = "templates/depart_button.png"
POLL_INTERVAL = 0.15
_PLAN_ROOT = Path(__file__).resolve().parents[2]


class DepartButtonError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


@lru_cache(maxsize=1)
def _load_template():
    path = _PLAN_ROOT / TEMPLATE
    try:
        encoded = np.frombuffer(path.read_bytes(), np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        if image is None or image.shape != (42, 106, 3):
            raise ValueError("Departure template must be an opaque 106x42 RGB image")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image.setflags(write=False)
        return image
    except (OSError, ValueError, cv2.error) as exc:
        raise DepartButtonError("depart_template_invalid", str(exc), {"template": TEMPLATE}) from exc


def probe_depart_button(*, app, vision, check_cancelled):
    check_cancelled()
    template = _load_template()
    started = time.monotonic()
    logger.debug("[DepartButton] phase=capture_start region=%s", REGION)
    capture = app.capture(rect=REGION)
    check_cancelled()
    image = getattr(capture, "image", None)
    if (not getattr(capture, "success", False) or not isinstance(image, np.ndarray)
            or image.shape != (REGION[3], REGION[2], 3) or image.dtype != np.uint8):
        raise DepartButtonError("depart_capture_failed", "Unable to capture the departure-button region")
    capture_ms = round((time.monotonic()-started)*1000, 1)
    logger.debug("[DepartButton] phase=capture_done elapsed_ms=%s", capture_ms)
    match_started = time.monotonic()
    logger.debug("[DepartButton] phase=match_start threshold=%s", THRESHOLD)
    hit = vision.find_template(source_image=image, template_image=template,
        threshold=THRESHOLD, use_grayscale=False,
        match_method=cv2.TM_SQDIFF_NORMED, preprocess="none")
    check_cancelled()
    try:
        score = float(hit.confidence)
        if not math.isfinite(score) or (getattr(hit, "debug_info", None) or {}).get("error"):
            raise ValueError("Invalid departure matching result")
        found = bool(hit.found) and score >= THRESHOLD
        center = None
        if found:
            point = hit.center_point
            if (not isinstance(point, (tuple, list)) or len(point) != 2
                    or not all(math.isfinite(float(value)) for value in point)
                    or not (0 <= point[0] < REGION[2] and 0 <= point[1] < REGION[3])):
                raise ValueError("Invalid departure match coordinates")
            center = [REGION[0]+int(point[0]), REGION[1]+int(point[1])]
    except (AttributeError, TypeError, ValueError) as exc:
        raise DepartButtonError("depart_template_match_failed", str(exc)) from exc
    result = {"found": found, "confidence": score, "center": center,
              "region": list(REGION), "template": TEMPLATE,
              "capture_ms": capture_ms, "match_ms": round((time.monotonic()-match_started)*1000, 1)}
    logger.info("[DepartButton] phase=probe found=%s confidence=%.4f capture_ms=%s match_ms=%s",
                found, score, result["capture_ms"], result["match_ms"])
    return result


def wait_depart_button(*, app, vision, check_cancelled, timeout_sec=5.0, stable_matches=2):
    started = time.monotonic()
    deadline = started+max(float(timeout_sec), 0.)
    confirmations = 0
    required = max(int(stable_matches), 2)
    while True:
        match = probe_depart_button(app=app, vision=vision, check_cancelled=check_cancelled)
        confirmations = confirmations+1 if match["found"] else 0
        confirmed = confirmations >= required
        if confirmed or time.monotonic() >= deadline:
            result = {"confirmed": confirmed, "match": match, "confirmations": confirmations,
                      "elapsed_ms": round((time.monotonic()-started)*1000, 1)}
            logger.info("[DepartButton] phase=main_ready confirmed=%s confirmations=%s elapsed_ms=%s",
                        confirmed, confirmations, result["elapsed_ms"])
            return result
        check_cancelled()
        time.sleep(min(POLL_INTERVAL, max(deadline-time.monotonic(), 0.)))
        check_cancelled()
