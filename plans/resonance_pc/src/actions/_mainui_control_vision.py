"""One fixed-size matcher for shared 1280x720 main-screen controls.

Frame observers and capture observers use the same pixels, ROI and decision.
An unavailable observation is unknown, never evidence that a control vanished.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from ._template_observation import capture_template_frame


PLAN_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MainUiControlProfile:
    template: str
    size: tuple[int, int]
    region: tuple[int, int, int, int]
    threshold: float
    use_grayscale: bool
    match_method: int
    preprocess: str


@lru_cache(maxsize=4)
def load_control_profile(control: str) -> MainUiControlProfile:
    catalog = json.loads((PLAN_ROOT / "data/meta/mainui_control_templates.json").read_text(encoding="utf-8"))
    if catalog["schema_version"] != 1 or catalog["reference_client"] != [1280, 720]:
        raise ValueError("Unsupported main-screen control catalog")
    spec = catalog["controls"][control]
    if "catalog" in spec:
        reference = spec["catalog"]
        path = (PLAN_ROOT / reference).resolve()
        if not isinstance(reference, str) or not path.is_relative_to(PLAN_ROOT / "data/meta"):
            raise ValueError("Control catalog must remain inside plan metadata")
        spec = json.loads(path.read_text(encoding="utf-8"))
        if spec["schema_version"] != 1 or spec["mode"] != "RGB":
            raise ValueError("Unsupported control template catalog")
    reference, size, match = spec["template"], spec["size"], spec["match"]
    path = (PLAN_ROOT / reference).resolve()
    if not isinstance(reference, str) or not reference.startswith("templates/") or not path.is_relative_to(PLAN_ROOT / "templates"):
        raise ValueError("Control template must remain inside plan templates")
    region = match["region"]
    if (not isinstance(size, list) or len(size) != 2
            or any(type(v) is not int or v <= 0 for v in size)
            or not isinstance(region, list) or len(region) != 4
            or any(type(v) is not int for v in region)
            or min(region[:2]) < 0 or min(region[2:]) <= 0
            or region[0] + region[2] > 1280 or region[1] + region[3] > 720
            or size[0] > region[2] or size[1] > region[3]):
        raise ValueError("Invalid control dimensions or capture region")
    threshold = match["threshold"]
    if (type(threshold) not in (int, float) or not math.isfinite(threshold)
            or not 0 <= threshold <= 1 or type(match["use_grayscale"]) is not bool
            or match["preprocess"] != "none" or match["mask"] is not False):
        raise ValueError("Unsupported control matching options")
    if match["method"] not in ("TM_SQDIFF_NORMED", "TM_CCOEFF_NORMED"):
        raise ValueError("Unsupported control matching method")
    return MainUiControlProfile(reference, tuple(size), tuple(region), float(threshold),
                                match["use_grayscale"], getattr(cv2, match["method"]), "none")


@lru_cache(maxsize=4)
def _load_pixels(control: str):
    profile = load_control_profile(control)
    raw = cv2.imdecode(np.frombuffer((PLAN_ROOT / profile.template).read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    if raw is None or raw.shape != (profile.size[1], profile.size[0], 3):
        raise ValueError(f"Invalid control template pixels: {profile.template}")
    pixels = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
    if profile.use_grayscale:
        pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    if profile.match_method == cv2.TM_CCOEFF_NORMED and not np.any(np.std(pixels.astype(np.float32), axis=(0, 1)) > 0):
        raise ValueError("Constant control template cannot establish correlation")
    pixels.setflags(write=False)
    return pixels


def _unknown(control, reason, *, profile=None, error=None):
    result = {"control": control, "valid": False, "found": None, "confidence": None,
              "center": None, "reason": reason, "capture_ms": None, "match_ms": None}
    if profile is not None:
        result.update(template=profile.template, region=list(profile.region))
    if error is not None:
        result["error"] = str(error)
    return result


def _match_region(image, control, profile):
    started = time.monotonic()
    try:
        template = _load_pixels(control)
    except (OSError, ValueError, KeyError, TypeError, cv2.error) as exc:
        return _unknown(control, "template_load_failed", profile=profile, error=exc)
    try:
        source = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if profile.use_grayscale else image
        response = cv2.matchTemplate(np.ascontiguousarray(source), template, profile.match_method)
        if not response.size or not np.isfinite(response).all():
            raise ValueError("Control match produced non-finite evidence")
        minimum, maximum, min_point, max_point = cv2.minMaxLoc(response)
        if profile.match_method == cv2.TM_SQDIFF_NORMED:
            score, point = 1.0 - minimum, min_point
        else:
            score, point = maximum, max_point
        found = score >= profile.threshold
        center = ([profile.region[0] + point[0] + profile.size[0] // 2,
                   profile.region[1] + point[1] + profile.size[1] // 2] if found else None)
        return {"control": control, "valid": True, "found": found, "confidence": float(score),
                "center": center, "region": list(profile.region), "template": profile.template,
                "reason": None, "capture_ms": None,
                "match_ms": round((time.monotonic() - started) * 1000, 1)}
    except (ValueError, TypeError, cv2.error) as exc:
        return _unknown(control, "template_match_failed", profile=profile, error=exc)


def match_mainui_control_frame(frame, control):
    """Match one current full-client RGB frame, without another capture."""
    try:
        profile = load_control_profile(control)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _unknown(control, "template_load_failed", error=exc)
    if not isinstance(frame, np.ndarray) or frame.shape != (720, 1280, 3) or frame.dtype != np.uint8:
        return _unknown(control, "invalid_frame", profile=profile)
    x, y, width, height = profile.region
    return _match_region(frame[y:y + height, x:x + width], control, profile)


def probe_mainui_control(*, app, control, check_cancelled):
    """Capture only the configured ROI, then use the shared matching decision."""
    check_cancelled()
    try:
        profile = load_control_profile(control)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _unknown(control, "template_load_failed", error=exc)
    started = time.monotonic()
    frame = capture_template_frame(app=app, region=profile.region, check_cancelled=check_cancelled)
    capture_ms = round((time.monotonic() - started) * 1000, 1)
    if frame["valid"] is not True:
        result = _unknown(control, "capture_failed", profile=profile, error=frame.get("error"))
        result["capture_ms"] = capture_ms
        return result
    result = _match_region(frame["image"], control, profile)
    check_cancelled()
    result["capture_ms"] = capture_ms
    return result
