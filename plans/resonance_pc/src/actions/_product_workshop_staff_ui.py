"""Production staffing observations from native UI templates. Frames are BGR."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np

from . import _raw_workshop_digits as digits
from . import _raw_workshop_ui as raw_ui
from . import _reforming_center_room_templates as rooms

PLAN = Path(__file__).resolve().parents[2]
ROOMS = ("1号生产车间", "2号生产车间")


@lru_cache(maxsize=1)
def catalog():
    return json.loads((PLAN / "data/meta/product_workshop_staff_templates_pc.json").read_text(encoding="utf-8"))


def validate_assets():
    data = catalog()
    if data.get("reference_client") != [1280, 720]:
        raise ValueError("Production staffing requires 1280x720 templates")
    required = {"room_1_title", "room_2_title", "quick", "selection_marker",
                "select_all", "cancel_all", "confirm", "back", "save_prompt", "apply", "discard"}
    if not required.issubset(data["templates"]):
        raise ValueError("Incomplete production staffing template catalog")
    for spec in data["templates"].values():
        rooms._asset(spec["image"])
    for style in ("list", "selection"):
        digits.glyphs(style)
    _editor_bank()
    for name in ("selection", "editor"):
        x, y, w, h = data["counters"][name]["region"]
        if not (0 <= x < x+w <= 1280 and 0 <= y < y+h <= 720):
            raise ValueError("Counter region outside client")
    return True


def control(image, name):
    spec = catalog()["templates"].get(name)
    return rooms.match_asset(image, spec) if spec else None


def point(image, name):
    hit = control(image, name)
    if hit is None:
        return None
    spec = catalog()["templates"][name]
    # Native text can be off-center inside the clickable control. Preserve its
    # measured offset when the matching text moves within the search region.
    if "click" in spec and "bbox" in spec:
        x, y, _, _ = spec["bbox"]
        return [spec["click"][0]+hit["rect"][0]-x,
                spec["click"][1]+hit["rect"][1]-y]
    return hit["center"]


def _integer(image, spec):
    x, y, w, h = spec["region"]
    ink = digits.foreground(image[y:y+h, x:x+w])
    boxes = digits.segment(ink) if ink is not None else []
    if not 1 <= len(boxes) <= 2:
        return None
    values = [_editor_digit(ink, box) for box in boxes]
    if any(value is None or value["character"] not in "0123456789"
           or value["score"] < .75 or value["margin"] < .04 for value in values):
        return None
    text = "".join(value["character"] for value in values)
    if len(text) > 1 and text.startswith("0"):
        return None
    return int(text)


@lru_cache(maxsize=1)
def _editor_bank():
    values = []
    for item in catalog()["editor_digits"]:
        path = (PLAN / item["image"]).resolve()
        if not path.is_relative_to(PLAN.resolve()):
            raise ValueError("Digit path must stay inside the plan")
        ink = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE)
        boxes = digits.segment(ink) if ink is not None else []
        if len(boxes) != 1:
            raise ValueError("Expected one native editor digit")
        sample = digits.normalize(ink, boxes[0])
        if sample is None:
            raise ValueError("Invalid native editor digit")
        values.append((item["character"], sample))
    if {key for key, _ in values} != set("0123456789"):
        raise ValueError("Incomplete editor digit bank")
    return values


def _editor_digit(ink, box):
    sample = digits.normalize(ink, box)
    if sample is None:
        return None
    scores = {}
    for letter, target in _editor_bank():
        score = max(float(2*np.minimum(sample, np.roll(target, (dy, dx), axis=(0, 1))).sum()/max(float(sample.sum()+target.sum()), 1e-6)) for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        scores[letter] = max(scores.get(letter, 0), score)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return {"character": ranked[0][0], "score": ranked[0][1], "margin": ranked[0][1]-ranked[1][1]}


def observe(image, room_name):
    if room_name not in ROOMS or not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.shape != (720, 1280, 3):
        return None
    # Foreground modals take precedence over the still-visible page underneath.
    prompt = [control(image, key) for key in ("save_prompt", "apply", "discard")]
    if any(prompt):
        return {"page": "save_prompt"} if all(prompt) else None
    if control(image, "selection_marker"):
        if not control(image, "confirm"):
            return None
        spec = catalog()["counters"]["selection"]
        count = digits.read_fraction(image, spec["region"], style=spec.get("style", "selection"))
        if count is None:
            return {"page": "selection", "count": None, "button": None}
        # A cancel button alone is never evidence of a full list.
        all_hit, cancel_hit = control(image, "select_all"), control(image, "cancel_all")
        button = "select_all" if all_hit and not cancel_hit else "cancel_all" if cancel_hit and not all_hit else None
        return {"page": "selection", "count": count["current"], "capacity": count["capacity"], "button": button}
    title = "room_1_title" if room_name == ROOMS[0] else "room_2_title"
    if control(image, title) and control(image, "quick"):
        count = _integer(image, catalog()["counters"]["editor"])
        return {"page": "editor", "count": count}
    spec = rooms.load_catalog()["rooms"][room_name]
    if rooms.destination_confirmed(image, spec):
        return {"page": "product_workshop"}
    if raw_ui.overview_state(image):
        return {"page": "reforming_center_overview"}
    return None


def edit_point(image, room_name):
    markers = rooms.load_catalog()["rooms"][room_name]["destination_markers"]
    hits = [rooms.match_asset(image, spec) for spec in markers]
    # Navigation identifies the numbered room; its inner title is shared.
    if len(hits) < 2 or any(hit is None for hit in hits):
        return None
    return hits[-1]["center"]
