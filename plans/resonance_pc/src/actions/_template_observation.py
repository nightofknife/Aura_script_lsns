"""Validated PC template evidence, independent of business success or absence.

Technical failures produce unknown (found=None). Only valid negative evidence
can prove absence. Click callers additionally require a usable in-ROI center.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real

import numpy as np


def observe_template(*, app, vision, template, region, threshold, check_cancelled,
                     require_center=False):
    base = {"template": str(template), "region": list(region)}

    def unknown(reason, error=None):
        result = {**base, "valid": False, "found": None, "confidence": None, "reason": reason}
        if error is not None:
            result["error"] = str(error)
        return result

    check_cancelled()
    try:
        capture = app.capture(rect=tuple(region))
    except Exception as exc:
        check_cancelled()
        return unknown("capture_failed", exc)
    check_cancelled()
    image = getattr(capture, "image", None)
    if (not getattr(capture, "success", False) or not isinstance(image, np.ndarray)
            or image.shape != (region[3], region[2], 3) or image.dtype != np.uint8):
        return unknown("capture_failed")
    try:
        hit = vision.find_template(source_image=image, template_image=str(template),
                                   threshold=float(threshold), use_grayscale=True)
    except Exception as exc:
        check_cancelled()
        return unknown("template_match_failed", exc)
    check_cancelled()
    try:
        score = float(hit.confidence)
        debug = getattr(hit, "debug_info", None)
        if ((debug is not None and (not isinstance(debug, Mapping) or debug.get("error")))
                or type(hit.found) not in (bool, np.bool_) or not math.isfinite(score)):
            return unknown("template_match_failed")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return unknown("template_match_failed")
    result = {**base, "valid": True, "found": bool(hit.found), "confidence": score}
    if result["found"] and require_center:
        center = getattr(hit, "center_point", None)
        try:
            if (len(center) != 2 or any(isinstance(point, (bool, np.bool_))
                                       or not isinstance(point, Real)
                                       or not math.isfinite(float(point)) for point in center)
                    or not (0 <= center[0] < region[2] and 0 <= center[1] < region[3])):
                return unknown("invalid_match_center")
            result["center"] = [int(region[0] + int(center[0])), int(region[1] + int(center[1]))]
        except (TypeError, ValueError, OverflowError):
            return unknown("invalid_match_center")
    return result
