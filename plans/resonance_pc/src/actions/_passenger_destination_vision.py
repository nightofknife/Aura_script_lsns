"""Passenger destination background-plus-badge matching; no masks or OCR."""
from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np

from ._template_observation import capture_template_frame


_PLAN_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = "data/meta/passenger_destination_templates.json"


class DestinationVisionError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.detail = code, detail or {}


@lru_cache(maxsize=1)
def load_catalog():
    try:
        payload = json.loads((_PLAN_ROOT / _CATALOG).read_text(encoding="utf-8"))
        entries = payload["cities"]
        profile = payload["match"]
        width, height = profile["size"]
        threshold = float(profile["threshold"])
        if (profile["method"] != "TM_CCOEFF_NORMED" or profile["preprocess"] != "none"
                or not (0 < threshold <= 1) or width <= 0 or height <= 0):
            raise ValueError("Invalid passenger destination matching profile")
        if len(entries) != 21 or len({row["city_key"] for row in entries}) != 21:
            raise ValueError("Expected 21 distinct passenger city badges")
        catalog = []
        for row in entries:
            template = cv2.imdecode(np.frombuffer((_PLAN_ROOT / row["template"]).read_bytes(), np.uint8),
                                    cv2.IMREAD_COLOR)
            if template is None or template.shape != (height, width, 3):
                raise ValueError(f"Invalid passenger destination template: {row['city_key']}")
            names = {row["city_key"], row["city_name"], *row.get("aliases", [])}
            gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            if np.std(gray) < 1:
                raise ValueError(f"Passenger destination template has no detail: {row['city_key']}")
            gray.setflags(write=False)
            catalog.append({**row, "aliases": tuple(names), "gray": gray,
                            "size": (width, height), "threshold": threshold})
        return tuple(catalog)
    except (OSError, ValueError, TypeError, KeyError, cv2.error) as exc:
        raise DestinationVisionError("passenger_destination_templates_invalid", str(exc)) from exc


def resolve_destination(name):
    target = str(name or "").strip().casefold()
    for row in load_catalog():
        if target and target in {alias.strip().casefold() for alias in row["aliases"]}:
            return row
    raise DestinationVisionError("destination_not_found", "No passenger badge is configured for destination",
                                 {"target": str(name)})


def capture_list(app, region, check_cancelled):
    frame = capture_template_frame(app=app, region=region, check_cancelled=check_cancelled)
    if not frame["valid"]:
        raise DestinationVisionError("capture_failed", "Unable to capture passenger destination list")
    return frame["image"]


def scroll_signature(image):
    """Reduce background animation sensitivity without depending on city labels."""
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    return cv2.resize(gray, (142, 100), interpolation=cv2.INTER_AREA).astype(np.float32)


def list_difference(previous, current):
    return None if previous is None else float(np.mean(np.abs(previous - current)))


def probe_destination(image, entry, region, check_cancelled):
    check_cancelled()
    try:
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        response = cv2.matchTemplate(gray, entry["gray"], cv2.TM_CCOEFF_NORMED)
        check_cancelled()
        # Invalid response positions cannot authorize a click or hide a valid match.
        finite = np.isfinite(response)
        if not np.any(finite):
            raise ValueError("No usable badge match evidence")
        ignored = int(response.size - np.count_nonzero(finite))
        response[~finite] = -1
        _, score, _, location = cv2.minMaxLoc(response)
        found = score >= entry["threshold"]
        result = {"found": found, "confidence": score, "city_key": entry["city_key"],
                  "method": "passenger_city_background_and_badge", "template": entry["template"],
                  "region": list(region), "nonfinite_locations_ignored": ignored}
        if found:
            width, height = entry["size"]
            center = [location[0] + width // 2, location[1] + height // 2]
            if not (0 <= location[0] <= region[2] - width and 0 <= location[1] <= region[3] - height):
                raise ValueError("Destination template is clipped or has invalid coordinates")
            result["center"] = [int(region[0] + center[0]), int(region[1] + center[1])]
        return result
    except Exception as exc:
        check_cancelled()
        raise DestinationVisionError("passenger_destination_match_failed", str(exc)) from exc
