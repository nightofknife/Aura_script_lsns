"""Conservative visual heuristics for the deep-dive board.

Scores are heuristic evidence strengths, not calibrated probabilities. A missed
or ambiguous candidate must remain unknown in the map fusion layer. In
particular, absence of a detection does not establish an empty cell.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


def _rgb(image: np.ndarray) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] < 3 or min(array.shape[:2]) < 8:
        raise ValueError("Expected an RGB image with at least 8 pixels per axis")
    if array.dtype == np.uint8:
        return np.ascontiguousarray(array[:, :, :3])
    return np.ascontiguousarray(np.clip(array[:, :, :3], 0, 255).astype(np.uint8))


def _contours(mask: np.ndarray, mode: int = cv2.RETR_EXTERNAL):
    return cv2.findContours(mask.astype(np.uint8) * 255, mode, cv2.CHAIN_APPROX_SIMPLE)


def _candidate(kind: str, contour: np.ndarray, confidence: float) -> dict:
    x, y, w, h = cv2.boundingRect(contour)
    moments = cv2.moments(contour)
    point = [x + w / 2, y + h / 2]
    if moments["m00"]:
        point = [moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]]
    return {"kind": kind, "point": point, "box": [x, y, w, h], "confidence": float(confidence)}


def detect_targets(image_rgb: np.ndarray) -> list[dict]:
    """Detect visible candidates in a normalized 1280 x 720 board region.

    Floating sprites' centers are returned, not their board contact points.
    Geometry and repeated observations must resolve the owning cell.
    """
    rgb = _rgb(image_rgb)
    original_h, original_w = rgb.shape[:2]
    if (original_w, original_h) != (1280, 720):
        rgb = cv2.resize(rgb, (1280, 720), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = cv2.split(hsv)
    roi = np.zeros((720, 1280), np.uint8)
    roi[78:622, 280:950] = 1
    # Reset-view control is drawn over the board and cannot be evidence.
    roi[506:579, 595:691] = 0
    allowed = roi.astype(bool)
    candidates = []

    # The pawn's spherical head has a nearly complete pale circular rim.
    # A pink connected component alone also matches cube walls and red glow.
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Accepted head centres are restricted to the board below. Keep 48px of
    # context beyond it for circle edges, filtering and neighbouring candidates.
    circles = cv2.HoughCircles(cv2.medianBlur(gray[30:670,232:998], 3), cv2.HOUGH_GRADIENT,
                               1, 14, param1=100, param2=14,
                               minRadius=8, maxRadius=16)
    if circles is not None:
        circles[0,:,0] += 232
        circles[0,:,1] += 30
    heads = []
    if circles is not None:
        yy, xx = np.mgrid[-24:25, -24:25]
        distances = np.sqrt(xx * xx + yy * yy)
        angles = np.arctan2(yy, xx)
        for cx, cy, radius in circles[0]:
            if not (280 < cx < 950 and 78 < cy < 622):
                continue
            # Mask the actual reset control and text, leaving nearby head
            # pixels available instead of masking the entire rectangular HUD.
            if (618 < cx < 663 and 506 < cy < 550) or (595 < cx < 691 and 550 < cy < 579):
                continue
            x, y = int(cx), int(cy)
            patch = hsv[y - 24:y + 25, x - 24:x + 25]
            pink = (patch[:, :, 0] >= 132) & (patch[:, :, 0] <= 177) & (patch[:, :, 1] > 35) & (patch[:, :, 2] > 85)
            pale = (patch[:, :, 1] < 110) & (patch[:, :, 2] > 175)
            inner = distances < radius * .70
            rim = (distances > radius * .85) & (distances < radius * 1.20)
            pink_fraction = float(pink[inner].mean())
            pale_fraction = float(pale[rim].mean())
            sectors = sum(np.count_nonzero(pale & rim & (angles >= -np.pi + i * np.pi / 6) & (angles < -np.pi + (i + 1) * np.pi / 6)) >= 2 for i in range(12))
            if pink_fraction < .83 or pale_fraction < .14 or sectors < 8:
                continue
            heads.append((float(radius), {"kind": "player", "point": [float(cx), float(cy)],
                "box": [int(cx - radius), int(cy - radius), int(radius * 2 + 1), int(radius * 2 + 1)],
                "confidence": float(round(min(.85, .62 + .12 * pink_fraction + .08 * sectors / 12), 3))}))
        # The broad base of the same pawn can also fit a circle. Within one
        # pawn-length retain the smaller sphere; do not infer a global winner.
        for radius, head in sorted(heads, key=lambda item: item[0]):
            if not any(other["kind"] == "player" and np.linalg.norm(np.array(head["point"]) - other["point"]) < 50 for other in candidates):
                candidates.append(head)

    # Inspiration has a thin yellow ring with a projecting star. Hexagonal
    # node icons have several internal cells; require one dominant open hole.
    yellow = (hue >= 20) & (hue <= 40) & (saturation > 65) & (value > 150) & allowed
    yellow = cv2.morphologyEx(yellow.astype(np.uint8), cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))) > 0
    contours, hierarchy = _contours(yellow, cv2.RETR_CCOMP)
    if hierarchy is not None:
        for index, contour in enumerate(contours):
            if hierarchy[0, index, 3] != -1:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if not (18 <= w <= 95 and 18 <= h <= 95 and 150 <= area <= 4300):
                continue
            child = int(hierarchy[0, index, 2])
            holes = []
            while child != -1:
                holes.append(contours[child])
                child = int(hierarchy[0, child, 0])
            holes = sorted(holes, key=cv2.contourArea, reverse=True)
            if not holes:
                # A lone side-facing star is useful occlusion evidence, but
                # must not be promoted to a confirmed inspiration location.
                hull_area = cv2.contourArea(cv2.convexHull(contour))
                polygon = cv2.approxPolyDP(contour, .035 * cv2.arcLength(contour, True), True)
                filled = np.zeros((720, 1280), np.uint8)
                cv2.drawContours(filled, [contour], -1, 1, -1)
                pixels = filled.astype(bool)
                bright_fraction = float(np.mean((value[pixels] > 160) & (saturation[pixels] > 100))) if pixels.any() else 0.0
                if w <= 38 and h <= 38 and area < 600 and area / max(hull_area, 1) < .76 and len(polygon) >= 6 and bright_fraction > .55:
                    candidates.append(_candidate("inspiration", contour, .40))
                continue
            if cv2.contourArea(holes[0]) / max(area, 1) < .42:
                continue
            # A broken hexagonal glyph may merge its internal cells into one
            # jagged cavity. The ring's empty interior remains nearly convex.
            hole_hull = cv2.contourArea(cv2.convexHull(holes[0]))
            if cv2.contourArea(holes[0]) / max(hole_hull, 1) < .85:
                continue
            if len(holes) > 1 and cv2.contourArea(holes[1]) > .16 * cv2.contourArea(holes[0]):
                continue
            # A dominant hollow interior survives perspective projection.
            # The star may be disconnected, so convex outer rings are valid.
            candidates.append(_candidate("inspiration", contour, .67))

    # The singularity's diffuse red halo covers a two-dimensional region;
    # tile seams and white block reflections are narrow. Smooth the red field
    # before finding connected regions so separate sparks belong to one halo.
    channels = rgb.astype(np.float32)
    red_value, green_value, blue_value = cv2.split(channels)
    diffuse = (red_value > 100) & (red_value > green_value * 1.6) & (red_value > blue_value * 1.25)
    halo_roi = np.zeros((720, 1280), bool)
    halo_roi[70:625, 280:970] = True
    diffuse &= halo_roi
    # sigma=20 on float32 uses radius 80. Preserve that zero context around
    # the nonzero halo ROI while avoiding convolution over unrelated HUD space.
    density = np.zeros((720,1280),np.float32)
    density[:706,199:1051] = cv2.GaussianBlur(diffuse[:706,199:1051].astype(np.float32), (0,0), 20)
    for contour in _contours(density > .56)[0]:
        x, y, w, h = cv2.boundingRect(contour)
        if not (28 <= w <= 180 and 28 <= h <= 180 and w * h >= 600):
            continue
        region = np.zeros((720, 1280), np.uint8)
        cv2.drawContours(region, [contour], -1, 1, -1)
        region = cv2.dilate(region, np.ones((31, 31), np.uint8)).astype(bool)
        sparkle = region & (red_value > 220) & (green_value > 100) & (blue_value > 110) & (red_value > green_value + 20)
        if np.count_nonzero(sparkle) < 45:
            continue
        x0, y0 = max(280, x - 14), max(70, y - 14)
        x1, y1 = min(970, x + w + 14), min(625, y + h + 14)
        candidates.append({"kind": "singularity", "point": [x + w / 2, y + h / 2],
                           "box": [x0, y0, x1 - x0, y1 - y0],
                           "_core_box": [x, y, w, h],
                           "confidence": .68 if min(w, h) >= 55 else .56})

    # Pale pink sparks inside the singularity can resemble the pawn. Only
    # suppress a pawn candidate when most of its area overlaps the unpadded
    # red core AND its center is near that core's center. A neighboring pawn
    # merely touched by the broad halo is deliberately retained.
    singularities = [item for item in candidates if item["kind"] == "singularity"]
    filtered = []
    for candidate in candidates:
        overlaps_core = False
        if candidate["kind"] == "player":
            px, py, pw, ph = candidate["box"]
            pcx, pcy = candidate["point"]
            for singularity in singularities:
                sx, sy, sw, sh = singularity["_core_box"]
                left, top, right, bottom = sx, sy, sx + sw, sy + sh
                core_w, core_h = max(1, right - left), max(1, bottom - top)
                overlap = max(0, min(px + pw, right) - max(px, left)) * max(0, min(py + ph, bottom) - max(py, top))
                centered = abs(pcx - (left + right) / 2) <= core_w * .48 and abs(pcy - (top + bottom) / 2) <= core_h * .48
                if centered and overlap / max(1, pw * ph) >= .70:
                    overlaps_core = True
                    break
        if not overlaps_core:
            filtered.append(candidate)

    # Merge nearby fragments of the same sprite, without imposing a global
    # count (a frame may show none, one or multiple inspiration targets).
    merged = []
    for candidate in sorted(filtered, key=lambda item: item["confidence"], reverse=True):
        if any(candidate["kind"] == other["kind"] and np.linalg.norm(np.array(candidate["point"]) - other["point"]) < 30 for other in merged):
            continue
        merged.append(candidate)
    sx, sy = original_w / 1280, original_h / 720
    for candidate in merged:
        candidate.pop("_core_box", None)
        candidate["point"] = [round(candidate["point"][0] * sx, 2), round(candidate["point"][1] * sy, 2)]
        x, y, w, h = candidate["box"]
        candidate["box"] = [round(x * sx), round(y * sy), round(w * sx), round(h * sy)]
    return merged


def _warm_glyph(rgb: np.ndarray) -> np.ndarray | None:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, s, v = cv2.split(hsv)
    mask = (((h <= 23) | (h >= 162)) & (s > 90) & (v > 100)).astype(np.uint8)
    mask[:16] = 0
    mask[80:] = 0
    mask[:, :16] = 0
    mask[:, 80:] = 0
    # Keep the central connected glyph; neighboring tile seams must not
    # become part of the shape used to distinguish the two eye symbols.
    count, labels, stats, centres = cv2.connectedComponentsWithStats(mask, 8)
    choices = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= 55
               and np.linalg.norm(centres[i] - [47.5, 47.5]) < 18]
    if not choices:
        return None
    index = max(choices, key=lambda i: stats[i, cv2.CC_STAT_AREA])
    x, y, w, height, area = stats[index]
    if min(w, height) < 19 or area / (w * height) > .8:
        return None
    glyph = (labels[y:y + height, x:x + w] == index).astype(np.float32)
    return cv2.resize(glyph, (64, 64), interpolation=cv2.INTER_AREA)


@lru_cache(maxsize=1)
def _eye_templates() -> dict[str, list[np.ndarray]]:
    # User recording 2026-09-27 16-39-22, frame at 0.100 s: red U21;
    # orange F10/R02/R11, rectified from the fitted initial cube geometry.
    root = Path(__file__).resolve().parents[2] / "templates" / "deep_dive_layout"
    result = {}
    for name in ("red_single_eye", "orange_triple_eye"):
        variants = []
        for path in sorted(root.glob(f"icon_{name}*.png")):
            raw = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if raw is None:
                continue
            glyph = _warm_glyph(cv2.cvtColor(raw, cv2.COLOR_BGR2RGB))
            if glyph is not None:
                variants.extend(np.ascontiguousarray(np.rot90(glyph, k)) for k in range(4))
        result[name] = variants
    return result


def _classify_eye_shape(rgb: np.ndarray) -> dict:
    glyph = _warm_glyph(rgb)
    if glyph is None:
        return {"icon_id": None, "confidence": 0.0}
    # Correlate foreground glyphs, allowing small rectification translations.
    # A score and a margin are both required: color does not break ties.
    padded = np.pad(glyph, 4)
    scores = []
    for name, variants in _eye_templates().items():
        best = max((float(cv2.minMaxLoc(cv2.matchTemplate(padded, variant, cv2.TM_CCORR_NORMED))[1])
                    for variant in variants), default=0.0)
        scores.append((best, name))
    scores.sort(reverse=True)
    if scores[0][0] < .78 or scores[0][0] - scores[1][0] < .055:
        return {"icon_id": None, "confidence": 0.0}
    return {"icon_id": scores[0][1], "confidence": round(min(.87, .58 + .29 * scores[0][0]), 3)}


def classify_icon(rectified_rgb_96x96: np.ndarray) -> dict:
    """Classify a rectified tile with color and red/orange shape evidence.

    Border-only color, bright filled side walls and mixed colors are rejected.
    The caller must skip target-occupied tiles and HUD/target-occluded patches.
    """
    rgb = cv2.resize(_rgb(rectified_rgb_96x96), (96, 96), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, s, v = cv2.split(hsv)
    center = np.zeros((96, 96), bool)
    center[19:77, 19:77] = True
    masks = {
        "white_diamond": (s < 62) & (v > 170),
        "blue_scales": (h >= 100) & (h <= 125) & (s > 100) & (v > 135),
        "green_burst": (h >= 40) & (h <= 91) & (s > 130) & (v > 135),
        "purple_ring": (h >= 126) & (h <= 148) & (s > 120) & (v > 130),
        "yellow_hex": (h >= 21) & (h <= 39) & (s > 110) & (v > 160),
        "warm_eye": ((h <= 23) | (h >= 162)) & (s > 100) & (v > 130),
    }
    ranked = sorted(((int(np.count_nonzero(mask & center)), name) for name, mask in masks.items()), reverse=True)
    count, name = ranked[0]
    if count < 65 or count < ranked[1][0] * 1.7 or count > 1650:
        return {"icon_id": None, "confidence": 0.0}
    mask = masks[name] & center
    yy, xx = np.nonzero(mask)
    width, height = int(xx.max() - xx.min() + 1), int(yy.max() - yy.min() + 1)
    # A glyph should span two axes and be centered, rather than a colored rim
    # or a uniformly filled portion of a protruding block's side wall.
    if min(width, height) < 15 or max(width, height) / min(width, height) > 3.0:
        return {"icon_id": None, "confidence": 0.0}
    if abs(float(xx.mean()) - 47.5) > 13 or abs(float(yy.mean()) - 47.5) > 13:
        return {"icon_id": None, "confidence": 0.0}
    if count / (width * height) > .70:
        return {"icon_id": None, "confidence": 0.0}
    middle = mask[32:64, 32:64]
    if np.count_nonzero(middle) < 18:
        return {"icon_id": None, "confidence": 0.0}
    if name == "warm_eye":
        return _classify_eye_shape(rgb)
    strength = min(1.0, count / 450)
    dominance = count / max(sum(item[0] for item in ranked), 1)
    return {"icon_id": name, "confidence": round(min(.83, .51 + .18 * strength + .14 * dominance), 3)}
