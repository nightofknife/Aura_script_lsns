"""Read small n/capacity regions using native single-glyph images, not OCR."""
from __future__ import annotations

from functools import lru_cache
from itertools import product
import json
from pathlib import Path

import cv2
import numpy as np

PLAN = Path(__file__).resolve().parents[2]


def foreground(image):
    """Convert white/black/yellow text to positive grayscale ink."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY)
    edge = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    background = float(np.median(edge))
    if background > 127:
        ink = background-gray.astype(np.float32)
    else:
        # On a dark UI, max-channel ink gives yellow/red and white characters
        # equal foreground strength before segmentation, not just afterwards.
        light = image if image.ndim == 2 else image[:, :, :3].max(axis=2)
        ink = light.astype(np.float32)-background
    ink = np.maximum(ink, 0)
    maximum = float(ink.max())
    if maximum < 30:
        return None
    return np.clip(ink*255/maximum, 0, 255).astype(np.uint8)


def segment(ink):
    threshold, mask = cv2.threshold(ink, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    occupied = (mask > 0).any(axis=0)
    edges = np.diff(np.r_[False, occupied, False].astype(np.int8))
    boxes = []
    for left, right in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        ys = np.flatnonzero((mask[:, left:right] > 0).any(axis=1))
        if not len(ys):
            continue
        top, bottom = int(ys[0]), int(ys[-1])+1
        if bottom-top < 6 or right-left < 2:
            return []
        # Clipped text is not allowed to silently turn into another number.
        if left == 0 or right == ink.shape[1] or top == 0 or bottom == ink.shape[0]:
            return []
        boxes.append((int(left), top, int(right-left), bottom-top))
    return boxes


def normalize(ink, box):
    x, y, w, h = box
    patch = ink[y:y+h, x:x+w].astype(np.float32)
    # A yellow numerator and white denominator have different luminance.
    # Normalize each character's contrast, not the whole fraction together.
    patch *= 255/max(float(patch.max()), 1)
    target_w = max(1, round(w*24/h))
    if target_w > 28:
        return None
    patch = cv2.resize(patch, (target_w, 24), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((32, 32), np.float32)
    left = (32-target_w)//2
    canvas[4:28, left:left+target_w] = patch/255
    return canvas


@lru_cache(maxsize=2)
def glyphs(style="list"):
    catalog = json.loads((PLAN / "data/meta/raw_workshop_digits_pc.json").read_text(encoding="utf-8"))
    output = []
    for item in catalog["assets"]:
        if item.get("style", "list") != style:
            continue
        path = (PLAN / item["image"]).resolve()
        if not path.is_relative_to(PLAN.resolve()):
            raise ValueError("Digit asset must stay inside the plan")
        ink = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE)
        if ink is None:
            raise ValueError("Digit asset is not readable")
        boxes = segment(ink)
        if len(boxes) != 1:
            raise ValueError(f"Expected one glyph: {item['image']}")
        output.append((item["character"], normalize(ink, boxes[0])))
    return output


def classify(ink, box, style="list"):
    sample = normalize(ink, box)
    if sample is None:
        return None
    letters, bank, sums = _bank(style)
    # Soft-ink Dice keeps antialiased edges; no large blank-area reward.
    similarities = 2*np.minimum(sample[None, :, :], bank).sum(axis=(1, 2))/np.maximum(sample.sum()+sums, 1e-6)
    scores = {letter: float(similarities[np.array(letters) == letter].max()) for letter in set(letters)}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return {"character": ranked[0][0], "score": ranked[0][1],
            "margin": ranked[0][1]-ranked[1][1]}


@lru_cache(maxsize=2)
def _bank(style):
    letters, arrays = [], []
    for letter, target in glyphs(style):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                letters.append(letter)
                arrays.append(np.roll(target, (dy, dx), axis=(0, 1)))
    bank = np.stack(arrays)
    return letters, bank, bank.sum(axis=(1, 2))


def read_fraction(image, region=None, *, style="list", minimum_score=.75, minimum_margin=.04):
    """Return current/capacity/full or None when unknown. Input is BGR."""
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.ndim not in (2, 3) or min(image.shape[:2]) < 6
            or (image.ndim == 3 and image.shape[2] != 3)):
        return None
    if region is not None:
        x, y, w, h = map(int, region)
        if x < 0 or y < 0 or w < 1 or h < 1 or x+w > image.shape[1] or y+h > image.shape[0]:
            return None
        image = image[y:y+h, x:x+w]
    ink = foreground(image)
    if ink is None:
        return None
    boxes = segment(ink)
    if style not in {"list", "selection"} or not 1 <= len(boxes) <= 5:
        return None
    def acceptable(item):
        return item is not None and item["score"] >= minimum_score and item["margin"] >= minimum_margin

    def trimmed_box(left, right, run):
        _, top, _, height = run
        patch = ink[top:top+height, left:right]
        _, mask = cv2.threshold(patch, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        ys = np.flatnonzero((mask > 0).any(axis=1))
        if not len(ys) or len(ys) < height*.75:
            return None
        return (left, top+int(ys[0]), right-left, int(ys[-1]-ys[0]+1))

    options = []
    for box in boxes:
        item = classify(ink, box, style)
        choices = [[item]] if acceptable(item) else []
        x, _, w, h = box
        # Italic /7 can touch. Try local character cuts, never whole-number
        # templates, and require both resulting glyphs to pass confidence.
        if w/h >= .85:
            for cut in range(x+3, x+w-2):
                left = trimmed_box(x, cut, box)
                right = trimmed_box(cut, x+w, box)
                if left is None or right is None:
                    continue
                pair = [classify(ink, left, style), classify(ink, right, style)]
                if all(acceptable(value) for value in pair):
                    choices.append(pair)
        if not choices:
            return None
        options.append(choices)
    candidates = {}
    for combination in product(*options):
        readings = [value for group in combination for value in group]
        text = "".join(item["character"] for item in readings)
        parts = text.split("/")
        if len(parts) != 2 or any(not p.isdigit() or not 1 <= len(p) <= 2 for p in parts):
            continue
        if any(len(p) > 1 and p.startswith("0") for p in parts):
            continue
        current, capacity = map(int, parts)
        if capacity <= 0 or current > capacity:
            continue
        score = min(item["score"] for item in readings)
        candidate = {"text": text, "current": current, "capacity": capacity,
                     "full": current == capacity, "score": score,
                     "margin": min(item["margin"] for item in readings), "glyphs": readings}
        if text not in candidates or score > candidates[text]["score"]:
            candidates[text] = candidate
    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0]["score"]-ranked[1]["score"] < minimum_margin):
        return None
    return ranked[0]
