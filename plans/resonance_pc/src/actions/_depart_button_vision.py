"""Shared, small-region main-screen evidence without OCR."""
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger


POLL_INTERVAL = 0.15
_PLAN_ROOT = Path(__file__).resolve().parents[2]


class DepartButtonError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


@dataclass(frozen=True)
class DepartureTemplateProfile:
    template: str
    size: tuple[int, int]
    region: tuple[int, int, int, int]
    threshold: float
    use_grayscale: bool
    match_method: int
    preprocess: str


@lru_cache(maxsize=4)
def _load_profile(plan_root: Path) -> DepartureTemplateProfile:
    try:
        catalog = json.loads((plan_root / "data/meta/depart_button_template.json").read_text(encoding="utf-8"))
        if catalog["schema_version"] != 1 or catalog["mode"] != "RGB":
            raise ValueError("Unsupported departure template schema or image mode")
        reference = catalog["template"]
        path = (plan_root / reference).resolve()
        if not isinstance(reference, str) or not reference.startswith("templates/") or not path.is_relative_to(plan_root / "templates"):
            raise ValueError("Departure template must remain inside plan templates")
        size, match = catalog["size"], catalog["match"]
        region = match["region"]
        if (not isinstance(size, list) or len(size) != 2
                or any(type(value) is not int or value <= 0 for value in size)
                or not isinstance(region, list) or len(region) != 4
                or any(type(value) is not int for value in region)
                or min(region[:2]) < 0 or min(region[2:]) <= 0
                or region[0]+region[2] > 1280 or region[1]+region[3] > 720
                or size[0] > region[2] or size[1] > region[3]):
            raise ValueError("Invalid departure template size or capture region")
        threshold = match["threshold"]
        if (type(threshold) not in (int, float) or not math.isfinite(threshold)
                or not 0 <= threshold <= 1 or match["use_grayscale"] is not False
                or match["mask"] is not False
                or match["preprocess"] != "none"):
            raise ValueError("Invalid departure matching configuration")
        method = match["method"]
        # The Black Moon frame observer shares this RGB/SQDIFF contract.
        # Reject unsupported edits instead of letting its scorer drift from this probe.
        if method != "TM_SQDIFF_NORMED":
            raise ValueError("Departure observers require RGB TM_SQDIFF_NORMED without preprocessing")
        return DepartureTemplateProfile(reference, tuple(size), tuple(region), float(threshold),
                                        match["use_grayscale"], getattr(cv2, method), match["preprocess"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DepartButtonError("depart_template_invalid", str(exc),
                                {"catalog": "data/meta/depart_button_template.json"}) from exc


def load_depart_profile() -> DepartureTemplateProfile:
    return _load_profile(_PLAN_ROOT.resolve())


def __getattr__(name):
    # Retain existing imports without introducing import-time catalog IO here.
    field = {"REGION": "region", "THRESHOLD": "threshold", "TEMPLATE": "template"}.get(name)
    if field is None:
        raise AttributeError(name)
    return getattr(load_depart_profile(), field)


@lru_cache(maxsize=1)
def _load_template():
    profile = load_depart_profile()
    path = _PLAN_ROOT / profile.template
    try:
        encoded = np.frombuffer(path.read_bytes(), np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        if image is None or image.shape != (profile.size[1], profile.size[0], 3):
            raise ValueError(f"Departure template must be an opaque {profile.size[0]}x{profile.size[1]} RGB image")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image.setflags(write=False)
        return image
    except (OSError, ValueError, cv2.error) as exc:
        raise DepartButtonError("depart_template_invalid", str(exc), {"template": profile.template}) from exc


def probe_depart_button(*, app, vision, check_cancelled):
    check_cancelled()
    profile = load_depart_profile()
    region = profile.region
    template = _load_template()
    started = time.monotonic()
    logger.debug("[DepartButton] phase=capture_start region=%s", region)
    capture = app.capture(rect=region)
    check_cancelled()
    image = getattr(capture, "image", None)
    if (not getattr(capture, "success", False) or not isinstance(image, np.ndarray)
            or image.shape != (region[3], region[2], 3) or image.dtype != np.uint8):
        raise DepartButtonError("depart_capture_failed", "Unable to capture the departure-button region")
    capture_ms = round((time.monotonic()-started)*1000, 1)
    logger.debug("[DepartButton] phase=capture_done elapsed_ms=%s", capture_ms)
    match_started = time.monotonic()
    logger.debug("[DepartButton] phase=match_start threshold=%s", profile.threshold)
    hit = vision.find_template(source_image=image, template_image=template,
        threshold=profile.threshold, use_grayscale=profile.use_grayscale,
        match_method=profile.match_method, preprocess=profile.preprocess)
    check_cancelled()
    try:
        score = float(hit.confidence)
        if not math.isfinite(score) or (getattr(hit, "debug_info", None) or {}).get("error"):
            raise ValueError("Invalid departure matching result")
        found = bool(hit.found) and score >= profile.threshold
        center = None
        if found:
            point = hit.center_point
            if (not isinstance(point, (tuple, list)) or len(point) != 2
                    or not all(math.isfinite(float(value)) for value in point)
                    or not (0 <= point[0] < region[2] and 0 <= point[1] < region[3])):
                raise ValueError("Invalid departure match coordinates")
            center = [region[0]+int(point[0]), region[1]+int(point[1])]
    except (AttributeError, TypeError, ValueError) as exc:
        raise DepartButtonError("depart_template_match_failed", str(exc)) from exc
    result = {"found": found, "confidence": score, "center": center,
              "region": list(region), "template": profile.template,
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
