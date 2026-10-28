"""Same-frame, separately recognized fatigue numbers and fresh-frame confirmation.

The small page-specific envelope contains both numbers. Its separator is located
geometrically, never OCR'd or substituted with a digit. Glyph bounds, rather than
a fixed midpoint, keep changing digit counts intact.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from numbers import Real

import cv2
import numpy as np

from packages.aura_core.scheduler.operation_scope import OperationTimeout
from packages.aura_core.utils.exceptions import TaskControlException


class FatigueConfirmation:
    """Require a complete identical pair on two consecutive distinct frames."""

    def __init__(self):
        self.previous = None
        self.frame_id = None

    def feed(self, pair, frame_id):
        if pair is None:
            self.previous, self.frame_id = None, None
            return False
        if frame_id == self.frame_id:
            return False
        confirmed = pair == self.previous
        self.previous, self.frame_id = pair, frame_id
        return confirmed


def split_fatigue_numbers(image):
    """Return complete left/right glyph crops, or unknown for ambiguous geometry."""
    if image.ndim != 3 or image.shape[2] < 3:
        return None, "fatigue_layout_unrecognized"
    rgb = image[:, :, :3].astype(np.int16)
    mask = ((rgb.min(axis=2) >= 170)
            & (rgb.max(axis=2) - rgb.min(axis=2) < 70)).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    glyphs = [(index, tuple(map(int, row))) for index, row in enumerate(stats[1:], 1)
              if row[3] >= 6 and row[2] <= row[3] * 1.4 and row[4] >= 5]
    if not glyphs:
        return None, "fatigue_layout_unrecognized"
    height = float(np.median([row[3] for _, row in glyphs]))
    glyphs = [(index, row) for index, row in glyphs if .7 * height <= row[3] <= 1.4 * height]
    candidates = []
    for index, (x, y, w, h, _) in glyphs:
        if w > .55 * h:
            continue
        component = labels[y:y+h, x:x+w] == index
        widths = component.sum(axis=1)
        if not widths.all() or widths.max() > max(3, h * .25):
            continue
        centers = np.array([np.flatnonzero(row).mean() for row in component])
        slope, intercept = np.polyfit(np.arange(h), centers, 1)
        residual = np.sqrt(np.mean((centers - (slope * np.arange(h) + intercept)) ** 2))
        if not (-.8 <= slope <= -.18 and residual <= .6):
            continue
        left = [row for _, row in glyphs if row[0] + row[2] <= x]
        right = [row for _, row in glyphs if row[0] >= x + w]
        if not left or not right:
            continue
        # Require every text glyph to belong to this single number line.
        if len(left) + len(right) + 1 != len(glyphs):
            continue
        line = left + right
        if any(abs((r[1] + r[3] / 2) - (y + h / 2)) > height * .3 for r in line):
            continue
        ordered = sorted(left + [(x, y, w, h, 0)] + right)
        if any(b[0] - (a[0] + a[2]) > height for a, b in zip(ordered, ordered[1:])):
            continue
        candidates.append((x, x+w, left, right))
    if len(candidates) != 1:
        return None, "fatigue_layout_unrecognized"
    separator_left, separator_right, left, right = candidates[0]
    crops = []
    for rows, lower, upper in ((left, 0, separator_left),
                               (right, separator_right, image.shape[1])):
        x = min(row[0] for row in rows)
        y = min(row[1] for row in rows)
        end_x = max(row[0] + row[2] for row in rows)
        end_y = max(row[1] + row[3] for row in rows)
        if x == 0 or end_x == image.shape[1] or y == 0 or end_y == image.shape[0]:
            return None, "fatigue_digits_clipped"
        crop = image[max(0, y-3):min(image.shape[0], end_y+3),
                     max(lower, x-2):min(upper, end_x+2)]
        crops.append(crop)
    return tuple(crops), "recognized"


def _number(result):
    debug = getattr(result, "debug_info", None)
    if (getattr(result, "success", True) is False
            or (debug is not None and (not isinstance(debug, Mapping) or debug.get("error")))):
        return None, "ocr_failed"
    texts = []
    for row in getattr(result, "results", ()) or ():
        text = str(getattr(row, "text", "")).strip()
        if not text:
            continue
        confidence = getattr(row, "confidence", None)
        if (isinstance(confidence, bool) or not isinstance(confidence, Real)
                or not math.isfinite(confidence) or confidence < .8):
            return None, "low_confidence"
        texts.append(text)
    # Do not strip punctuation or guess digits from an incomplete OCR fragment.
    text = "".join(texts)
    if not re.fullmatch(r"[0-9]{1,5}", text):
        return None, "invalid_fatigue_digits"
    return int(text), "recognized"


def read_fatigue_pair(frame, roi, ocr, *, guard=lambda: None):
    """Recognize both sides from this frame, retaining no image across rounds."""
    guard()
    crops, reason = split_fatigue_numbers(frame.crop(roi).rgb_image())
    if crops is None:
        return None, reason
    results = []
    for crop in crops:
        guard()
        image = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        try:
            result = ocr.recognize_all(source_image=image)
        except (OperationTimeout, TaskControlException):
            raise
        except Exception:
            guard()
            return None, "ocr_failed"
        guard()
        results.append(result)
    parsed = [_number(result) for result in results]
    for number, reason in parsed:
        if number is None:
            return None, reason
    current, maximum = (item[0] for item in parsed)
    if maximum == 0:
        return None, "invalid_max_fatigue"
    return (current, maximum), "recognized"
