"""Production staffing observations from native UI templates. Frames are BGR."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np

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
    required = {"room_1_title", "room_2_title", "quick",
                "select_all", "cancel_all", "confirm", "back", "apply", "discard"}
    if not required.issubset(data["templates"]):
        raise ValueError("Incomplete production staffing template catalog")
    for spec in data["templates"].values():
        for variant in (spec, *spec.get("variants", [])):
            rooms._asset(variant["image"])
    return True


def control(image, name):
    spec = catalog()["templates"].get(name)
    if not spec:
        return None
    hits = [{**hit,"anchor_bbox":variant.get("bbox"),"click":variant.get("click")}
            for variant in (spec, *spec.get("variants", []))
            if (hit := rooms.match_asset(image, variant)) is not None]
    if not hits:
        return None
    hit = max(hits, key=lambda value: value["score"])
    if name in {"room_1_title", "room_2_title"}:
        # Rich headers share a large icon/background. Verify the numbered part
        # within the full images as well, so shared pixels cannot identify a room.
        scores = {}
        for key in ("room_1_title", "room_2_title"):
            item = catalog()["templates"][key]
            scores[key] = max(_number_score(image, hit, variant)
                              for variant in (item, *item.get("variants", [])))
        other = "room_2_title" if name == "room_1_title" else "room_1_title"
        if scores[name] < spec["number_min_score"] or scores[name]-scores[other] < spec["number_min_margin"]:
            return None
    return hit


def _number_score(image, hit, spec):
    x,y,w,h = spec["number_region"]
    target = rooms._asset(spec["image"])[y:y+h,x:x+w]
    sx,sy = hit["rect"][0]+x,hit["rect"][1]+y
    source = image[max(0,sy-2):min(720,sy+h+2), max(0,sx-2):min(1280,sx+w+2)]
    if source.shape[0] < h or source.shape[1] < w:
        return -1
    scores = cv2.matchTemplate(cv2.cvtColor(source,cv2.COLOR_BGR2GRAY),
                               cv2.cvtColor(target,cv2.COLOR_BGR2GRAY), cv2.TM_CCOEFF_NORMED)
    scores[~np.isfinite(scores)] = -1
    return float(scores.max())


def point(image, name):
    hit = control(image, name)
    if hit is None:
        return None
    # Native text can be off-center inside the clickable control. Preserve its
    # measured offset when the matching text moves within the search region.
    bbox, click = hit.get("anchor_bbox"),hit.get("click")
    if click is not None and bbox is not None:
        x, y, _, _ = bbox
        return [click[0]+hit["rect"][0]-x,
                click[1]+hit["rect"][1]-y]
    return hit["center"]


def observe(image, room_name):
    if room_name not in ROOMS or not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.shape != (720, 1280, 3):
        return None
    # Foreground modals take precedence over the still-visible page underneath.
    prompt = [control(image, key) for key in ("apply", "discard")]
    if any(prompt):
        return {"page": "save_prompt"} if all(prompt) else None
    all_hit, cancel_hit = control(image, "select_all"), control(image, "cancel_all")
    if all_hit or cancel_hit:
        # Both full blue buttons share most pixels. The better text match must
        # win clearly; ambiguous buttons never authorize either operation.
        if all_hit and cancel_hit:
            if abs(all_hit["score"]-cancel_hit["score"]) < .01:
                return None
            button = "select_all" if all_hit["score"] > cancel_hit["score"] else "cancel_all"
        else:
            button = "select_all" if all_hit else "cancel_all"
        return {"page":"selection", "button":button}
    if control(image, "confirm"):
        return None
    title = "room_1_title" if room_name == ROOMS[0] else "room_2_title"
    if control(image, title) and control(image, "quick"):
        return {"page": "editor"}
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
    # The label identifies the control, but the clickable target requested by
    # the user is the blue icon above it (1280x720 client, real entry screenshot).
    # Keep the offset relative to the matched label rather than its screen origin.
    x, y = hits[-1]["center"]
    return [x, y-50]
