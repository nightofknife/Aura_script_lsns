"""Read-only recognition for a 1280x720 Deep Dive single-run test."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import math

import cv2
import numpy as np

from ._deep_dive_entry_card_vision import classify_entry_card
from ._deep_dive_battle_vision import detect_battle_page
from ._deep_dive_event_vision import detect_event_page, board_visible


TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "consciousness_deep_dive_single_run"
REGIONS = {
    "player_turn": (125, 120, 260, 180),
    "settlement_failure": (490, 50, 790, 155),
    "event_enter": (990, 500, 1200, 590),
    "event_vortex_choice_title": (530, 10, 770, 90),
    "event_vortex_floor": (80, 480, 250, 550),
    "rotate_confirm": (700, 505, 880, 580),
    "rotate_cancel": (400, 505, 600, 580),
    "move_pending": (1150, 370, 1270, 445),
    "move_done": (1135, 370, 1270, 445),
    "rotate_pending": (1150, 445, 1270, 515),
    "shop_title": (490, 8, 810, 105),
    "shop_back": (20, 5, 155, 100),
}
CYAN_LOW = np.array([80, 135, 135], np.uint8)
CYAN_HIGH = np.array([105, 255, 255], np.uint8)
MOVE_BUTTON = (960, 375, 1265, 445)
ROTATE_BUTTON = (960, 445, 1265, 515)
MOVE_BOARD = (320, 170, 955, 570)
ROTATE_BOARD = (420, 100, 865, 270)


@lru_cache(maxsize=None)
def _template(name: str) -> np.ndarray:
    image = cv2.imread(str(TEMPLATES / f"{name}.png"), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise FileNotFoundError(TEMPLATES / f"{name}.png")
    return image


def _crop(image: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    x1, y1, x2, y2 = box
    return image[y1:y2, x1:x2]


def _match(gray: np.ndarray, name: str) -> dict:
    x1, y1, x2, y2 = REGIONS[name]
    template = _template(name)
    response = cv2.matchTemplate(gray[y1:y2, x1:x2], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(response)
    return {
        "score": round(float(score), 4),
        "point": [x1 + x + template.shape[1] // 2, y1 + y + template.shape[0] // 2],
    }


def _dedupe(options: list[dict], radius: int = 28) -> list[dict]:
    unique: list[dict] = []
    for row in sorted(options, key=lambda item: -item["evidence"]):
        if all(math.dist(row["point"], old["point"]) >= radius for old in unique):
            unique.append(row)
    return sorted(unique, key=lambda item: (item["point"][1], item["point"][0]))


def _move_options(mask: np.ndarray) -> list[dict]:
    x0, y0, _, _ = MOVE_BOARD
    contours, _ = cv2.findContours(_crop(mask, MOVE_BOARD), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    options = []
    for contour in contours:
        area = cv2.contourArea(contour)
        x, y, width, height = cv2.boundingRect(contour)
        if 145 <= width <= 225 and 65 <= height <= 120 and area >= 600:
            options.append({
                "point": [int(x + x0 + width / 2), int(y + y0 + height / 2)],
                "box": [x + x0, y + y0, width, height],
                "evidence": int(area),
            })
    return _dedupe(options)


def _rotate_options(mask: np.ndarray) -> list[dict]:
    x0, y0, _, _ = ROTATE_BOARD
    count, _, stats, centers = cv2.connectedComponentsWithStats(_crop(mask, ROTATE_BOARD))
    options = []
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if 30 <= width <= 52 and 12 <= height <= 28 and area >= 270:
            options.append({
                "point": [int(round(centers[index][0] + x0)), int(round(centers[index][1] + y0))],
                "box": [x + x0, y + y0, width, height],
                "evidence": area,
            })
    return _dedupe(options)


def observe(image_rgb: np.ndarray, event_family: str = 'healing') -> dict:
    """Classify one client frame; uncertain frames never provide click candidates."""
    if not isinstance(image_rgb, np.ndarray) or image_rgb.shape != (720, 1280, 3) or image_rgb.dtype != np.uint8:
        return {"valid": False, "scene": "invalid_frame", "options": [], "scores": {}}
    if float(image_rgb.std()) < 2:
        return {"valid": False, "scene": "blank_frame", "options": [], "scores": {}}
    bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    matches = {name: _match(gray, name) for name in REGIONS}
    scores = {name: row["score"] for name, row in matches.items()}
    result = {"valid": True, "scene": "unknown", "options": [], "scores": scores,
              "result_visual": cv2.resize(gray[75:510,485:795],(24,24),interpolation=cv2.INTER_AREA).flatten().tolist(),
              "rotate_confirm_point": matches['rotate_confirm']['point'],
              "player_turn": scores["player_turn"] >= .85,
              "move_pending": scores["move_pending"] >= .85,
              "move_done": scores["move_done"] >= .85,
              "rotate_pending": scores["rotate_pending"] >= .85}

    # Overlays take priority: their underlying board and old cyan buttons remain visible.
    if scores["settlement_failure"] >= .85:
        result.update(scene="settlement", outcome="failure")
        return result
    if scores["shop_title"] >= .85 and scores["shop_back"] >= .85:
        result.update(scene="shop", click=matches["shop_back"]["point"])
        return result
    if event_family == 'vortex':
        from ._deep_dive_vortex_vision import detect_event_page as vortex_page, board_visible as vortex_board
        result['event_board'] = vortex_board(image_rgb)
        page = vortex_page(image_rgb)
        if page is not None:
            result.update(page)
            return result
    else:
        result['event_board'] = board_visible(image_rgb)
    battle_page = detect_battle_page(image_rgb)
    if battle_page is not None:
        result.update(battle_page)
        return result
    if event_family != 'vortex' and scores["event_vortex_choice_title"] >= .85 and scores["event_vortex_floor"] >= .85:
        result.update(scene="event_vortex_choice", event_type="vortex_floor_collapse")
        return result
    event_page = detect_event_page(image_rgb) if event_family != 'vortex' else None
    if event_page is not None:
        result.update(event_page)
        return result
    if scores["event_enter"] >= .80:
        entry_card = classify_entry_card(image_rgb)
        result["entry_card"] = entry_card
        if entry_card["status"] == "recognized":
            kind = entry_card["kind"]
            result.update(scene="event_entry", entry_kind=kind,
                          event_type={"reward": "item", "adventure": "vortex"}.get(kind, kind),
                          click=entry_card["enter_point"])
        else:
            result["scene"] = "ambiguous_event_entry"
        return result
    if scores["rotate_confirm"] >= .80 and scores["rotate_cancel"] >= .80:
        result.update(scene="rotate_preview", click=matches["rotate_confirm"]["point"])
        return result

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, CYAN_LOW, CYAN_HIGH)
    move_cyan = cv2.countNonZero(_crop(cyan, MOVE_BUTTON))
    rotate_cyan = cv2.countNonZero(_crop(cyan, ROTATE_BUTTON))
    result["cyan_pixels"] = {"move": int(move_cyan), "rotate": int(rotate_cyan)}
    yellow = cv2.inRange(hsv, np.array([18, 115, 120], np.uint8), np.array([40, 255, 255], np.uint8))
    result["rotate_done"] = cv2.countNonZero(_crop(yellow, (960, 445, 1265, 515))) >= 500

    if not result["player_turn"]:
        return result
    if move_cyan >= 2000 and rotate_cyan < 2000:
        options = _move_options(cyan)
        if 1 <= len(options) <= 4:
            result.update(scene="choose_move", options=options)
        return result
    if rotate_cyan >= 2000 and move_cyan < 2000:
        options = _rotate_options(cyan)
        if 2 <= len(options) <= 4:
            result.update(scene="choose_rotate", options=options)
        return result
    result["scene"] = "board"
    return result


__all__ = ["observe"]
