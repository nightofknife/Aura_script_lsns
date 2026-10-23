"""Experimental native-font reconstruction reader; no game or input integration.

Templates are rendered from the installed game's TTF, not cropped screenshot
glyphs. Font atlas pixels are unavailable. All decisions remain offline-only.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


PLAN = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = PLAN / "data/meta/prison_warehouse_native_digits_pc.json"


def native_response(card_rgb):
    gray = cv2.cvtColor(card_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    response = gray - cv2.GaussianBlur(gray, (0, 0), 1.0)
    response -= np.median(response[:, 30:62], axis=1)[:, None]
    return response


def native_white_bar(card_rgb):
    white = (card_rgb.min(axis=2) >= 245).mean(axis=1) >= .75
    starts = []
    for y in range(68, 82):
        if white[y] and (y == 68 or not white[y - 1]):
            end = y
            while end < 84 and white[end]:
                end += 1
            if end - y >= 3:
                starts.append(y)
    return max(starts) if starts else None


def native_digit_scores(response, bar, position, bank):
    ranked = []
    for digit, template in bank.items():
        needle, support = template["response"], template["mask"]
        expected = needle[support]
        best, best_box = -1.0, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                x, y = 70 - position * 9 + dx, bar - 13 + dy
                actual = response[y:y + 17, x:x + 13][support]
                denominator = float(np.linalg.norm(actual) * np.linalg.norm(expected))
                score = float(np.dot(actual, expected) / max(denominator, 1e-6))
                if score > best:
                    best, best_box = min(1.0, score), [x, y, 13, 17]
        ranked.append({"digit": digit, "score": round(best, 6), "bbox": best_box})
    return sorted(ranked, key=lambda row: row["score"], reverse=True)


class NativeWarehouseQuantityReader:
    """Decode unknown-length 1-4 digit stacks with conservative rejection gates."""

    def __init__(self, catalog=DEFAULT_CATALOG):
        self.catalog = json.loads(Path(catalog).read_text(encoding="utf-8"))
        self.config = self.catalog["reader"]
        self.bank = {}
        for spec in self.catalog["templates"]:
            self.bank[spec["digit"]] = {
                "response": np.load(PLAN / spec["response"], allow_pickle=False),
                "mask": cv2.imread(str(PLAN / spec["mask"]), cv2.IMREAD_GRAYSCALE) > 0,
            }

    def read_card(self, card_rgb):
        result = {"status": "unknown", "value": None, "candidate_text": None, "experimental": True,
                  "offline_only": True, "auto_click_ready": False, "reasons": [], "positions": []}
        if not isinstance(card_rgb, np.ndarray) or card_rgb.shape != (84, 84, 3) or card_rgb.dtype != np.uint8:
            result["reasons"].append("requires_exact_84x84_uint8_rgb_card")
            return result
        bar = native_white_bar(card_rgb)
        result["white_bar_y"] = bar
        if bar is None:
            result["reasons"].append("white_bar_not_confirmed")
            return result
        response = native_response(card_rgb)
        chosen, absent = [], False
        for position in range(self.config["max_digits"] + 1):
            ranked = native_digit_scores(response, bar, position, self.bank)
            top = ranked[0]
            margin = top["score"] - ranked[1]["score"]
            result["positions"].append({"from_right": position, "ranked": ranked, "margin": round(margin, 6)})
            if top["score"] < self.config["absence_score_ceiling"]:
                absent = True
                continue
            if absent:
                result["reasons"].append("unexplained_glyph_after_gap")
                continue
            if position >= self.config["max_digits"]:
                result["reasons"].append("more_than_four_glyphs")
                continue
            if top["score"] < self.config["minimum_score"] or margin < self.config["minimum_margin"]:
                result["reasons"].append("weak_or_ambiguous_native_glyph")
                continue
            chosen.append(top["digit"])
        if chosen:
            text = "".join(reversed(chosen))
            result["candidate_text"] = text
            if text.startswith("0"):
                result["reasons"].append("zero_or_leading_zero_stack")
            if any(digit in "89" for digit in text):
                result["reasons"].append("digit_8_or_9_has_no_supplied_capture_calibration")
        else:
            result["reasons"].append("no_confident_right_aligned_digits")
        if not result["reasons"]:
            result["status"] = "recognized_native_reconstruction"
            result["value"] = int(result["candidate_text"])
        return result
