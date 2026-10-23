"""Read warehouse stack quantities with tightly cropped recognition-only OCR.

The caller supplies one fully visible 84x84 RGB card and the existing OCR
service. This module does not capture, click or create an OCR engine. Historical
screenshot glyph helpers are retained below for offline comparison only.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re

import cv2
import numpy as np


PLAN = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = PLAN / "data/meta/prison_warehouse_quantity_pc.json"
DEFAULT_OCR_CATALOG = PLAN / "data/meta/prison_warehouse_quantity_ocr_pc.json"


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def parse_quantity_ocr(response, *, minimum_confidence=.80):
    """Parse one whole positive ASCII integer, preserving the original output."""
    result = {"status": "unknown", "value": None, "text": None,
              "confidence": None, "raw_results": [], "reasons": []}
    if _field(response, "success", True) is False or (_field(response, "debug_info", {}) or {}).get("error"):
        result["reasons"].append("ocr_service_failed")
        return result
    rows = _field(response, "results", ()) or ()
    result["raw_results"] = [{"text": _field(row, "text", ""),
                              "confidence": _field(row, "confidence")} for row in rows]
    if len(rows) != 1:
        result["reasons"].append("ocr_requires_one_result")
        return result
    text, confidence = _field(rows[0], "text", ""), _field(rows[0], "confidence")
    result.update(text=text, confidence=confidence)
    if not isinstance(text, str) or re.fullmatch(r"[1-9][0-9]*", text) is None:
        result["reasons"].append("not_a_whole_positive_ascii_integer")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0 <= confidence <= 1
            or confidence < minimum_confidence):
        result["reasons"].append("ocr_confidence_below_threshold_or_invalid")
    if not result["reasons"]:
        result.update(status="recognized_ocr_quantity", value=int(text))
    return result


class WarehouseQuantityReader:
    """Use the existing OCR service on the complete, vertically tightened text band."""

    def __init__(self, ocr, *, catalog=DEFAULT_OCR_CATALOG):
        self.ocr = ocr
        self.catalog = json.loads(Path(catalog).read_text(encoding="utf-8"))
        self.config = self.catalog["reader"]
        self.roi = tuple(self.config["roi_xywh"])
        self.rois = [self.roi]
        if self.config.get("retry_roi_xywh") is not None:
            self.rois.append(tuple(self.config["retry_roi_xywh"]))
        for roi in self.rois:
            if (len(roi) != 4 or any(type(value) is not int for value in roi)
                    or roi[0] < 0 or roi[1] < 0 or roi[2] <= 0 or roi[3] <= 0
                    or roi[0] + roi[2] > 84 or roi[1] + roi[3] > 84):
                raise ValueError("Warehouse quantity ROI must stay inside an 84x84 card")
        self.minimum_confidence = float(self.config["minimum_confidence"])
        if not math.isfinite(self.minimum_confidence) or not 0 <= self.minimum_confidence <= 1:
            raise ValueError("Warehouse quantity OCR confidence must be between zero and one")

    def read_card(self, card_rgb):
        if not isinstance(card_rgb, np.ndarray) or card_rgb.shape != (84, 84, 3) or card_rgb.dtype != np.uint8:
            return {"status": "unknown", "value": None, "text": None, "confidence": None,
                    "raw_results": [], "reasons": ["requires_exact_84x84_uint8_rgb_card"],
                    "roi_xywh": list(self.roi), "method": "ocr_recognition_only"}
        attempts = []
        for roi in self.rois:
            x, y, width, height = roi
            crop = card_rgb[y:y + height, x:x + width].copy()
            response = self.ocr.recognize_all(
                source_image=crop, recognition_only=True,
                use_textline_orientation=False, rotate_tall_crops=False,
            )
            result = parse_quantity_ocr(response, minimum_confidence=self.minimum_confidence)
            result["roi_xywh"] = list(roi)
            attempts.append(result)
            if result["value"] is not None or "ocr_service_failed" in result["reasons"]:
                break
        final = dict(attempts[-1])
        final.update(method="ocr_recognition_only", attempts=attempts,
                     ocr_calls=len(attempts), used_retry=len(attempts) > 1)
        return final


def locate_white_bar(card_rgb: np.ndarray) -> int | None:
    """Locate the broad card stripe without treating its top as a text baseline."""
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


def quantity_response(card_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Suppress smooth goods texture and the common horizontal stripe response."""
    gray = cv2.cvtColor(card_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    response = gray - cv2.GaussianBlur(gray, (0, 0), 1.0)
    response -= np.median(response[:, 30:62], axis=1)[:, None]
    return gray, response


class ScreenshotQuantityReader:
    """Historical screenshot-glyph comparison; the current reader above uses OCR."""

    def __init__(self, catalog: Path | str = DEFAULT_CATALOG):
        self.catalog_path = Path(catalog)
        self.catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.config = self.catalog["reader"]
        self.templates = []
        for spec in self.catalog["templates"]:
            if spec["status"] != "supplied_screenshot_calibrated":
                continue
            response = np.load(PLAN / spec["response"], allow_pickle=False)
            mask = cv2.imread(str(PLAN / spec["mask"]), cv2.IMREAD_GRAYSCALE) > 0
            self.templates.append({**spec, "array": response, "support": mask})

    def read_card(self, card_rgb: np.ndarray) -> dict:
        result = {
            "status": "unknown", "value": None, "candidate_text": None,
            "experimental": True, "offline_only": True, "auto_click_ready": False,
            "calibrated_digits": "01234567", "unresolved_digits": "89",
            "reasons": [], "positions": [],
        }
        if not isinstance(card_rgb, np.ndarray) or card_rgb.shape != (84, 84, 3) or card_rgb.dtype != np.uint8:
            result["reasons"].append("requires_exact_84x84_uint8_rgb_card")
            return result
        bar = locate_white_bar(card_rgb)
        result["white_bar_y"] = bar
        if bar is None:
            result["reasons"].append("white_bar_not_confirmed")
            return result
        gray, response = quantity_response(card_rgb)
        support_union = np.zeros((84, 84), np.uint8)
        chosen = []
        absent_seen = False
        for position in range(self.config["max_digits"] + 1):
            by_digit = {}
            for spec in self.templates:
                needle, support = spec["array"], spec["support"]
                height, width = needle.shape
                best = {"score": -1.0, "digit": spec["digit"], "template": spec["id"], "bbox": None}
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        x = 70 - position * self.config["digit_advance"] + dx
                        y = bar - 13 + dy
                        patch = response[y:y + height, x:x + width]
                        actual, expected = patch[support], needle[support]
                        denominator = float(np.linalg.norm(actual) * np.linalg.norm(expected))
                        score = float(np.dot(actual, expected) / max(denominator, 1e-6))
                        if score > best["score"]:
                            best = {"score": min(1.0, score), "digit": spec["digit"], "template": spec["id"], "bbox": [x, y, width, height], "support": support}
                if best["score"] > by_digit.get(spec["digit"], {"score": -1})["score"]:
                    by_digit[spec["digit"]] = best
            ranked = sorted(by_digit.values(), key=lambda item: item["score"], reverse=True)
            top = ranked[0]
            margin = top["score"] - ranked[1]["score"]
            result["positions"].append({
                "from_right": position, "score": round(top["score"], 6),
                "margin": round(margin, 6), "digit": top["digit"], "bbox": top["bbox"],
                "ranked": [{k: round(row[k], 6) if k == "score" else row[k] for k in ("digit", "score", "template", "bbox")} for row in ranked[:3]],
            })
            if top["score"] < self.config["absence_score_ceiling"]:
                absent_seen = True
                continue
            if absent_seen:
                result["reasons"].append("unexplained_glyph_after_gap")
                continue
            if position >= self.config["max_digits"]:
                result["reasons"].append("more_than_four_glyphs")
                continue
            minimum_score = self.config["digit_minimum_score"].get(top["digit"], self.config["minimum_score"])
            if top["score"] < minimum_score or margin < self.config["minimum_margin"]:
                result["reasons"].append("weak_or_ambiguous_digit")
                continue
            chosen.append(top)
            x, y, width, height = top["bbox"]
            support_union[y:y + height, x:x + width] |= top["support"].astype(np.uint8)
        if chosen:
            text = "".join(item["digit"] for item in reversed(chosen))
            result["candidate_text"] = text
            if text.startswith("0"):
                result["reasons"].append("zero_or_leading_zero_is_not_a_collected_stack")
        else:
            result["reasons"].append("no_confident_right_aligned_digits")
        # This is a conservative residue check, not a guarantee against every
        # unseen symbol or adversarial commodity texture.
        white_ink = ((gray > 225) & ((gray - cv2.erode(gray, np.ones((3, 3), np.uint8))) > 35)).astype(np.uint8)
        white_ink[:bar - 12] = 0
        white_ink[bar:] = 0
        white_ink[:, :43] = 0
        white_ink[:, 83:] = 0
        explained = cv2.dilate(support_union, np.ones((3, 3), np.uint8))
        unexplained = int(((white_ink > 0) & (explained == 0)).sum())
        result["unexplained_white_pixels"] = unexplained
        if unexplained > self.config["unexplained_white_pixel_limit"]:
            result["reasons"].append("unexplained_white_strokes")
        if not result["reasons"]:
            result["status"] = "recognized_calibrated_glyphs"
            result["value"] = int(result["candidate_text"])
        return result
