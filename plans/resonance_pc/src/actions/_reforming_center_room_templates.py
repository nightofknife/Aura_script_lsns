"""Screenshot and native game templates; all input images are BGR. No OCR."""
from __future__ import annotations

import json
import math
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

_PLAN = Path(__file__).resolve().parents[2]
_ROOM_ROI = ContextVar("reforming_center_room_roi", default=None)


@lru_cache(maxsize=1)
def load_catalog():
    return json.loads((_PLAN / "data/meta/reforming_center_templates_pc.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=160)
def _asset(relative_path):
    path = (_PLAN / relative_path).resolve()
    if not path.is_relative_to(_PLAN.resolve()):
        raise ValueError("Template path must remain inside this plan")
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Template not readable: {relative_path}")
    return image


def match_asset(image, spec, region=None):
    region = region or spec.get("search_region") or [0, 0, 1280, 720]
    x, y, w, h = map(int, region)
    x, y = max(x, 0), max(y, 0)
    w, h = min(w, 1280-x), min(h, 720-y)
    target = _asset(spec["image"])
    source = image[y:y+h, x:x+w, :3]
    # Preserve antialiased gray edges. Hard white-pixel thresholding rejected
    # readable live warehouse titles (0.865/0.876 vs >=0.92 required).
    target = cv2.cvtColor(target, cv2.COLOR_BGR2GRAY)
    context = _ROOM_ROI.get()
    if context is not None and context[0] is image:
        gray_rois = context[1]
        key = (x, y, w, h)
        if key not in gray_rois:
            gray_rois[key] = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
        source = gray_rois[key]
    else:
        source = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    th, tw = target.shape[:2]
    if h < th or w < tw or float(target.std()) < 1:
        return None
    scores = cv2.matchTemplate(source, target, cv2.TM_CCOEFF_NORMED)
    scores[~np.isfinite(scores)] = -1
    _, score, _, location = cv2.minMaxLoc(scores)
    if not math.isfinite(score) or score < float(spec.get("threshold", .92)):
        return None
    return {"score": float(score), "rect": [x+location[0], y+location[1], tw, th],
            "center": [x+location[0]+tw//2, y+location[1]+th//2]}


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    inter = max(0, min(ax+aw, bx+bw)-max(ax, bx)) * max(0, min(ay+ah, by+bh)-max(ay, by))
    return inter / max(1, min(aw*ah, bw*bh))


def room_hits(image, catalog, region=(30, 90, 1250, 490)):
    # Bind only this observation. Nested calls restore the outer observation;
    # exceptions never leave a frame or its gray pixels bound to the caller.
    token = _ROOM_ROI.set((image, {}))
    try:
        return _room_hits(image, catalog, region)
    finally:
        _ROOM_ROI.reset(token)


def _room_hits(image, catalog, region):
    matches = []
    for room, spec in catalog["rooms"].items():
        variants = [spec["map_title"], *spec.get("map_title_variants", [])]
        hits = [hit for asset in variants
                if (hit := match_asset(image, asset, region)) is not None]
        if hits:
            matches.append({**max(hits, key=lambda hit: hit["score"]), "room_name": room})
    # Similar names must win against other room labels at the SAME position,
    # not against rooms elsewhere on screen.
    return [hit for hit in matches if not any(
        other["room_name"] != hit["room_name"] and _overlap(hit["rect"], other["rect"]) > .65
        and other["score"] >= hit["score"]-.015 for other in matches)]


def selected_rectangles(image):
    hsv = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (15, 150, 160), (40, 255, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rectangles = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if 250 <= w <= 900 and 100 <= h <= 350 and 80 <= y < 580:
            patch = mask[y:y+h, x:x+w] > 0
            if min(patch[:8].any(axis=0).mean(), patch[-8:].any(axis=0).mean(),
                   patch[:, :8].any(axis=1).mean(), patch[:, -8:].any(axis=1).mean()) >= .65:
                rectangles.append([x, y, w, h])
    return rectangles


def entry_button(image, room_spec, button_spec):
    for x, y, w, h in selected_rectangles(image):
        title = match_asset(image, room_spec["selected_title"], (x, y, w, 65))
        if not title:
            continue
        button = match_asset(image, button_spec, (x+w, max(80, y-20), min(260, 1280-x-w), h+40))
        if button:
            return button
    return None


def destination_confirmed(image, spec):
    if spec.get("room_key") in {"植物纤维原料车间", "动物纤维原料车间", "缝纫工具原料车间", "人造纤维原料车间"}:
        # Lazy import avoids a module cycle. A running work page deliberately
        # lacks the choose-prisoners marker required by the old idle-only rule.
        from . import _raw_workshop_ui
        return _raw_workshop_ui.room_ready(image, spec["room_key"])
    markers = spec.get("destination_markers", [])
    return len(markers) >= 2 and all(match_asset(image, marker) for marker in markers)
