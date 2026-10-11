"""Pure, bounded workshop observations. All frames are 1280x720 BGR.

No OCR, game input, fixed fraction strings, or runtime game-data access. Tab
polarity is inferred from native glyphs and actual light/dark backgrounds;
the missing reserve-selected screenshot is not replaced by a fake asset.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np

from . import _raw_workshop_digits as digits
from . import _reforming_center_room_templates as rooms

PLAN = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def catalog():
    return json.loads((PLAN / "data/meta/raw_workshop_templates_pc.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _specs():
    return {item["name"]: item for item in catalog()["assets"]}


def _valid(image):
    return (isinstance(image, np.ndarray) and image.dtype == np.uint8
            and image.shape == (720, 1280, 3))


def _control(image, name, *, polarity=False, horizontal_margin=8):
    if not _valid(image) or name not in _specs():
        return None
    spec = _specs()[name]
    x, y, w, h = spec["client_bbox_xywh"]
    region = (max(0, x-horizontal_margin), max(0, y-6),
              w+horizontal_margin*2, h+12)
    if not polarity:
        return rooms.match_asset(image, {"image": spec["image"], "threshold": .92}, region)
    # CCOEFF normalizes luminance, and absolute correlation supports the same
    # glyphs on either tab polarity. Color/background is checked separately.
    target = cv2.cvtColor(rooms._asset(spec["image"]), cv2.COLOR_BGR2GRAY)
    rx, ry, rw, rh = region
    source = cv2.cvtColor(image[ry:ry+rh, rx:rx+rw], cv2.COLOR_BGR2GRAY)
    scores = np.abs(cv2.matchTemplate(source, target, cv2.TM_CCOEFF_NORMED))
    scores[~np.isfinite(scores)] = -1
    _, score, _, location = cv2.minMaxLoc(scores)
    if score < .92:
        return None
    return {"score": float(score), "rect": [rx+location[0], ry+location[1], w, h]}


def control_visible(image, name):
    """Known native control at its bounded client location, not whole-screen."""
    aliases = {"start": "start_enabled", "work_tab": "work_tab_selected",
               "reserve_tab": "reserve_tab_unselected"}
    name = aliases.get(name, name)
    if name in {"start_enabled", "start_disabled"}:
        return any(_control(image, item) is not None
                   for item in ("start_enabled", "start_disabled"))
    if name in {"work_tab_selected", "reserve_tab_unselected"}:
        return _control(image, name, polarity=True, horizontal_margin=30) is not None
    return _control(image, name) is not None


def _color_fraction(image, rect, kind):
    x, y, w, h = rect
    hsv = cv2.cvtColor(image[y:y+h, x:x+w], cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    if kind == "blue":
        mask = (hue >= 95) & (hue <= 125) & (saturation >= 90) & (value >= 100)
    elif kind == "red":
        mask = ((hue < 12) | (hue > 170)) & (saturation >= 90) & (value >= 100)
    elif kind == "yellow":
        mask = (hue >= 15) & (hue <= 40) & (saturation >= 90) & (value >= 100)
    else:
        mask = (saturation < 45) & (value >= 170)
    return float(mask.mean())


def control_enabled(image, name):
    aliases = {"start": "start_enabled"}
    name = aliases.get(name, name)
    hit = _control(image, name)
    if not hit:
        return False
    # The staffing confirm is white, unlike blue deposit/start/select controls.
    if name == "staff_confirm":
        return _color_fraction(image, hit["rect"], "white") >= .45
    if name in {"start_enabled", "deposit_confirm", "choose_prisoners", "select_all"}:
        return _color_fraction(image, hit["rect"], "blue") >= .4
    if name in {"deposit_button", "deposit_max"}:
        return True
    return False


def production_state(image):
    if not _valid(image):
        return None
    stop, speed = _control(image, "running_stop"), _control(image, "running_speed")
    running = (stop is not None and speed is not None
               and _color_fraction(image, stop["rect"], "red") >= .4
               and _color_fraction(image, speed["rect"], "yellow") >= .4)
    idle = control_visible(image, "start")
    if bool(running) == bool(idle):
        return None
    return "running" if running else "idle"


def _labels(image):
    work = _control(image, "work_tab_selected", polarity=True, horizontal_margin=30)
    reserve = _control(image, "reserve_tab_unselected", polarity=True, horizontal_margin=30)
    if not work or not reserve:
        return None
    return {"work": work, "reserve": reserve}


def _room_title(image, room_name):
    if not _valid(image):
        return False
    spec = rooms.load_catalog()["rooms"].get(room_name)
    if not spec or spec.get("entry_type") != "production":
        return False
    markers = spec.get("destination_markers", [])
    return bool(markers and rooms.match_asset(image, markers[0]))


def room_ready(image, room_name):
    # Running work pages intentionally have no choose_prisoners button.
    return bool(_room_title(image, room_name) and _labels(image)
                and production_state(image) is not None)


def _fraction_after(image, label, *, style="list"):
    x, y, w, h = label["rect"]
    # Relative to the detected label; enough space for 15/15, not a 7/7 crop.
    # Native label assets end immediately before the first digit.
    return digits.read_fraction(image, (x+w-1, y+2, 80, h-4), style=style)


def _tab(image, labels):
    selected = []
    for name, hit in labels.items():
        x, y, w, h = hit["rect"]
        patch = cv2.cvtColor(image[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
        edges = np.concatenate((patch[0], patch[-1], patch[:, 0], patch[:, -1]))
        brightness = float(np.median(edges))
        if brightness >= 180:
            selected.append(name)
        elif brightness > 90:
            return None
    return selected[0] if len(selected) == 1 else None


def snapshot(image, room_name):
    if not room_ready(image, room_name):
        return None
    labels = _labels(image)
    tab = _tab(image, labels)
    work = _fraction_after(image, labels["work"])
    reserve = _fraction_after(image, labels["reserve"])
    if tab is None or work is None or reserve is None:
        return None
    return {"production": production_state(image), "start_enabled": control_enabled(image, "start"),
            "work": work, "reserve": reserve, "tab": tab}


def _deposit_dialog(image):
    return all(control_visible(image, name) for name in
               ("deposit_currency", "deposit_min_marker", "deposit_max", "deposit_confirm", "deposit_cancel"))


def deposit_outcome(image, room_name):
    if not _valid(image):
        return None
    if _deposit_dialog(image):
        return "dialog"
    if _room_title(image, room_name) and control_visible(image, "deposit_full_toast"):
        return "already_full"
    return None


def deposit_maximum(image):
    """Recognize the real slider geometry, not an invented maximum screenshot.

    Native initial track is available; maximum geometry is only synthetically
    checked until a user-authorized live observation supplies that state.
    """
    if not _valid(image) or not _deposit_dialog(image):
        return False
    x, y, w, h = _specs()["slider_track_reference"]["client_bbox_xywh"]
    patch = image[y:y+h, x:x+w]
    bright = (patch.min(axis=2) >= 210)
    # Track is a long thin line. Knob occupies substantially more rows than
    # that line, and must sit at the right endpoint (not center or left).
    row_widths = bright.sum(axis=1)
    rows = np.flatnonzero(row_widths >= w*.8)
    if not len(rows) or len(rows) > 6:
        return False
    line = bright[rows].any(axis=0)
    ends = np.flatnonzero(line)
    if not len(ends):
        return False
    dense = bright.sum(axis=0) >= 14
    columns = np.flatnonzero(dense)
    if not 5 <= len(columns) <= 20 or columns[-1]-columns[0]+1 != len(columns):
        return False
    return bool(abs(float(columns.mean())-int(ends[-1])) <= 10
                and int(columns[-1]) >= int(ends[-1])-5)


def selection_state(image):
    if not _valid(image):
        return None
    footer = _control(image, "selection_footer")
    if not footer or not control_visible(image, "staff_confirm") or not control_visible(image, "selection_back"):
        return None
    buttons = [name for name in ("select_all", "cancel_all") if control_visible(image, name)]
    if len(buttons) != 1:
        return None
    count = _fraction_after(image, footer, style="selection")
    if count is None:
        return None
    return {"count": count, "button": buttons[0], "confirm_enabled": control_enabled(image, "staff_confirm"),
            "selected_marker": control_visible(image, "prisoner_selected_ribbon")}
