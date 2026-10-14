"""Production workshop template observations, 1280x720 BGR. No OCR or input."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np

from . import _reforming_center_room_templates as rooms
from . import _reforming_center_clicks as clicks

PLAN = Path(__file__).resolve().parents[2]
RAW = "templates/reforming_center/raw_workshop/"
SHARED = {
    "all": (RAW+"select_all.png", (810, 635, 235, 80)),
    "cancel_all": (RAW+"cancel_all.png", (810, 635, 235, 80)),
    "confirm": (RAW+"staff_confirm.png", (1040, 635, 225, 80)),
    "back": (RAW+"selection_back.png", (40, 5, 110, 70)),
}


@lru_cache(maxsize=1)
def catalog():
    path = PLAN / "data/meta/product_workshop_templates_pc.json"
    if not path.is_file():
        raise clicks.CheckedClickError("product_workshop_templates_missing", {"catalog": str(path)})
    return json.loads(path.read_text(encoding="utf-8"))


def require_assets():
    paths = [entry["image"] for control in catalog()["controls"].values() for entry in control["variants"]]
    paths.extend(path for path, _ in SHARED.values())
    missing = [path for path in paths if not (PLAN / path).is_file()]
    if missing:
        raise clicks.CheckedClickError("product_workshop_templates_missing", {"images": missing})


def _valid(frame):
    return isinstance(frame, np.ndarray) and frame.shape == (720, 1280, 3) and frame.dtype == np.uint8


def _foreground(image, polarity):
    if polarity == "dark":
        return np.where(image.max(axis=2) < 100, 255, 0).astype(np.uint8)
    return np.where((image.min(axis=2) >= 190) &
                    (image.max(axis=2).astype(int)-image.min(axis=2) < 55), 255, 0).astype(np.uint8)


def _hit(frame, name):
    if not _valid(frame):
        return None
    if name in SHARED:
        path, region = SHARED[name]
        return rooms.match_asset(frame, {"image": path, "threshold": .92}, region)
    spec = catalog()["controls"][name]
    x, y, w, h = spec["region"]
    candidates = []
    for polarity in spec["polarity"]:
        source = _foreground(frame[y:y+h, x:x+w], polarity)
        for variant in spec["variants"]:
            target = cv2.cvtColor(rooms._asset(variant["image"]), cv2.COLOR_BGR2GRAY)
            th, tw = target.shape
            if h < th or w < tw or target.std() < 1:
                continue
            scores = cv2.matchTemplate(source, target, cv2.TM_CCOEFF_NORMED)
            scores[~np.isfinite(scores)] = -1
            _, score, _, location = cv2.minMaxLoc(scores)
            if score >= spec["threshold"]:
                candidates.append({"score": score, "rect": [x+location[0], y+location[1], tw, th],
                                   "center": [x+location[0]+tw//2, y+location[1]+th//2]})
    return max(candidates, key=lambda hit: hit["score"]) if candidates else None


def control_point(frame, name):
    hit = _hit(frame, name)
    return clicks.rounded_point(hit["center"]) if hit else None


def editor_ready(frame):
    return bool(_hit(frame, "quick") and _hit(frame, "apply_marker")
                and not _hit(frame, "confirm") and not _hit(frame, "save_message"))


def selection_state(frame):
    if not _hit(frame, "confirm") or not _hit(frame, "back"):
        return None
    selected, cancelled = _hit(frame, "all"), _hit(frame, "cancel_all")
    # Missing/unrecognized controls are UNKNOWN, never proof of no vacancies.
    if bool(selected) == bool(cancelled):
        return None
    if cancelled:
        return {"button": "cancel_all"}
    x, y, w, h = selected["rect"]
    hsv = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2HSV)
    blue = (hsv[:, :, 0] >= 95) & (hsv[:, :, 0] <= 125) & (hsv[:, :, 1] >= 90) & (hsv[:, :, 2] >= 100)
    return {"button": "select_all" if blue.mean() >= .3 else "unavailable"}


def save_prompt(frame):
    if not _hit(frame, "save_message") or not _hit(frame, "save_no"):
        return None
    yes = control_point(frame, "save_yes")
    return {"page": "save_changes_prompt", "confirm_point": yes} if yes else None
