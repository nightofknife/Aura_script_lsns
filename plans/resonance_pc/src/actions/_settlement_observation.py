"""Read-only presence evidence; technical failures never prove absence."""
import math

import numpy as np


def observe_presence(*, app, vision, template, region, threshold, check_cancelled):
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
        if ((getattr(hit, "debug_info", None) or {}).get("error")
                or type(hit.found) not in (bool, np.bool_) or not math.isfinite(score)):
            return unknown("template_match_failed")
    except (AttributeError, TypeError, ValueError):
        return unknown("template_match_failed")
    return {**base, "valid": True, "found": bool(hit.found), "confidence": score}
