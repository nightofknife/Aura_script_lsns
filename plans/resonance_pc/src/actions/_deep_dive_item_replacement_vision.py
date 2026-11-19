"""Recognize the Deep Dive intuition overflow panel and its discard control."""

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ._deep_dive_node_rewards import ITEM_REPLACEMENT


TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "deep_dive" / ITEM_REPLACEMENT


@lru_cache(maxsize=None)
def _template(name, mode):
    data = cv2.imdecode(np.fromfile(TEMPLATES / (name + ".png"), dtype=np.uint8), mode)
    if data is None:
        raise FileNotFoundError(TEMPLATES / (name + ".png"))
    return data


def _label_score(gray, name, region):
    x1, y1, x2, y2 = region
    reference = _template(name, cv2.IMREAD_GRAYSCALE)
    scores = cv2.matchTemplate(gray[y1:y2, x1:x2], reference, cv2.TM_CCOEFF_NORMED)
    return float(np.max(scores))


@lru_cache(maxsize=1)
def _discard_template():
    sprite = _template("discard", cv2.IMREAD_UNCHANGED)
    return sprite[:, :, :3].copy(), (sprite[:, :, 3] >= 224).astype(np.uint8)


def detect_item_replacement(bgr, gray):
    # The original panel uses both lines just above and below the red discard
    # button. The three markers distinguish it from normal reward screens.
    if not (_label_score(gray, "replace_prompt", (553, 600, 727, 649)) >= .75
            and _label_score(gray, "inventory_full", (585, 631, 695, 675)) >= .75):
        return None
    reference, mask = _discard_template()
    x1, y1 = 471, 581
    scores = cv2.matchTemplate(bgr[y1:690, x1:576], reference, cv2.TM_SQDIFF_NORMED, mask=mask)
    scores = np.where(np.isfinite(scores), scores, np.inf)
    score, _, (x, y), _ = cv2.minMaxLoc(scores)
    observation = {"scene": ITEM_REPLACEMENT, "valid": True, "options": [],
                   "scores": {"replacement_discard_error": score if np.isfinite(score) else None}}
    if score <= .12:
        h, w = reference.shape[:2]
        observation["click"] = [x1 + x + w // 2, y1 + y + h // 2]
        # This area contains the incoming intuition, so consecutive panels can
        # be distinguished without treating their identical buttons as progress.
        observation["result_visual"] = cv2.resize(gray[270:470, 205:442], (32, 24),
                                                   interpolation=cv2.INTER_AREA).flatten().tolist()
    return observation
