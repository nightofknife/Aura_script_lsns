"""Pure quad-only research quality; never target/classification/action proof.

Quads are row-major, already bound to the requested face by the caller.
No clock, capture, tracker, classifier or waiting runs here. Source freshness,
current pose support and settling are caller attestations, bound to source.
The exact official HUD mask is distinct from its extra navigation erosion.
"""
from copy import deepcopy
import math

import cv2
import numpy as np


def official_ui_mask():
    """Exact current _layout_vision._ui_mask; parity regression detects drift."""
    mask = np.zeros((720, 1280), np.uint8)
    mask[82:619, 295:951] = 255
    mask[494:579, 605:677] = 0
    return mask


def _source_key(source):
    if not isinstance(source, dict):
        return None
    names = ('session_id', 'generation', 'frame_id', 'frame_time', 'map_revision')
    if any(name not in source for name in names):
        return None
    if (not isinstance(source['session_id'], (str, int)) or
            isinstance(source['session_id'], bool) or source['session_id'] == '' or
            any(type(source[k]) is not int or source[k] < 0
                for k in ('generation', 'frame_id', 'map_revision')) or
            type(source['frame_time']) not in (int, float) or
            not math.isfinite(source['frame_time']) or source['frame_time'] < 0):
        return None
    return tuple(source[k] for k in names)


def _jacobian_condition(quad):
    transform = cv2.getPerspectiveTransform(
        np.float32(((0, 0), (1, 0), (1, 1), (0, 1))), quad.astype(np.float32))
    conditions = []
    for x, y in ((0, 0), (1, 0), (1, 1), (0, 1), (.5, .5)):
        denominator = transform[2] @ [x, y, 1]
        numerator = transform[:2] @ [x, y, 1]
        if not np.isfinite(denominator) or abs(denominator) < 1e-9:
            return math.inf
        jacobian = (transform[:2, :2]*denominator -
                    numerator[:, None]*transform[2, :2])/denominator**2
        singular = np.linalg.svd(jacobian, compute_uv=False)
        conditions.append(float(singular[0]/singular[-1]) if singular[-1] > 0 else math.inf)
    return max(conditions)


def evaluate_face_view_quality(quads, *, face, source=None, source_valid=False,
                               settle_evidence=None, current_support=None,
                               ui_mask=None, min_area=1600., min_edge=28.,
                               max_edge_ratio=2.5, max_jacobian_condition=3.,
                               safety_margin_px=6):
    """Inspect nine projected quads; assertions never create source evidence.

    For geometry_shot_ready, source_valid must be literally True and both
    caller evidence dictionaries must have matching ``source`` identity.
    Support also needs {valid:True, face:<face>}; settle needs {settled:True}.
    geometry_quads_ok can be inspected for old saved images independently.
    ui_mask, if provided, must equal the official mask pixel-for-pixel; a caller
    cannot silently replace glyph exclusions with a more lenient drag mask.
    """
    output = dict(face=face, source=deepcopy(source), geometry_quads_ok=False,
        exact_mask_quads_ok=False,
        geometry_shot_ready=False, targets_ready=False, action_ready=False,
        complete_recognition=False, formal_acceptance=False, cells=[], reasons=[],
        freshness_evaluated=False, current_support_evaluated=False, settle_evaluated=False,
        source_freshness='caller_attested' if source_valid is True else 'not_attested',
        mask_policy=dict(exact='official_glyph_ui_mask',
            additional='navigation_only_eroded_mask', safety_margin_px=safety_margin_px))

    def reject(reason):
        output['reasons'].append(reason)
        return output

    parameters = (min_area, min_edge, max_edge_ratio, max_jacobian_condition)
    if (face not in ('U', 'R', 'F', 'D', 'L', 'B') or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                not math.isfinite(v) or v <= 0 for v in parameters) or
            max_edge_ratio < 1 or max_jacobian_condition < 1 or
            type(safety_margin_px) is not int or not 0 <= safety_margin_px <= 64):
        return reject('invalid_configuration')
    try:
        q = np.asarray(quads, float)
    except (TypeError, ValueError):
        return reject('invalid_quads')
    if q.shape != (9, 4, 2) or not np.isfinite(q).all():
        return reject('nine_finite_quads_required')
    if np.max(np.abs(q)) >= 1e6:
        return reject('quad_coordinates_out_of_range')
    official = official_ui_mask()
    if ui_mask is not None and not np.array_equal(ui_mask, official):
        return reject('official_ui_mask_mismatch')
    valid_pixels = official > 0
    diameter = 2*safety_margin_px+1
    navigation_pixels = cv2.erode(official, np.ones((diameter, diameter), np.uint8),
                                 borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    orientations = []
    for index, quad in enumerate(q):
        reasons = []
        edges = np.roll(quad, -1, axis=0)-quad
        lengths = np.linalg.norm(edges, axis=1)
        cross = edges[:, 0]*np.roll(edges[:, 1], -1)-edges[:, 1]*np.roll(edges[:, 0], -1)
        convex = bool((cross > 1e-6).all() or (cross < -1e-6).all())
        signed_area = .5*float(np.sum(quad[:, 0]*np.roll(quad[:, 1], -1)-
                                      quad[:, 1]*np.roll(quad[:, 0], -1)))
        area = abs(signed_area)
        shortest = float(min(lengths))
        ratio = float(max(lengths)/shortest) if shortest > 0 else math.inf
        try:
            condition = _jacobian_condition(quad) if convex else math.inf
        except (cv2.error, np.linalg.LinAlgError, FloatingPointError):
            condition = math.inf
        within_roi = bool(((quad[:, 0] >= 300) & (quad[:, 0] <= 950) &
                           (quad[:, 1] >= 84) & (quad[:, 1] <= 615)).all())
        raster = np.zeros((720, 1280), np.uint8)
        if convex and np.max(np.abs(quad)) < 1e6:
            # Match saved glyph-probe raster semantics: int32 truncation.
            cv2.fillConvexPoly(raster, np.int32(quad), 1)
        pixel_count = int(np.count_nonzero(raster))
        exact_overlap = int(np.count_nonzero((raster > 0) & ~valid_pixels))
        navigation_overlap = int(np.count_nonzero((raster > 0) & ~navigation_pixels))
        for failed, reason in ((not convex, 'nonconvex_quad'), (not within_roi, 'outside_roi'),
                (area < min_area, 'area_too_small'), (shortest < min_edge, 'edge_too_short'),
                (ratio > max_edge_ratio, 'edge_ratio'),
                (not math.isfinite(condition) or condition > max_jacobian_condition, 'jacobian_condition'),
                (pixel_count == 0, 'empty_raster'), (exact_overlap > 0, 'official_ui_overlap'),
                (navigation_overlap > 0, 'navigation_margin_overlap')):
            if failed:
                reasons.append(reason)
        orientations.append(np.sign(signed_area))
        output['cells'].append(dict(index=index, row=index//3, col=index%3,
            area_px=area, min_edge_px=shortest, edge_ratio=ratio,
            max_jacobian_condition=condition if math.isfinite(condition) else None,
            within_roi=within_roi, convex=convex, raster_pixels=pixel_count,
            exact_ui_overlap_pixels=exact_overlap,
            exact_ui_overlap_fraction=exact_overlap/max(1, pixel_count),
            navigation_overlap_pixels=navigation_overlap,
            exact_ui_clear=exact_overlap == 0 and pixel_count > 0,
            navigation_margin_clear=navigation_overlap == 0 and pixel_count > 0,
            reasons=reasons, quality_ok=not reasons))
    if len(set(orientations)) != 1:
        output['reasons'].append('inconsistent_quad_orientation')
    for i in range(9):
        if not output['cells'][i]['convex']:
            continue
        for j in range(i):
            if not output['cells'][j]['convex']:
                continue
            intersection, _ = cv2.intersectConvexConvex(q[i].astype(np.float32), q[j].astype(np.float32))
            if intersection > 1.:
                output['reasons'].append('overlapping_quads')
                break
    output['blocked_cells'] = [c['index'] for c in output['cells'] if not c['quality_ok']]
    output['geometry_quads_ok'] = not output['blocked_cells'] and not output['reasons']
    output['exact_mask_quads_ok'] = (not output['reasons'] and all(
        not [r for r in c['reasons'] if r != 'navigation_margin_overlap'] for c in output['cells']))
    key = _source_key(source)
    source_ok = key is not None and source_valid is True
    support_ok = (isinstance(current_support, dict) and current_support.get('valid') is True and
                  current_support.get('face') == face and key is not None and
                  _source_key(current_support.get('source')) == key)
    settle_ok = (isinstance(settle_evidence, dict) and settle_evidence.get('settled') is True and
                 key is not None and _source_key(settle_evidence.get('source')) == key)
    output['caller_contract'] = dict(source_bound=source_ok, support_bound=support_ok,
                                    settle_bound=settle_ok)
    if not source_ok:
        output['reasons'].append('caller_current_source_required')
    if not support_ok:
        output['reasons'].append('caller_current_face_support_required')
    if not settle_ok:
        output['reasons'].append('caller_settle_evidence_required')
    output['geometry_shot_ready'] = output['geometry_quads_ok'] and source_ok and support_ok and settle_ok
    return output
