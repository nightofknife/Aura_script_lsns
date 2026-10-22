"""Pure observations for production collection; native 1280x720 assets, BGR frames.

No input or task execution lives here. Matching owns immutable collection assets.
Synthetic card positions are provenance only; runtime positions come from hits.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np

from . import _reforming_center_room_templates as rooms
from . import _product_collection_matching as matching

PLAN = Path(__file__).resolve().parents[2]
ROOMS = tuple(f"{number}号生产车间" for number in range(1, 7))


@lru_cache(maxsize=1)
def catalog():
    return json.loads((PLAN / "data/meta/product_collection_templates_pc.json").read_text(encoding="utf-8"))


def validate_assets():
    data = catalog()
    if data.get("reference_client") != [1280, 720]:
        raise ValueError("Production collection requires 1280x720 templates")
    required = {"navigation_popup_title", "navigation_button_label", "back",
                "collect_all_text", "collect_all_icon", "collectable_badge",
                "collection_complete", "collection_complete_title",
                "collection_complete_icon", "exit_hint", "capacity_warning_text",
                "capacity_cancel", "workshop_edit_marker", "locked_hint"}
    for number in range(1, 7):
        required.add(f"room_{number}_title")
        required.update(f"navigation_{number}_{state}_identity" for state in ("open", "selected", "locked"))
        required.update(f"number_{number}_{color}" for color in ("white", "gray"))
    if not required.issubset(data.get("templates", {})) or not data.get("navigation_viewport"):
        raise ValueError("Incomplete production collection template catalog")
    # A fresh preflight scope still checks every file/version on every call;
    # sibling PNGs share only this call's canonical directory resolution.
    with matching.observe_frame(None):
        for spec in data["templates"].values():
            for variant in (spec, *spec.get("variants", [])):
                matching.validate_asset(variant["image"])
    return True


def _frame(frame):
    return isinstance(frame, np.ndarray) and frame.dtype == np.uint8 and frame.shape == (720, 1280, 3)


def _hit(frame, name, region=None):
    if not _frame(frame):
        return None
    spec = catalog()["templates"].get(name)
    return matching.match_asset(frame, spec, region) if spec else None


def _clip(rect):
    x, y, w, h = map(int, rect)
    right, bottom = min(1280, x+w), min(720, y+h)
    x, y = max(0, x), max(0, y)
    return [x, y, max(0, right-x), max(0, bottom-y)]


def _number_score(frame, anchor, spec):
    """Compare the numbered region at this anchor, not elsewhere on screen."""
    x, y, w, h = spec["number_region"]
    target = matching.gray_asset(spec["image"], frame=frame)[y:y+h, x:x+w]
    ax, ay = anchor["rect"][:2]
    sx, sy, sw, sh = _clip([ax+x-1, ay+y-1, w+2, h+2])
    source = frame[sy:sy+sh, sx:sx+sw]
    if source.shape[0] < h or source.shape[1] < w:
        return -1.0
    scores = cv2.matchTemplate(matching.gray_region(frame, (sx, sy, sw, sh)),
                               target, cv2.TM_CCOEFF_NORMED)
    scores[~np.isfinite(scores)] = -1
    return float(scores.max())


@matching.frame_observation
def _title(frame, number):
    spec = catalog()["templates"][f"room_{number}_title"]
    hit = _hit(frame, f"room_{number}_title")
    if hit is None:
        return None
    scores = {n: _number_score(frame, hit, catalog()["templates"][f"room_{n}_title"])
              for n in range(1, 7)}
    if scores[number] < spec["number_min_score"]:
        return None
    if scores[number] - max(score for n, score in scores.items() if n != number) < spec["number_min_margin"]:
        return None
    return hit


def navigation_open(frame):
    return _hit(frame, "navigation_popup_title") is not None


@matching.frame_observation
def navigation_region(frame):
    """The real Viewport relative to the popup title, allowing popup movement."""
    hit = _hit(frame, "navigation_popup_title")
    if hit is None:
        return None
    dx, dy, w, h = catalog()["navigation_viewport"]["offset_xywh"]
    x, y = hit["rect"][:2]
    return _clip([x+dx, y+dy, w, h])


def navigation_close_point(frame):
    region = navigation_region(frame)
    if region is None:
        return None
    x, y, w, h = region
    # The native Group_Windows/Btn_ full-screen backdrop closes the menu.
    return [x+w//2, min(700, y+h+80)]


def _digit_matches(frame, identity_hit, spec):
    """Use shape AND luminance: CCOEFF alone normalizes white/gray away."""
    x, y, w, h = spec["number_region"]
    ax, ay = identity_hit["rect"][:2]
    color = "gray" if spec["state"] == "locked" else "white"
    # 01 is narrower than 02..06. Give every rival the same centered search
    # window; otherwise its larger image cannot fit and the margin is false.
    max_width = max(catalog()["templates"][f"number_{n}_{color}"]["bbox"][2]
                    for n in range(1, 7))
    region = _clip([ax+x+w//2-max_width//2-2, ay+y-2, max_width+4, h+4])
    digit_key = f"number_{spec['number']}_{color}"
    digit = _hit(frame, digit_key, region)
    if digit is None or digit["score"] < spec["number_min_score"]:
        return False
    digit_scores = []
    for number in range(1, 7):
        if number == spec["number"]:
            other = digit
        else:
            candidate = {**catalog()["templates"][f"number_{number}_{color}"], "threshold": -1.0}
            other = matching.match_asset(frame, candidate, region)
        digit_scores.append(other["score"] if other else -1.0)
    n = spec["number"]
    if digit_scores[n-1] - max(s for i, s in enumerate(digit_scores, 1) if i != n) < spec["number_min_margin"]:
        return False
    target = matching.gray_asset(catalog()["templates"][digit_key]["image"], frame=frame)
    dx, dy, dw, dh = digit["rect"]
    source = cv2.cvtColor(frame[dy:dy+dh, dx:dx+dw], cv2.COLOR_BGR2GRAY)
    bright = target >= float(np.percentile(target, 95)) - 3
    return bool(bright.any() and abs(float(np.median(source[bright])) - float(np.median(target[bright]))) <= 32)


@matching.frame_observation
def navigation_cards(frame):
    region = navigation_region(frame)
    if region is None:
        return []
    candidates = []
    for number in range(1, 7):
        for state in ("open", "selected", "locked"):
            primary = catalog()["templates"][f"navigation_{number}_{state}_identity"]
            for spec in (primary, *primary.get("variants", [])):
                hit = matching.match_asset(frame, spec, region)
                if hit is None or not _digit_matches(frame, hit, spec):
                    continue
                dx, dy, w, h = spec["card_offset"]
                x, y = hit["rect"][:2]
                rect = [x+dx, y+dy, w, h]
                # A partial edge card must be revealed by scrolling before use.
                rx, ry, rw, rh = region
                if rect[0] < rx or rect[1] < ry or rect[0]+w > rx+rw or rect[1]+h > ry+rh:
                    continue
                if state == "locked" and not _hit(frame, "locked_hint", rect):
                    continue
                candidates.append({"number": number, "state": "locked" if state == "locked" else "open",
                                   "rect": rect, "center": [rect[0]+w//2, rect[1]+h//2], "score": hit["score"]})
    result = []
    for number in range(1, 7):
        values = [c for c in candidates if c["number"] == number]
        if values and len({c["state"] for c in values}) == 1:
            result.append(max(values, key=lambda c: c["score"]))
    # Overlapping identities cannot authorize either card.
    return [c for c in result if not any(other["number"] != c["number"]
            and rooms._overlap(c["rect"], other["rect"]) > .5 for other in result)]


@matching.frame_observation
def control(frame, name):
    """Return a matched control with its runtime click center, or None."""
    aliases = {"navigation_button": "navigation_button_label", "complete": "collection_complete",
               "capacity_warning": "capacity_warning_text"}
    if name == "collect_all":
        text = _hit(frame, "collect_all_text")
        if text is None:
            return None
        x, y = text["rect"][:2]
        # Text and a positive native box/badge feature must describe the same button.
        feature_region = _clip([x-60, y-170, 190, 180])
        if not (_hit(frame, "collect_all_icon", feature_region) or _hit(frame, "collectable_badge", feature_region)):
            return None
        dx, dy = catalog()["templates"]["collect_all_text"]["click_offset"]
        return {**text, "center": [x+dx, y+dy]}
    hit = _hit(frame, aliases.get(name, name))
    if name == "complete" and hit is None:
        title = _hit(frame, "collection_complete_title")
        if title:
            x, y = title["rect"][:2]
            icon = _hit(frame, "collection_complete_icon", _clip([x-90, y-25, 100, 105]))
            if icon:
                hit = title
    if hit and name == "navigation_button":
        dx, dy = catalog()["templates"]["navigation_button_label"]["click_offset"]
        hit = {**hit, "center": [hit["rect"][0]+dx, hit["rect"][1]+dy]}
    return hit


@matching.frame_observation
def physical(frame, room_name):
    if room_name not in ROOMS or not _frame(frame):
        return False
    if navigation_open(frame) or control(frame, "complete") or control(frame, "capacity_warning"):
        return False
    number = ROOMS.index(room_name)+1
    return bool(_title(frame, number) and _hit(frame, "workshop_edit_marker"))


@matching.frame_observation
def collection_state(frame, room_name):
    if not _frame(frame):
        return None
    if control(frame, "capacity_warning"):
        return "capacity_blocked"
    if control(frame, "complete"):
        return "complete"
    if not physical(frame, room_name):
        return None
    if control(frame, "collect_all"):
        return "collectable"
    # Partially detected collection controls are uncertainty, never an empty room.
    if any(_hit(frame, name) for name in ("collect_all_text", "collect_all_icon", "collectable_badge")):
        return None
    return "idle"
