"""Read-only recognition of the node card shown before Deep Dive movement is confirmed."""

from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path

import cv2
import numpy as np


_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "consciousness_deep_dive_single_run"
_KINDS = ("empty", "battle", "elite_battle", "reward", "adventure", "healing", "shop")
_ENEMY_PORTRAIT_KINDS = frozenset({"battle", "elite_battle"})
_ENTER_SEARCH = (990, 500, 1200, 590)
_TITLE_SEARCH = (980, 280, 1230, 355)
_ICON_SEARCH = (980, 105, 1230, 280)
_ENTER_THRESHOLD = .80
_PART_THRESHOLD = .80
_MARGIN_THRESHOLD = .08


@lru_cache(maxsize=None)
def _template(name: str) -> np.ndarray:
    path = _TEMPLATE_DIR / f"{name}.png"
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise FileNotFoundError(path)
    return image


def _match(gray: np.ndarray, name: str, region: tuple[int, int, int, int]) -> tuple[float, list[int]]:
    x1, y1, x2, y2 = region
    template = _template(name)
    response = cv2.matchTemplate(gray[y1:y2, x1:x2], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(response)
    return float(score), [x1 + x + template.shape[1] // 2, y1 + y + template.shape[0] // 2]


def classify_entry_card(image_rgb: np.ndarray) -> dict:
    """Return a type only when the stable fields provide unambiguous evidence.

    Coordinates are relative to the 1280x720 game client. This function never
    clicks, and its `enter_point` is only an observation, not authorization to
    use the point before the game flow supports the detected type.
    """
    if not isinstance(image_rgb, np.ndarray) or image_rgb.shape != (720, 1280, 3) or image_rgb.dtype != np.uint8:
        return {"status": "invalid_frame", "kind": None, "confidence": 0.0, "enter_point": None, "scores": {}}
    if float(image_rgb.std()) < 2:
        return {"status": "invalid_frame", "kind": None, "confidence": 0.0, "enter_point": None, "scores": {}}

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    enter_score, enter_point = _match(gray, "event_enter", _ENTER_SEARCH)
    if not math.isfinite(enter_score):
        return {"status": "ambiguous", "kind": None, "confidence": 0.0, "enter_point": None, "scores": {}}
    if enter_score < _ENTER_THRESHOLD:
        return {"status": "absent", "kind": None, "confidence": round(enter_score, 4),
                "enter_point": None, "scores": {"enter": round(enter_score, 4)}}

    parts = {}
    for kind in _KINDS:
        title, _ = _match(gray, f"entry_{kind}_title", _TITLE_SEARCH)
        icon, _ = _match(gray, f"entry_{kind}_icon", _ICON_SEARCH)
        parts[kind] = {"title": title, "icon": icon, "combined": (title + icon) / 2}
    if any(not math.isfinite(value) for scores in parts.values() for value in scores.values()):
        return {"status": "ambiguous", "kind": None, "confidence": 0.0,
                "enter_point": enter_point, "scores": {"enter": round(enter_score, 4)}}

    title_winner = max(_KINDS, key=lambda kind: parts[kind]["title"])
    icon_winner = max(_KINDS, key=lambda kind: parts[kind]["icon"])
    ranking = sorted(_KINDS, key=lambda kind: parts[kind]["combined"], reverse=True)
    best, runner_up = ranking[:2]
    margin = parts[best]["combined"] - parts[runner_up]["combined"]
    title_ranking = sorted(_KINDS, key=lambda kind: parts[kind]["title"], reverse=True)
    title_margin = parts[title_ranking[0]]["title"] - parts[title_ranking[1]]["title"]
    # Combat cards render the current enemy portrait in the icon area. Its
    # artwork changes between encounters, while the type title remains fixed.
    if title_winner in _ENEMY_PORTRAIT_KINDS:
        accepted = parts[title_winner]["title"] >= .86 and title_margin >= .14
        best = title_winner
        confidence = parts[best]["title"]
        margin = title_margin
        evidence_mode = "enter_and_title_with_variable_enemy_portrait"
    else:
        accepted = (best == title_winner == icon_winner and parts[best]["title"] >= _PART_THRESHOLD
                    and parts[best]["icon"] >= _PART_THRESHOLD and margin >= _MARGIN_THRESHOLD)
        confidence = parts[best]["combined"]
        evidence_mode = "enter_title_and_icon"
    return {
        "status": "recognized" if accepted else "ambiguous",
        "kind": best if accepted else None,
        "confidence": round(confidence, 4),
        "score_margin": round(margin, 4),
        "evidence_mode": evidence_mode,
        "enter_point": enter_point,
        "scores": {"enter": round(enter_score, 4), **{
            kind: {part: round(value, 4) for part, value in scores.items()}
            for kind, scores in parts.items()}},
    }


__all__ = ["classify_entry_card"]
