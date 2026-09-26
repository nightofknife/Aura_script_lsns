"""Battle and reward page observations; no input and no portrait/card identity matching."""
from functools import lru_cache
from pathlib import Path
import math

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2] / "templates/consciousness_deep_dive_single_run"


@lru_cache(maxsize=None)
def template(name):
    path = (ROOT.parent / 'deep_dive_events/result_exit.png' if name == 'reward_exit_prompt'
            else ROOT / (name + '.png'))
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise FileNotFoundError(name)
    return image


def match(gray, name, region):
    x1, y1, x2, y2 = region
    ref = template(name)
    response = cv2.matchTemplate(gray[y1:y2, x1:x2], ref, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(response)
    return (float(score) if math.isfinite(score) else 0.0,
            [x1 + x + ref.shape[1] // 2, y1 + y + ref.shape[0] // 2])


def _cards(gray):
    # SELECT markers are shared by reward cards; artwork and card names vary.
    roi = gray[475:575, 100:1180]
    candidates = []
    for selected, name in ((False, "battle_card_unselected"), (True, "battle_card_selected")):
        ref = template(name)
        response = cv2.matchTemplate(roi, ref, cv2.TM_CCOEFF_NORMED)
        for _ in range(5):
            _, score, _, (x, y) = cv2.minMaxLoc(response)
            if not math.isfinite(score) or score < .83:
                break
            cx, cy = x + 100 + ref.shape[1] // 2, y + 475 + ref.shape[0] // 2
            candidates.append({"point": [cx, cy - 180], "selected": selected, "score": float(score)})
            response[max(0, y-50):y+51, max(0, x-100):x+101] = -1
    unique = []
    for row in sorted(candidates, key=lambda c: -c["score"]):
        if all(abs(row["point"][0]-old["point"][0]) > 120 for old in unique):
            unique.append(row)
    return sorted(unique, key=lambda c: c["point"][0])


def detect_battle_page(image_rgb):
    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    probes = {
        "formation": match(gray, "battle_formation_title", (500, 10, 790, 100)),
        "start": match(gray, "battle_start", (970, 550, 1275, 705)),
        "victory": match(gray, "battle_victory", (820, 70, 1140, 190)),
        "next": match(gray, "battle_next", (1000, 580, 1240, 705)),
        "selection": match(gray, "battle_reward_title", (510, 8, 780, 90)),
        "confirm": match(gray, "battle_reward_confirm", (520, 595, 790, 675)),
        "obtained": match(gray, "reward_obtained_title", (530, 50, 775, 145)),
        "exit": match(gray, "reward_exit_prompt", (500, 645, 790, 719)),
    }
    scores = {key: round(value[0], 4) for key, value in probes.items()}
    base = {"scores": scores}
    if scores["obtained"] >= .85 and scores["exit"] >= .85:
        return {**base, "scene": "reward_obtained", "click": [280, 630]}
    if scores["selection"] >= .85 and scores["confirm"] >= .85:
        cards = _cards(gray)
        count = sum(card['selected'] for card in cards)
        return {**base, "scene": "battle_reward_selection", "cards": cards,
                "selected_count": count, "required_count": 1, "click": probes["confirm"][1]}
    if scores["victory"] >= .85 and scores["next"] >= .85:
        return {**base, "scene": "battle_victory", "click": probes["next"][1]}
    if scores["formation"] >= .85 and scores["start"] >= .85:
        return {**base, "scene": "battle_formation", "click": probes["start"][1]}
    return None
