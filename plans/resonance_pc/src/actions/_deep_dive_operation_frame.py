"""Register a mode's current RGB view before binding a directed cube input.

This module has no input/capture API. Camera priors only propose geometry; a
frame becomes usable after a current-image three-face fit and joint atlas
registration. Layer previews are compared at the locked body pose, never fed
through the whole-cube tracker. All coordinates use a fresh scan's slot frame.
"""
from __future__ import annotations

import hashlib
import itertools
import math
import time
from functools import lru_cache

import cv2
import numpy as np

from ._deep_dive_layout_semantics import classify_icon, detect_targets
from ._deep_dive_layout_vision import K as _NORMAL_K, LayoutScanner, _crop, _point, _quad
from ._deep_dive_planner_rules import cell_to_slot, geometry, slot_to_cell, validate_slot
from ._deep_dive_single_run_vision import observe


_VISIBLE = tuple(range(27))  # Operation U/R/F, not logical U/R/F.
_CONFIDENCE = .68
_CONSENSUS = .88
_MIN_ANCHORS = 7
_MAX_RMSE = 6.
_Q_MARGIN = .08
_BUILD_BUDGET_SEC = 3.
_MAX_SEEDS = 162
_MAX_FITS = 8
_MODES = {"move": "choose_move", "rotate": "choose_rotate",
          "choose_move": "choose_move", "choose_rotate": "choose_rotate"}
_ARROWS = {"NW": (2, -1), "NE": (0, -1), "SW": (0, 1), "SE": (2, 1)}


def _cell(slot: int) -> dict:
    value = slot_to_cell(slot)
    return dict(face=value.face, row=value.row, col=value.col)


def _layout(layout: dict) -> tuple[dict[int, dict], int]:
    cells = {cell_to_slot(cell): cell for cell in layout.get("cells", ())}
    if len(cells) != 54:
        raise ValueError("operation_mapping_requires_54_unique_cells")
    players = [slot for slot, cell in cells.items()
               if cell.get("occupant") == "player"
               and cell.get("occupant_status") == "confirmed"]
    if len(players) != 1:
        raise ValueError("operation_mapping_requires_confirmed_unique_player")
    return cells, players[0]


def _known_icon(cell: dict) -> str | None:
    # Occupied cells can have a stale icon from an earlier observation. They
    # cannot become orientation evidence for the obscured underlying node.
    if cell.get("occupant", "unknown") != "none":
        return None
    if cell.get("node_status") != "known":
        return None
    if cell.get("icon_id") is None and cell.get("node_kind") == "empty":
        # A strong glyph over a transaction-confirmed empty node contradicts
        # this Q. Empty-looking pixels themselves are still not an anchor.
        return "__empty__"
    return cell.get("icon_id")


def _layout_digest(cells) -> str:
    values = tuple((slot, cell.get("occupant"), cell.get("occupant_status"),
                    cell.get("icon_id"), cell.get("node_status")) for slot, cell in sorted(cells.items()))
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def _reference_frame(reference, cells, actor, scan_epoch, map_revision, view_epoch):
    if (not isinstance(reference, dict) or reference.get("status") != "ready"
            or reference.get("mode") not in ("board", "choose_rotate")):
        raise ValueError("three_face_registration_reference_not_ready")
    if (reference.get("scan_epoch") != scan_epoch or reference.get("map_revision") != map_revision
            or reference.get("layout_digest") != _layout_digest(cells)
            or reference.get("actor_slot") != actor):
        raise ValueError("three_face_registration_reference_stale")
    if reference.get("view_epoch", view_epoch) >= view_epoch:
        raise ValueError("three_face_registration_requires_new_mode_view_epoch")
    quality = reference.get("geometry_quality", {})
    if (quality.get("anchors", 0) < _MIN_ANCHORS
            or len(quality.get("face_anchors", ())) != 3
            or min(quality.get("face_anchors", (0, 0, 0))) < 2
            or quality.get("rmse_px", float("inf")) > _MAX_RMSE
            or not reference.get("image_digest") or not reference.get("Q_candidates")
            or not isinstance(reference.get("actor_operation_slot"), int)
            or not 0 <= reference["actor_operation_slot"] < 9):
        raise ValueError("three_face_registration_reference_evidence_insufficient")
    for mapping in reference["Q_candidates"]:
        permutation = mapping.get("op_to_logical", ())
        if (len(permutation) != 54 or sorted(permutation) != list(range(54))
                or permutation[reference["actor_operation_slot"]] != actor):
            raise ValueError("three_face_registration_reference_mapping_invalid")
    if not reference.get("pose"):
        raise ValueError("three_face_registration_reference_pose_missing")
    return reference


def _camera(mode: str) -> tuple[np.ndarray, np.ndarray]:
    focal = 360. / math.tan(math.radians(4. if mode == "choose_move" else 6.))
    intrinsic = np.array(((focal, 0., 640.), (0., focal, 360.), (0., 0., 1.)))
    pitch = math.radians(29.6)
    root = math.sqrt(.5)
    row0 = np.array((root, 0., root))
    row1 = np.array((math.sin(pitch) * root, math.cos(pitch), -math.sin(pitch) * root))
    rotation = np.array((row0, row1, np.cross(row0, row1)))
    return intrinsic, cv2.Rodrigues(rotation)[0]


def _project(objects, intrinsic, rvec, tvec) -> np.ndarray:
    return cv2.projectPoints(np.asarray(objects, np.float64), np.asarray(rvec, np.float64),
                             np.asarray(tvec, np.float64), intrinsic, None)[0].reshape(-1, 2)


@lru_cache(maxsize=1)
def _render_geometry() -> np.ndarray:
    # One projection call for the complete 27-cell model, rather than a call
    # per centre and quad for every candidate camera seed.
    return np.asarray([np.concatenate([_point(_cell(slot))[None, :], _quad(_cell(slot))])
                       for slot in _VISIBLE], np.float64)


def _surface(intrinsic, rvec, tvec) -> list[dict]:
    projected = _project(_render_geometry().reshape(-1, 3), intrinsic, rvec, tvec).reshape(27, 5, 2)
    result = []
    for slot in _VISIBLE:
        cell = _cell(slot)
        centre, quad = projected[slot, 0], projected[slot, 1:]
        result.append(dict(operation_slot=slot, operation_face=cell["face"],
                           row=cell["row"], col=cell["col"],
                           centre=centre, quad=quad))
    return result


def _hud_mask(shape, *, preview=False) -> np.ndarray:
    mask = np.zeros(shape[:2], np.uint8)
    mask[82:619, 298:955] = 255
    mask[82:151, 585:706] = 0  # Screen-fixed plane title can obscure the pawn.
    mask[494:584, 587:698] = 0  # Reset View control.
    if preview:
        mask[505:590, 395:887] = 0  # Confirm/cancel overlays.
    return mask


def _candidate_shapes(rgb, mode) -> list[dict]:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, np.uint8((80, 135, 135)), np.uint8((105, 255, 255)))
    mask &= _hud_mask(rgb.shape)
    options = []
    if mode == "choose_move":
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if not (95 <= width <= 265 and 40 <= height <= 155 and area >= 600):
                continue
            polygon = cv2.approxPolyDP(contour, .035 * cv2.arcLength(contour, True), True)
            if not (4 <= len(polygon) <= 8) or not cv2.isContourConvex(cv2.convexHull(polygon)):
                continue
            moments = cv2.moments(contour)
            point = [x + width / 2, y + height / 2]
            if moments["m00"]:
                point = [moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]]
            options.append(dict(point=point, box=[x, y, width, height], evidence=area,
                                polygon=cv2.convexHull(polygon).reshape(-1, 2).tolist()))
    else:
        count, labels, stats, centres = cv2.connectedComponentsWithStats(mask)
        for index in range(1, count):
            x, y, width, height, area = map(int, stats[index])
            if not (25 <= width <= 70 and 10 <= height <= 45 and 260 <= area <= 2000
                    and 1.3 <= width / height <= 4.8 and area / (width * height) >= .42):
                continue
            component = (labels[y:y + height, x:x + width] == index).astype(np.uint8)
            contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if len(contours) != 1:
                continue
            contour = contours[0]
            hull_area = cv2.contourArea(cv2.convexHull(contour))
            solidity = cv2.contourArea(contour) / max(hull_area, 1.)
            # The genuine mesh arrow has a concave tail/head junction; its
            # solidity is around .64-.73. Axis text can be very solid but has
            # much less foreground area, already rejected above.
            if solidity < .58:
                continue
            options.append(dict(point=centres[index].tolist(), box=[x, y, width, height],
                                evidence=float(area), shape_evidence=dict(fill_ratio=area / (width * height), solidity=solidity)))
    unique = []
    for item in sorted(options, key=lambda row: -row["evidence"]):
        if all(np.linalg.norm(np.array(item["point"]) - old["point"]) > 28 for old in unique):
            unique.append(item)
    return unique


def _seed(intrinsic, rvec, scale, origin) -> tuple[np.ndarray, np.ndarray]:
    depth = float(intrinsic[0, 0]) / float(scale)
    tx = (float(origin[0]) - 640.) * depth / intrinsic[0, 0]
    ty = (float(origin[1]) - 360.) * depth / intrinsic[1, 1]
    return rvec.copy(), np.array((tx, ty, depth)).reshape(3, 1)


def _geometry_seeds(options, mode, intrinsic, rvec) -> list[dict]:
    """Enumerate small candidate constellations; never fit H from two points."""
    geo = geometry()
    seeds = []
    measured = np.asarray([item["point"] for item in options], float)
    reference = _surface(intrinsic, rvec, np.array((0., 0., 1000.)).reshape(3, 1))
    normalised = np.asarray([item["centre"] for item in reference]) - (640., 360.)
    normalised /= intrinsic[0, 0] / 1000.
    if mode == "choose_move":
        if not 2 <= len(options) <= 4:
            return []
        for actor in range(9):
            neighbours = geo.moves[actor, :geo.counts[actor]].tolist()
            if len(neighbours) < len(options):
                continue
            for ordering in itertools.permutations(neighbours, len(options)):
                model = normalised[list(ordering)]
                centred, observed = model - model.mean(axis=0), measured - measured.mean(axis=0)
                scale = float(np.sum(centred * observed) / max(np.sum(centred ** 2), 1e-9))
                if not 35 < scale < 165:
                    continue
                origin = measured.mean(axis=0) - model.mean(axis=0) * scale
                error = float(np.sqrt(np.mean(np.sum((model * scale + origin - measured) ** 2, axis=1))))
                if error > 15:
                    continue
                rv, tv = _seed(intrinsic, rvec, scale, origin)
                seeds.append(dict(rvec=rv, tvec=tv, actor=actor, seed_error=error))
    else:
        if not 2 <= len(options) <= 6:
            return []
        # Four arrows can be isolated from stray cyan components by subsets.
        subsets = list(itertools.combinations(range(len(options)), min(4, len(options))))
        if len(options) >= 4:
            ranked_subsets = []
            for subset in subsets:
                selected = measured[list(subset)]
                vectors = selected - selected.mean(axis=0)
                labels = {(int(dx > 0), int(dy > 0)) for dx, dy in vectors}
                radii = np.linalg.norm(vectors, axis=1)
                if len(labels) == 4 and radii.min() > 20 and radii.max() / radii.min() < 1.75:
                    ranked_subsets.append((float(radii.std() / radii.mean()), subset))
            subsets = [subset for _, subset in sorted(ranked_subsets)[:1]]
        for subset in subsets[:1]:
            centre = measured[list(subset)].mean(axis=0)
            for actor in range(9):
                for scale in (45., 60., 75., 90., 100., 110., 135.):
                    for elevation in (.08, .35, .65, .95):
                        rv, tv = _seed(intrinsic, rvec, scale, (640., 360.))
                        cell = _cell(actor)
                        pawn = _point(cell) + np.array((0., -elevation, 0.))
                        offset = _project([pawn], intrinsic, rv, tv)[0] - (640., 360.)
                        rv, tv = _seed(intrinsic, rvec, scale, centre - offset)
                        seeds.append(dict(rvec=rv, tvec=tv, actor=actor, seed_error=0.))
    return seeds


def _features(rgb, targets, *, preview=False) -> dict:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = cv2.split(hsv)
    glyph = (((saturation > 125) & (value > 155) & ((hue < 80) | (hue > 105)))
             | ((saturation < 65) & (value > 195))).astype(np.uint8) * 255
    allowed = _hud_mask(rgb.shape, preview=preview)
    for target in targets:
        x, y, width, height = map(int, target["box"])
        cv2.rectangle(allowed, (max(0, x - 8), max(0, y - 8)),
                      (min(1279, x + width + 8), min(719, y + height + 8)), 0, -1)
    glyph &= allowed
    binary = (glyph > 0).astype(np.float32)
    return dict(glyph=glyph, allowed=allowed, all_pixels=np.ones(allowed.shape, np.uint8),
                mass=cv2.integral(binary, sdepth=cv2.CV_64F),
                mass_x=cv2.integral(binary * np.arange(rgb.shape[1], dtype=np.float32)[None, :], sdepth=cv2.CV_64F),
                mass_y=cv2.integral(binary * np.arange(rgb.shape[0], dtype=np.float32)[:, None], sdepth=cv2.CV_64F))


def _patch_pixels(mask, polygon):
    x, y, width, height = cv2.boundingRect(np.float32(polygon))
    x0, y0, x1, y1 = max(0, x), max(0, y), min(mask.shape[1], x + width), min(mask.shape[0], y + height)
    if x1 <= x0 or y1 <= y0:
        return np.empty(0), np.empty(0)
    inner = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillConvexPoly(inner, np.int32(polygon - (x0, y0)), 255)
    yy, xx = np.nonzero((inner > 0) & (mask[y0:y1, x0:x1] > 0))
    return yy + y0, xx + x0


def _coarse_score(surface, features, actor):
    # Constant-time rectangular moment lookups rank seeds only. Precise
    # convex-patch classification and three-face PnP remain mandatory later.
    centres = np.asarray([item["centre"] for item in surface])
    quads = np.asarray([item["quad"] for item in surface])
    half = np.maximum(np.ptp(quads, axis=1) * .19, 6.)
    low = np.floor(centres - half).astype(int)
    high = np.ceil(centres + half).astype(int)
    low[:, 0], high[:, 0] = np.clip(low[:, 0], 0, 1280), np.clip(high[:, 0], 0, 1280)
    low[:, 1], high[:, 1] = np.clip(low[:, 1], 0, 720), np.clip(high[:, 1], 0, 720)
    def sums(integral):
        return (integral[high[:, 1], high[:, 0]] - integral[low[:, 1], high[:, 0]]
                - integral[high[:, 1], low[:, 0]] + integral[low[:, 1], low[:, 0]])
    mass = sums(features["mass"])
    observed = np.column_stack((sums(features["mass_x"]), sums(features["mass_y"]))) / np.maximum(mass[:, None], 1.)
    distances = np.linalg.norm(observed - centres, axis=1)
    shifted = np.roll(quads, -1, axis=1)
    areas = np.abs(np.sum(quads[:, :, 0] * shifted[:, :, 1] - shifted[:, :, 0] * quads[:, :, 1], axis=1)) / 2
    supported = (mass >= 28) & (areas >= 450) & (distances < np.maximum(9., np.sqrt(areas) * .25))
    supported[actor] = False
    counts = np.bincount(np.flatnonzero(supported) // 9, minlength=3)
    return float(counts.sum() + counts.min() * 2 - np.median(distances[supported]) / 10.) if supported.any() else -100.


def _colour_mask(crop, icon):
    hue, saturation, value = cv2.split(cv2.cvtColor(crop, cv2.COLOR_RGB2HSV))
    masks = {
        "white_diamond": (saturation < 62) & (value > 170),
        "blue_scales": (hue >= 100) & (hue <= 125) & (saturation > 100) & (value > 135),
        "green_burst": (hue >= 40) & (hue <= 91) & (saturation > 130) & (value > 135),
        "purple_ring": (hue >= 126) & (hue <= 148) & (saturation > 120) & (value > 130),
        "yellow_hex": (hue >= 21) & (hue <= 39) & (saturation > 110) & (value > 160),
        "red_single_eye": ((hue <= 6) | (hue >= 168)) & (saturation > 140) & (value > 140),
        "orange_triple_eye": (hue >= 7) & (hue <= 20) & (saturation > 110) & (value > 150),
    }
    return masks.get(icon)


def _classify_operation_crop(crop):
    original = classify_icon(crop)
    if original.get("icon_id"):
        return original, crop, 1.
    # Active mode dims unselected node glyphs. Exposure is bounded, and the
    # same independent colour/shape classifier must agree at two gains. Never
    # use the atlas's expected label to manufacture an observed label.
    variants = []
    for gain in (1.35, 1.65, 1.95):
        adjusted = np.clip(crop.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        classification = classify_icon(adjusted)
        icon = classification.get("icon_id")
        # Brightness alone can turn pale eye pixels/white UI into a purported
        # white diamond. Original white evidence remains required for that id.
        if icon and icon != "white_diamond" and classification.get("confidence", 0.) >= _CONFIDENCE:
            variants.append((icon, classification, adjusted, gain))
    if len(variants) < 2 or len({row[0] for row in variants}) != 1:
        return dict(icon_id=None, confidence=0.), crop, 1.
    _, classification, adjusted, gain = variants[0]
    classification = dict(classification, confidence=min(.80, classification["confidence"]))
    return classification, adjusted, gain


def _readings(rgb, surface, features, *, actor=None) -> list[dict]:
    readings = []
    for item in surface:
        if item["operation_slot"] == actor:
            continue
        quad, centre = item["quad"], item["centre"]
        area = cv2.contourArea(np.float32(quad))
        if area < 650 or not np.isfinite(quad).all():
            continue
        # Visibility applies to the actual central glyph footprint. A far
        # outside tile corner is not evidence that a fully visible glyph is
        # obscured; conversely a partial/HUD-covered centre is never an anchor.
        inner = centre + (quad - centre) * .60
        yy, xx = _patch_pixels(features["all_pixels"], inner)
        if not len(xx) or np.mean(features["allowed"][yy, xx] > 0) < .98:
            continue
        crop = _crop(rgb, quad)
        classification, measured_crop, photo_scale = _classify_operation_crop(crop)
        icon, confidence = classification.get("icon_id"), classification.get("confidence", 0.)
        if not icon or confidence < _CONFIDENCE:
            continue
        mask = _colour_mask(measured_crop, icon)
        if mask is None:
            continue
        mask[:13] = False
        mask[83:] = False
        mask[:, :13] = False
        mask[:, 83:] = False
        yy, xx = np.nonzero(mask)
        if len(xx) < 50:
            continue
        allowed_crop = _crop(np.repeat(features["allowed"][:, :, None], 3, axis=2), quad)[:, :, 0]
        if np.mean(allowed_crop[yy, xx] > 250) < .98:
            continue
        midpoint = np.float32([[(np.percentile(xx, 3) + np.percentile(xx, 97)) / 2,
                                (np.percentile(yy, 3) + np.percentile(yy, 97)) / 2]])
        inverse = cv2.getPerspectiveTransform(np.float32(((0, 0), (95, 0), (95, 95), (0, 95))), np.float32(quad))
        pixel = cv2.perspectiveTransform(midpoint.reshape(1, 1, 2), inverse).reshape(2)
        if np.linalg.norm(pixel - centre) > max(12., np.sqrt(area) * .3):
            continue
        readings.append(dict(operation_slot=item["operation_slot"], icon_id=icon,
                             confidence=float(confidence), point=pixel, area=float(area), photo_scale=photo_scale))
    return readings


def _top_support(readings):
    slots = [row["operation_slot"] for row in readings]
    if len(slots) < 5 or len({slot // 3 for slot in slots}) < 2 or len({slot % 3 for slot in slots}) < 2:
        return False
    pixels = np.float32([row["point"] for row in readings])
    return cv2.contourArea(cv2.convexHull(pixels)) >= 3000.


def _refine(rgb, seed, intrinsic, features, *, top_only=False):
    rvec, tvec = seed["rvec"].copy(), seed["tvec"].copy()
    seed_rotation = cv2.Rodrigues(rvec)[0]
    rmse, inlier_count = float("inf"), 0
    for _ in range(2):
        surface = _surface(intrinsic, rvec, tvec)
        readings = _readings(rgb, surface[:9] if top_only else surface, features, actor=seed["actor"])
        if top_only:
            if not _top_support(readings):
                return None
        elif len(readings) < _MIN_ANCHORS or any(sum(row["operation_slot"] // 9 == face for row in readings) < 2 for face in range(3)):
            return None
        objects = np.asarray([_point(_cell(row["operation_slot"])) for row in readings], np.float64)
        pixels = np.asarray([row["point"] for row in readings], np.float64)
        if top_only:
            # Generic RANSAC's planar minimal solver can pick the reflected
            # face branch even when a valid canonical-pose guess is supplied.
            # Refine locally from the independently established FaceUp body
            # orientation, then retain actual residual-supported centres.
            ok, fitted_r, fitted_t = cv2.solvePnP(
                objects, pixels, intrinsic, None, rvec=rvec.copy(), tvec=tvec.copy(),
                useExtrinsicGuess=True, flags=cv2.SOLVEPNP_ITERATIVE)
            errors = np.linalg.norm(_project(objects, intrinsic, fitted_r, fitted_t) - pixels, axis=1)
            inliers = np.flatnonzero(errors <= 7.).reshape(-1, 1) if ok else None
        else:
            ok, fitted_r, fitted_t, inliers = cv2.solvePnPRansac(
                objects, pixels, intrinsic, None, rvec=rvec, tvec=tvec,
                useExtrinsicGuess=True, iterationsCount=80, reprojectionError=7.,
                confidence=.995, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok or inliers is None or len(inliers) < (5 if top_only else _MIN_ANCHORS):
            return None
        indices = inliers.ravel()
        if top_only:
            if not _top_support([readings[index] for index in indices]):
                return None
        elif any(sum(readings[index]["operation_slot"] // 9 == face for index in indices) < 2 for face in range(3)):
            return None
        turn = np.degrees(np.linalg.norm(cv2.Rodrigues(cv2.Rodrigues(fitted_r)[0] @ seed_rotation.T)[0]))
        depth = float(fitted_t[2, 0])
        if turn > (3. if top_only else 12.) or not intrinsic[0, 0] / 170. < depth < intrinsic[0, 0] / 33.:
            return None
        fitted = _project(objects[indices], intrinsic, fitted_r, fitted_t)
        error = np.linalg.norm(fitted - pixels[indices], axis=1)
        rmse = float(np.sqrt(np.mean(error ** 2)))
        if rmse > _MAX_RMSE or float(np.max(error)) > 9.:
            return None
        rvec, tvec, inlier_count = fitted_r, fitted_t, len(indices)
    surface = _surface(intrinsic, rvec, tvec)
    readings = _readings(rgb, surface[:9] if top_only else surface, features, actor=seed["actor"])
    if top_only and not _top_support(readings):
        return None
    return dict(rvec=rvec, tvec=tvec, surface=surface, readings=readings,
                actor=seed["actor"], rmse=rmse, inliers=inlier_count)


def _q_candidates(actor: int, actor_operation: int):
    geo = geometry()
    for index, permutation in enumerate(geo.symmetries):
        if permutation[actor_operation] == actor and permutation[4] // 9 == actor // 9:
            matrix = np.column_stack([geo.normals[permutation[slot]] for slot in (9, 27, 45)])
            yield dict(symmetry_id=index, Q=matrix.tolist(), op_to_logical=permutation[:54].tolist())


def _registration(cells, fit, actor, *, reference=None):
    result = []
    candidates = ([dict(row) for row in reference["Q_candidates"]] if reference is not None
                  else _q_candidates(actor, fit["actor"]))
    for candidate in candidates:
        permutation = candidate["op_to_logical"]
        matches, conflicts, counts = [], [], [0, 0, 0]
        for reading in fit["readings"]:
            expected = _known_icon(cells[permutation[reading["operation_slot"]]])
            if expected is None:
                continue
            if expected == reading["icon_id"]:
                counts[reading["operation_slot"] // 9] += 1
                matches.append(reading)
            else:
                conflicts.append(reading)
        total = len(matches) + len(conflicts)
        confidence = sum(row["confidence"] for row in matches) / max(
            sum(row["confidence"] for row in matches + conflicts), 1e-9)
        sufficient = (len(matches) >= 5 and counts[0] >= 5 if reference is not None
                      else len(matches) >= _MIN_ANCHORS and min(counts) >= 2)
        if sufficient and len(conflicts) <= 1 and confidence >= _CONSENSUS:
            candidate.update(anchors=total, matched=len(matches), conflicts=len(conflicts),
                             face_anchors=counts, consensus=confidence,
                             score=confidence + min(total, 18) * .012 - fit["rmse"] * .003)
            result.append(candidate)
    if not result:
        return []
    best = max(row["score"] for row in result)
    return [row for row in result if row["score"] >= best - _Q_MARGIN]


def _camera_transition(fit, reference):
    rotation = cv2.Rodrigues(fit["rvec"])[0]
    old_rotation = cv2.Rodrigues(np.asarray(reference["pose"]["rvec"], float))[0]
    delta = np.degrees(np.linalg.norm(cv2.Rodrigues(rotation @ old_rotation.T)[0]))
    old_t = np.asarray(reference["pose"]["tvec"], float).reshape(3)
    change = fit["tvec"].reshape(3) - old_t
    # The mode switch changes camera Y and FOV, not yaw/roll or lateral X.
    # Broad translation bounds allow the normal glyph-fit residual, while
    # rejecting a different body pose or an arbitrary new camera depth.
    good = (delta <= 3. and abs(change[0]) <= .25 and -.20 <= change[1] <= 1.75
            and abs(change[2]) <= max(2., abs(old_t[2]) * .08))
    return dict(confirmed=bool(good), rotation_delta_degrees=float(delta),
                translation_delta=change.tolist(), same_canonical_faceup=bool(good))


def _arrow_labels(options, fit, intrinsic) -> list[dict]:
    points = np.asarray([row["point"] for row in options], float)
    cell = _cell(fit["actor"])
    corridor = _project([_point(cell) + (0., -height, 0.) for height in (.1, .35, .6, .85, 1.1)],
                        intrinsic, fit["rvec"], fit["tvec"])
    if len(points) == 4:
        centre = points.mean(axis=0)
        if np.min(np.linalg.norm(corridor - centre, axis=1)) > 18:
            return []
    else:
        centres = []
        for first, second in itertools.combinations(points, 2):
            midpoint = (first + second) / 2
            if np.min(np.linalg.norm(corridor - midpoint, axis=1)) < 15:
                centres.append(midpoint)
        if centres and np.max(np.linalg.norm(np.asarray(centres) - np.mean(centres, axis=0), axis=1)) <= 8:
            centre = np.mean(centres, axis=0)
        else:
            centre = corridor[2]
    result = []
    for row in options:
        dx, dy = np.asarray(row["point"]) - centre
        radius = math.hypot(dx, dy)
        slope = abs(dy) / max(abs(dx), 1e-9)
        if not (20 < radius < 155 and abs(dx) > 10 and abs(dy) > 5 and .18 < slope < 1.4):
            continue
        label = ("N" if dy < 0 else "S") + ("W" if dx < 0 else "E")
        item = dict(row, arrow_label=label, relative_to_player=centre.tolist())
        axis, sign = _ARROWS[label]
        point = geometry().points[fit["actor"]]
        spec = (axis, int(point[axis]), sign)
        item["operation_rotation_id"] = geometry().rotation_specs.index(spec)
        result.append(item)
    # No missing quadrant is fabricated, and duplicates invalidate that label.
    return [row for row in result if sum(other["arrow_label"] == row["arrow_label"] for other in result) == 1]


def _move_labels(options, fit) -> list[dict]:
    geo = geometry()
    allowed = set(map(int, geo.moves[fit["actor"], :geo.counts[fit["actor"]]]))
    result = []
    for row in options:
        point = np.asarray(row["point"], float)
        candidates = []
        for item in fit["surface"][:9]:
            if item["operation_slot"] not in allowed:
                continue
            quad = np.float32(item["quad"])
            if cv2.pointPolygonTest(quad, tuple(map(float, point)), False) < 0:
                continue
            distance = float(np.linalg.norm(point - item["centre"]))
            if distance <= max(12., np.sqrt(cv2.contourArea(quad)) * .27):
                candidates.append(item["operation_slot"])
        if len(candidates) == 1:
            result.append(dict(row, operation_slot=candidates[0]))
    return [row for row in result if sum(other["operation_slot"] == row["operation_slot"] for other in result) == 1]


def build_wide_reference_frame(image_rgb, layout, *, scan_epoch=0, map_revision=0, view_epoch=0) -> dict:
    """Register the ordinary wide board after a runtime-controlled Reset View.

    The reset and unchanged HUD/actor are caller preconditions. A temporary
    scanner supplies only a current-image ordinary-camera geometry proposal;
    current glyph classification on all three planes must independently agree
    with one proper Q before this reference can be used for an operation mode.
    """
    deadline = time.monotonic() + _BUILD_BUDGET_SEC
    base = dict(status="waiting", reason="wide_reset_reference_unconfirmed", mode="board",
                scan_epoch=scan_epoch, map_revision=map_revision, view_epoch=view_epoch,
                Q=None, op_to_logical=None, Q_candidates=[], grid_cells=[], candidates=[], evidence={})
    rgb = np.asarray(image_rgb)
    if rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
        return dict(base, status="blocked", reason="operation_frame_requires_1280x720_rgb")
    try:
        cells, actor = _layout(layout)
    except (ValueError, KeyError, TypeError) as error:
        return dict(base, status="blocked", reason=str(error))
    base["actor_slot"] = actor
    observation = observe(rgb)
    base["evidence"]["scene"] = observation.get("scene")
    if not observation.get("player_turn") or observation.get("scene") not in ("board", "unknown"):
        return dict(base, reason="ordinary_wide_player_board_not_visible")
    cyan = observation.get("cyan_pixels", {})
    if cyan.get("move", 0) >= 2000 or cyan.get("rotate", 0) >= 2000:
        return dict(base, reason="ordinary_wide_board_has_selected_mode")
    proposal = LayoutScanner()
    if not proposal._bootstrap(rgb):
        base["evidence"]["ordinary_geometry_reason"] = proposal.last_error
        return dict(base, reason="ordinary_reset_geometry_unconfirmed")
    targets = detect_targets(rgb)
    features = _features(rgb, targets)
    solutions = []
    for operation_actor in range(9):
        if next(_q_candidates(actor, operation_actor), None) is None:
            continue
        if time.monotonic() >= deadline:
            return dict(base, reason="operation_geometry_budget_exhausted")
        seed = dict(rvec=proposal.rvec.copy(), tvec=proposal.tvec.copy(), actor=operation_actor)
        fit = _refine(rgb, seed, _NORMAL_K, features)
        if fit is None:
            continue
        registrations = _registration(cells, fit, actor)
        if registrations:
            fit.update(registration=registrations, score=max(row["score"] for row in registrations))
            solutions.append(fit)
    if not solutions:
        return dict(base, reason="wide_three_face_geometry_or_content_unconfirmed")
    solutions.sort(key=lambda row: -row["score"])
    fit = solutions[0]
    mappings = {row["symmetry_id"]: row for row in fit["registration"]}
    for other in solutions[1:]:
        if other["score"] < fit["score"] - _Q_MARGIN:
            continue
        displacement = np.mean([np.linalg.norm(a["centre"] - b["centre"])
                                for a, b in zip(fit["surface"], other["surface"])])
        if other["actor"] != fit["actor"] or displacement > 10:
            return dict(base, reason="operation_geometry_ambiguous")
        for row in other["registration"]:
            if row["score"] >= fit["score"] - _Q_MARGIN:
                mappings.setdefault(row["symmetry_id"], row)
    mappings = list(mappings.values())
    surface = [dict(row, centre=row["centre"].tolist(), quad=row["quad"].tolist(),
                    logical_slots=sorted({mapping["op_to_logical"][row["operation_slot"]] for mapping in mappings}))
               for row in fit["surface"]]
    top_h = cv2.getPerspectiveTransform(np.float32(((0, 0), (2, 0), (2, 2), (0, 2))),
                                        np.float32([fit["surface"][index]["centre"] for index in (0, 2, 8, 6)]))
    base.update(status="ready", reason="ordinary_wide_three_face_registration_confirmed",
                actor_operation_slot=fit["actor"], Q_candidates=mappings, grid_cells=surface,
                topH=top_h.tolist(), target_candidates=targets, layout_digest=_layout_digest(cells),
                pose=dict(K=_NORMAL_K.tolist(), rvec=fit["rvec"].ravel().tolist(), tvec=fit["tvec"].ravel().tolist()),
                camera_model=dict(kind="ordinary_reset_calibration", fov_degrees=12., frame_size=[1280, 720]),
                geometry_quality=dict(rmse_px=fit["rmse"], inliers=fit["inliers"],
                                      anchors=max(row["anchors"] for row in mappings),
                                      face_anchors=mappings[0]["face_anchors"], consensus=min(row["consensus"] for row in mappings)),
                stability_signature=np.round(np.asarray([row["centre"] for row in fit["surface"]]), 1).tolist(),
                image_digest=hashlib.sha256(cv2.resize(rgb[82:619, 298:955], (64, 64)).tobytes()).hexdigest())
    base["evidence"].update(proof_source="ordinary_wide_board_after_controlled_reset", Q_count=len(mappings),
                             independent_faces=["U", "R", "F"], geometry_solutions=len(solutions),
                             readings=[dict(row, point=row["point"].tolist()) for row in fit["readings"]],
                             requires_verified_reset_and_unchanged_hud=True)
    if len(mappings) == 1:
        base.update(Q=mappings[0]["Q"], op_to_logical=mappings[0]["op_to_logical"])
    return base


def build_operation_frame(image_rgb, layout, mode, *, scan_epoch=0, map_revision=0, view_epoch=0,
                          registration_frame=None) -> dict:
    """Fit a new mode view and map its three visible faces to one logical cube.

    One image does not establish temporal settling. The runtime must require
    consecutive stable frames with the same epochs before issuing an input.
    With registration_frame, the runtime must additionally prove unchanged
    HUD quotas/round/plane and an input ledger containing only the mode toggle.
    This function checks the referenced atlas and current camera/content; it
    does not independently certify that runtime transition or its HUD values.
    """
    deadline = time.monotonic() + _BUILD_BUDGET_SEC
    base = dict(status="waiting", reason="operation_view_not_registered", mode=_MODES.get(mode, mode),
                scan_epoch=scan_epoch, map_revision=map_revision, view_epoch=view_epoch,
                Q=None, op_to_logical=None, Q_candidates=[], grid_cells=[], candidates=[], evidence={})
    rgb = np.asarray(image_rgb)
    if rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
        return dict(base, status="blocked", reason="operation_frame_requires_1280x720_rgb")
    if mode not in _MODES:
        return dict(base, status="blocked", reason="unsupported_operation_mode")
    mode = _MODES[mode]
    try:
        cells, actor = _layout(layout)
    except (ValueError, KeyError, TypeError) as error:
        return dict(base, status="blocked", reason=str(error))
    base["actor_slot"] = actor
    reference = None
    if registration_frame is not None:
        if mode != "choose_move":
            return dict(base, status="blocked", reason="cross_view_registration_only_supports_move")
        try:
            reference = _reference_frame(registration_frame, cells, actor, scan_epoch, map_revision, view_epoch)
        except (ValueError, KeyError, TypeError) as error:
            return dict(base, status="blocked", reason=str(error))
    observation = observe(rgb)
    base["evidence"]["scene"] = observation.get("scene")
    cyan = observation.get("cyan_pixels", {})
    selected_mode = (observation.get("player_turn") and cyan.get("move" if mode == "choose_move" else "rotate", 0) >= 2000
                     and cyan.get("rotate" if mode == "choose_move" else "move", 0) < 2000)
    # The older scene recognizer's fixed arrow ROI can miss a pawn on a lower
    # row. A current active-mode button plus our independent extended-ROI
    # shapes may establish that mode; overlays/battle never use this fallback.
    if observation.get("scene") != mode and not (
            observation.get("scene") in ("unknown", "board") and selected_mode):
        return dict(base, reason="operation_mode_not_visible")
    options = _candidate_shapes(rgb, mode)
    if mode == "choose_move" and len(options) > 4:
        return dict(base, status="blocked", reason="unmodelled_move_candidates", evidence=dict(candidate_count=len(options)))
    if len(options) < 2:
        return dict(base, reason="operation_candidates_insufficient")
    intrinsic, prior_r = _camera(mode)
    if reference is not None:
        prior_r = np.asarray(reference["pose"]["rvec"], float).reshape(3, 1)
        # The ordinary scanner's lens calibration and the resource-derived
        # operation camera prior use different absolute focal normalisations.
        # Carry the validated wide calibration through the known 12 -> 8 FOV
        # change, then fit all current positions afresh from this image.
        old_k = np.asarray(reference["pose"]["K"], float)
        ratio = _camera("choose_move")[0][0, 0] / _camera("choose_rotate")[0][0, 0]
        intrinsic[0, 0], intrinsic[1, 1] = old_k[0, 0] * ratio, old_k[1, 1] * ratio
    targets = detect_targets(rgb)
    features = _features(rgb, targets)
    seeds = _geometry_seeds(options, mode, intrinsic, prior_r)
    if reference is not None:
        seeds = [row for row in seeds if row["actor"] == reference["actor_operation_slot"]]
    else:
        # A proper FaceUp rotation preserves centre/edge/corner membership.
        # For a centre actor only operation slot 4 is possible; spending the
        # fit budget on eight impossible actor positions can hide the real
        # camera seed behind bright but unrelated captions/side-wall patches.
        seeds = [row for row in seeds if next(_q_candidates(actor, row["actor"]), None) is not None]
    seeds = seeds[:_MAX_SEEDS]
    ranked = []
    for row in seeds:
        if time.monotonic() >= deadline:
            return dict(base, reason="operation_geometry_budget_exhausted")
        score = float(_coarse_score(_surface(intrinsic, row["rvec"], row["tvec"]), features, row["actor"]))
        ranked.append((score, row))
    ranked.sort(key=lambda row: -row[0])
    # Keep diverse grid placements; duplicate seeds must not consume all fits.
    selected = []
    for score, seed in ranked:
        if score < (0. if reference is not None else 5.):
            break
        signature = _project([_point(_cell(4))], intrinsic, seed["rvec"], seed["tvec"])[0]
        if all(old["actor"] != seed["actor"] or np.linalg.norm(signature - old["signature"]) > 12
               or abs(float(seed["tvec"][2, 0] - old["tvec"][2, 0])) > 4 for old in selected):
            selected.append(dict(seed, signature=signature))
        if len(selected) >= _MAX_FITS:
            break
    solutions = []
    for seed in selected:
        if time.monotonic() >= deadline:
            # Incomplete exploration must not silently choose an unchallenged
            # grid placement. Let the runtime request another settled frame.
            return dict(base, reason="operation_geometry_budget_exhausted")
        fit = _refine(rgb, seed, intrinsic, features, top_only=reference is not None)
        if fit is None:
            continue
        if reference is not None:
            fit["camera_transition"] = _camera_transition(fit, reference)
            if not fit["camera_transition"]["confirmed"]:
                continue
        registration = _registration(cells, fit, actor, reference=reference)
        if registration:
            fit["registration"] = registration
            fit["score"] = max(row["score"] for row in registration)
            solutions.append(fit)
    if not solutions:
        base["evidence"].update(geometry_seeds=len(seeds), fitted_seeds=len(selected))
        return dict(base, reason="three_face_geometry_or_content_unconfirmed")
    solutions.sort(key=lambda row: -row["score"])
    fit = solutions[0]
    mappings = {row["symmetry_id"]: row for row in fit["registration"]}
    for other in solutions[1:]:
        if other["score"] < fit["score"] - _Q_MARGIN:
            continue
        displacement = np.mean([np.linalg.norm(a["centre"] - b["centre"])
                                for a, b in zip(fit["surface"], other["surface"])])
        if other["actor"] != fit["actor"] or displacement > 10:
            return dict(base, reason="operation_geometry_ambiguous")
        for row in other["registration"]:
            if row["score"] >= fit["score"] - _Q_MARGIN:
                mappings.setdefault(row["symmetry_id"], row)
    mappings = list(mappings.values())
    candidates = (_move_labels(options, fit) if mode == "choose_move"
                  else _arrow_labels(options, fit, intrinsic))
    if mode == "choose_move" and len(candidates) != len(options):
        allowed = set(map(int, geometry().moves[fit["actor"], :geometry().counts[fit["actor"]]]))
        for option in options:
            point = tuple(map(float, option["point"]))
            for item in fit["surface"]:
                if item["operation_slot"] in allowed:
                    continue
                if (cv2.pointPolygonTest(np.float32(item["quad"]), point, False) >= 0
                        and np.linalg.norm(np.asarray(point) - item["centre"]) <= max(
                            12., np.sqrt(cv2.contourArea(np.float32(item["quad"]))) * .27)):
                    return dict(base, status="blocked", reason="unmodelled_move_candidates")
        return dict(base, reason="blue_candidate_geometry_unconfirmed")
    if not candidates:
        return dict(base, reason="operation_candidates_do_not_match_fitted_grid")
    serial_surface = []
    for item in fit["surface"]:
        serial_surface.append(dict(item, centre=item["centre"].tolist(), quad=item["quad"].tolist(),
                                   logical_slots=sorted({row["op_to_logical"][item["operation_slot"]] for row in mappings})))
    for candidate in candidates:
        if "operation_slot" in candidate:
            candidate["logical_slots"] = sorted({row["op_to_logical"][candidate["operation_slot"]] for row in mappings})
    top = fit["surface"][:9]
    top_h = cv2.getPerspectiveTransform(np.float32(((0, 0), (2, 0), (2, 2), (0, 2))),
                                        np.float32([top[index]["centre"] for index in (0, 2, 8, 6)]))
    pose = dict(K=intrinsic.tolist(), rvec=fit["rvec"].ravel().tolist(), tvec=fit["tvec"].ravel().tolist())
    quality = dict(rmse_px=fit["rmse"], inliers=fit["inliers"],
                   anchors=max(row["anchors"] for row in mappings),
                   face_anchors=mappings[0]["face_anchors"], consensus=min(row["consensus"] for row in mappings))
    base.update(status="ready", reason="three_face_registration_confirmed", actor_operation_slot=fit["actor"],
                Q_candidates=mappings, pose=pose, topH=top_h.tolist(), grid_cells=serial_surface,
                candidates=candidates, geometry_quality=quality, target_candidates=targets,
                layout_digest=_layout_digest(cells),
                stability_signature=np.round(np.asarray([item["centre"] for item in fit["surface"]]), 1).tolist(),
                image_digest=hashlib.sha256(cv2.resize(rgb[82:619, 298:955], (64, 64)).tobytes()).hexdigest())
    base["evidence"].update(geometry_seeds=len(seeds), geometry_solutions=len(solutions),
                             Q_count=len(mappings), independent_faces=["U"] if reference is not None else ["U", "R", "F"],
                             readings=[dict(row, point=row["point"].tolist()) for row in fit["readings"]])
    if reference is not None:
        base["reason"] = "three_face_reference_and_current_top_registration_confirmed"
        base["evidence"].update(proof_source=("three_face_ordinary_reset_view_with_verified_mode_transition"
                                            if reference["mode"] == "board"
                                            else "three_face_rotate_view_with_verified_mode_transition"),
                                reference=dict(image_digest=reference["image_digest"],
                                               mode=reference["mode"],
                                               scan_epoch=reference["scan_epoch"], map_revision=reference["map_revision"],
                                               view_epoch=reference["view_epoch"], actor_slot=reference["actor_slot"],
                                               actor_operation_slot=reference["actor_operation_slot"],
                                               geometry_quality=reference["geometry_quality"],
                                               Q_symmetry_ids=[row["symmetry_id"] for row in reference["Q_candidates"]]),
                                current_view_epoch=view_epoch, camera_transition=fit["camera_transition"],
                                current_top_anchors=len(fit["readings"]),
                                requires_verified_hud_mode_transition=True,
                                hud_transition_verification_owner="directed_operation_runtime")
    if len(mappings) == 1:
        base.update(Q=mappings[0]["Q"], op_to_logical=mappings[0]["op_to_logical"])
    return base


def _safe_move_point(frame, operation_slot, candidate):
    item = next(row for row in frame["grid_cells"] if row["operation_slot"] == operation_slot)
    quad, centre = np.asarray(item["quad"], float), np.asarray(item["centre"], float)
    points = [centre] + list(centre + (quad - centre) * .64)
    allowed = _hud_mask((720, 1280, 3))
    safe = []
    for point in points:
        x, y = np.rint(point).astype(int)
        if not (0 <= x < 1280 and 0 <= y < 720 and allowed[y, x]):
            continue
        if any(tx - 3 <= x <= tx + tw + 3 and ty - 3 <= y <= ty + th + 3
               for target in frame.get("target_candidates", ()) for tx, ty, tw, th in [target["box"]]):
            continue
        if "polygon" in candidate and cv2.pointPolygonTest(np.float32(candidate["polygon"]), (float(x), float(y)), False) < 0:
            continue
        safe.append((cv2.pointPolygonTest(np.float32(quad), (float(x), float(y)), True), [int(x), int(y)]))
    return max(safe, key=lambda row: row[0])[1] if safe else None


def bind_move(frame, destination_slot) -> dict:
    """Resolve the same logical destination under every still-valid Q."""
    try:
        destination = validate_slot(destination_slot)
    except ValueError:
        return dict(status="blocked", reason="invalid_move_destination")
    if frame.get("status") != "ready" or frame.get("mode") != "choose_move":
        return dict(status="blocked", reason="move_operation_frame_not_ready")
    actor = frame["actor_slot"]
    geo = geometry()
    if destination not in geo.moves[actor, :geo.counts[actor]]:
        return dict(status="blocked", reason="move_destination_not_current_face_neighbour")
    bindings = []
    for mapping in frame.get("Q_candidates", ()):
        inverse = np.argsort(mapping["op_to_logical"])
        operation_slot = int(inverse[destination])
        matches = [row for row in frame["candidates"] if row.get("operation_slot") == operation_slot]
        if len(matches) != 1:
            return dict(status="blocked", reason="destination_blue_candidate_missing", logical_slot=destination)
        bindings.append((operation_slot, matches[0]))
    if not bindings or len({slot for slot, _ in bindings}) != 1:
        return dict(status="blocked", reason="operation_orientation_ambiguous", logical_slot=destination)
    operation_slot, candidate = bindings[0]
    point = _safe_move_point(frame, operation_slot, candidate)
    if point is None:
        return dict(status="blocked", reason="destination_has_no_safe_click_point", logical_slot=destination)
    return dict(status="ready", point=point, selected_point=point, logical_slot=destination,
                operation_slot=operation_slot, candidate=candidate,
                target_quad=next(row["quad"] for row in frame["grid_cells"] if row["operation_slot"] == operation_slot))


def _operation_rotation(mapping, rotation_id):
    geo = geometry()
    permutation = np.asarray(mapping["op_to_logical"], int)
    inverse = np.argsort(permutation)
    logical = geo.rotations[rotation_id, :54]
    operation = inverse[logical[permutation]]
    matches = [index for index, row in enumerate(geo.rotations[:, :54]) if np.array_equal(operation, row)]
    return matches[0] if len(matches) == 1 else None


def bind_rotation(frame, rotation_id) -> dict:
    """Bind by the full 54-slot conjugation, including axis/sign/layer flips."""
    geo = geometry()
    if not isinstance(rotation_id, (int, np.integer)) or isinstance(rotation_id, bool) or not 0 <= rotation_id < 18:
        return dict(status="blocked", reason="invalid_rotation_id")
    rotation_id = int(rotation_id)
    if frame.get("status") != "ready" or frame.get("mode") != "choose_rotate":
        return dict(status="blocked", reason="rotation_operation_frame_not_ready")
    if rotation_id not in geo.actor_rotations[frame["actor_slot"]]:
        return dict(status="blocked", reason="rotation_not_actor_legal")
    ids = [_operation_rotation(row, rotation_id) for row in frame.get("Q_candidates", ())]
    if not ids or None in ids or len(set(ids)) != 1:
        return dict(status="blocked", reason="operation_orientation_ambiguous")
    matches = [row for row in frame["candidates"] if row.get("operation_rotation_id") == ids[0]]
    if len(matches) != 1:
        return dict(status="blocked", reason="requested_rotation_arrow_missing", operation_rotation_id=ids[0])
    candidate = matches[0]
    point = list(map(lambda value: int(round(value)), candidate["point"]))
    return dict(status="ready", point=point, arrow_point=point, selected_point=point,
                rotation_id=rotation_id, operation_rotation_id=ids[0],
                arrow_label=candidate["arrow_label"], candidate=candidate)


def _preview_score(cells, readings, mapping, rotation_id):
    geo = geometry()
    permutation = np.asarray(mapping["op_to_logical"], int)
    inverse = np.arange(54) if rotation_id is None else np.argsort(geo.rotations[rotation_id, :54])
    matched, conflicts, changed, static, changed_faces, static_faces = 0, 0, 0, 0, set(), set()
    matched_weight, total_weight = 0., 0.
    details = []
    for row in readings:
        operation_slot = row["operation_slot"]
        logical_slot = int(permutation[operation_slot])
        source = int(inverse[logical_slot])
        expected, before = _known_icon(cells[source]), _known_icon(cells[logical_slot])
        if expected is None:
            continue
        is_match = expected == row["icon_id"]
        matched += int(is_match)
        conflicts += int(not is_match)
        total_weight += row["confidence"]
        matched_weight += row["confidence"] * is_match
        if before is not None and before != expected:
            changed += int(is_match)
            if is_match:
                changed_faces.add(operation_slot // 9)
        elif before is not None and before == expected and source == logical_slot:
            static += int(is_match)
            if is_match:
                static_faces.add(operation_slot // 9)
        details.append(dict(operation_slot=operation_slot, logical_slot=logical_slot, source_slot=source,
                            expected=expected, observed=row["icon_id"], matched=is_match,
                            changed_content=before is not None and before != expected))
    consensus = matched_weight / max(total_weight, 1e-9)
    independent_static = bool(static_faces - changed_faces) or len(static_faces) >= 2
    return dict(rotation_id=rotation_id, matched=matched, conflicts=conflicts,
                changed_anchors=changed, static_anchors=static, consensus=consensus,
                static_other_face=independent_static, score=consensus + min(matched, 18) * .01,
                sufficient=(matched >= _MIN_ANCHORS and conflicts <= 1 and consensus >= _CONSENSUS
                            and changed >= 3 and static >= 3 and independent_static), details=details)


def verify_rotation_preview(image_rgb, layout, rotation_id, frame) -> dict:
    """Compare all four legal endpoint permutations at the locked before pose.

    Unknown is intentional for still-moving, occluded, or repetitive layouts.
    A temporal-stability gate remains the caller's responsibility.
    """
    unknown = dict(status="unknown", reason="rotation_preview_unconfirmed", evidence={})
    if frame.get("status") != "ready" or frame.get("mode") != "choose_rotate" or not frame.get("pose"):
        return dict(unknown, reason="before_rotation_frame_not_ready")
    rgb = np.asarray(image_rgb)
    if rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
        return dict(unknown, reason="invalid_preview_image")
    try:
        cells, actor = _layout(layout)
    except (ValueError, KeyError, TypeError) as error:
        return dict(unknown, reason=str(error))
    if (not isinstance(rotation_id, (int, np.integer)) or isinstance(rotation_id, bool)
            or not 0 <= rotation_id < 18 or actor != frame.get("actor_slot")
            or rotation_id not in geometry().actor_rotations[actor]
            or _layout_digest(cells) != frame.get("layout_digest")):
        return dict(unknown, reason="preview_before_state_or_rotation_invalid")
    pose = frame["pose"]
    intrinsic = np.asarray(pose["K"], float)
    rvec, tvec = np.asarray(pose["rvec"], float).reshape(3, 1), np.asarray(pose["tvec"], float).reshape(3, 1)
    features = _features(rgb, detect_targets(rgb), preview=True)
    surface = _surface(intrinsic, rvec, tvec)
    readings = _readings(rgb, surface, features)
    if len(readings) < _MIN_ANCHORS:
        return dict(unknown, reason="preview_surface_evidence_insufficient", evidence=dict(anchors=len(readings)))
    outcomes, diagnostic = [], []
    for mapping in frame.get("Q_candidates", ()):
        scores = [_preview_score(cells, readings, mapping, int(candidate))
                  for candidate in geometry().actor_rotations[actor]]
        scores.sort(key=lambda row: -row["score"])
        diagnostic.append(dict(symmetry_id=mapping["symmetry_id"], scores=scores))
        supported = [row for row in scores if row["sufficient"]]
        if len(supported) != 1:
            return dict(unknown, reason="preview_permutation_ambiguous_or_occluded", evidence=dict(registrations=diagnostic))
        winner = supported[0]
        runner_score = max((row["score"] for row in scores if row is not winner), default=0.)
        if winner["score"] - runner_score < .07:
            return dict(unknown, reason="preview_permutation_margin_insufficient", evidence=dict(registrations=diagnostic))
        outcomes.append(winner["rotation_id"])
    if not outcomes or len(set(outcomes)) != 1:
        return dict(unknown, reason="preview_orientation_ambiguous", evidence=dict(registrations=diagnostic))
    observed = outcomes[0]
    return dict(status="matched" if observed == int(rotation_id) else "mismatch",
                reason="locked_body_pose_and_changed_nodes_match" if observed == int(rotation_id) else "preview_matches_other_legal_rotation",
                requested_rotation_id=int(rotation_id), observed_rotation_id=observed,
                evidence=dict(registrations=diagnostic, locked_pose=True, anchors=len(readings)))


__all__ = ["build_wide_reference_frame", "build_operation_frame", "bind_move", "bind_rotation", "verify_rotation_preview"]
