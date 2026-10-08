"""Research-only known-glyph pixel measurements; never authorizes an action.

Keeping a known hypothesis does NOT justify deleting mixed-colour rejection or
the red/orange competitor margin. These functions preserve those gates and do
not call the seven-class public classifier. Nothing imports this module in the
production scanner. A returned point still requires its original pose/support,
same-source, residual and all54 entity guards in a separately approved caller.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib

import cv2
import numpy as np

from . import _deep_dive_layout_semantics as semantics

ICONS = ('white_diamond', 'blue_scales', 'green_burst', 'purple_ring',
         'yellow_hex', 'red_single_eye', 'orange_triple_eye')


def _rgb(rgb):
    array = np.asarray(rgb)
    if array.shape != (96, 96, 3) or array.dtype != np.uint8:
        raise ValueError('known_glyph_requires_uint8_96_rgb')
    return array


def _masks(rgb):
    h, s, v = cv2.split(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV))
    return {
        'white_diamond': (s < 62) & (v > 170),
        'blue_scales': (h >= 100) & (h <= 125) & (s > 100) & (v > 135),
        'green_burst': (h >= 40) & (h <= 91) & (s > 130) & (v > 135),
        'purple_ring': (h >= 126) & (h <= 148) & (s > 120) & (v > 130),
        'yellow_hex': (h >= 21) & (h <= 39) & (s > 110) & (v > 160),
        'warm_eye': ((h <= 23) | (h >= 162)) & (s > 100) & (v > 130),
    }


def _known_colour_gate(rgb, expected):
    masks = _masks(rgb)
    ranked = sorted(((int(mask[19:77, 19:77].sum()), name)
                     for name, mask in masks.items()), reverse=True)
    if ({ranked[0][1], ranked[1][1]} == {'yellow_hex', 'warm_eye'}
            and ranked[0][0] < ranked[1][0] * 1.7 and semantics._hex_outline(masks['yellow_hex'])):
        masks['warm_eye'] &= ~masks['yellow_hex']
        ranked = sorted(((int(mask[19:77, 19:77].sum()), name)
                         for name, mask in masks.items()), reverse=True)
    count, name = ranked[0]
    wanted = 'warm_eye' if expected in ('red_single_eye', 'orange_triple_eye') else expected
    # Same global rejection, with an early known-hypothesis mismatch rejection.
    if (name != wanted or count < 65 or count < ranked[1][0] * 1.7
            or (count > 1650 and not (name == 'purple_ring' and semantics._purple_outline(masks[name])))):
        return None
    center = np.zeros((96, 96), bool); center[19:77, 19:77] = True
    yy, xx = np.nonzero(masks[name] & center)
    width, height = int(xx.max()-xx.min()+1), int(yy.max()-yy.min()+1)
    if (min(width, height) < 15 or max(width, height)/min(width, height) > 3.
            or abs(float(xx.mean())-47.5) > 13 or abs(float(yy.mean())-47.5) > 13
            or count/(width*height) > .70 or masks[name][32:64, 32:64].sum() < 18):
        return None
    return masks[name], count, ranked


def _shape(mask):
    center = np.zeros((96, 96), bool); center[10:86, 10:86] = True
    yy, xx = np.nonzero(mask & center)
    if len(xx) < 50:
        return None
    x1, x2, y1, y2 = xx.min(), xx.max(), yy.min(), yy.max()
    return cv2.resize(mask[y1:y2+1, x1:x2+1].astype(np.float32), (64, 64), interpolation=cv2.INTER_AREA)


def match_known_icon(rgb, expected, *, pixel_template=None):
    """Subset-equivalent classifier plus optional individual-pixel verification.

    Without pixel_template, output exactly the public classifier's positive if
    it is expected; otherwise None. A template may only veto, never create a
    positive rejected by the unchanged shape/colour gates. No new node label.
    """
    rgb = _rgb(rgb)
    if expected not in ICONS:
        raise ValueError('unknown_glyph_hypothesis')
    empty = dict(icon_id=None, confidence=0., status='proposal_only', input_authorized=False)
    gate = _known_colour_gate(rgb, expected)
    if gate is None:
        return dict(empty, reason='known_colour_or_shape_rejected')
    mask, count, ranked = gate
    if expected in ('red_single_eye', 'orange_triple_eye'):
        confidence = None
        for whole in (False, True):
            glyph = semantics._warm_glyph(rgb, whole=whole)
            if glyph is None:
                continue
            # The competitor is necessary negative evidence, not a new label.
            scores = sorted(semantics._eye_correlation_scores(glyph, whole=whole), reverse=True)
            if scores[0][0] >= .78 and scores[0][0]-scores[1][0] >= .055:
                if scores[0][1] != expected:
                    return dict(empty, reason='known_warm_competitor')
                confidence = round(min(.87, .58+.29*scores[0][0]), 3)
                break
        if confidence is None:
            return dict(empty, reason='known_warm_shape_rejected')
    else:
        strength = min(1., count/450)
        dominance = count/max(sum(item[0] for item in ranked), 1)
        confidence = round(min(.83, .51+.18*strength+.14*dominance), 3)
    result = dict(icon_id=expected, confidence=confidence, status='proposal_only', input_authorized=False)
    if pixel_template is not None:
        if pixel_template.get('icon_id') != expected:
            return dict(empty, reason='known_pixel_template_identity')
        actual = _shape(mask)
        template = np.asarray(pixel_template.get('shape'), np.float32)
        if actual is None or template.shape != (64, 64) or not np.isfinite(template).all():
            return dict(empty, reason='known_pixel_template_invalid')
        # Preserve the native matcher's four right-angle glyph orientations.
        # These rotate the actual source pixels, not synthetic new identities.
        score = max(float(cv2.minMaxLoc(cv2.matchTemplate(np.pad(actual, 4),
                    np.ascontiguousarray(np.rot90(template, k)), cv2.TM_CCORR_NORMED))[1]) for k in range(4))
        # This extra research threshold is a veto only, never a relaxed gate.
        if score < .90:
            return dict(empty, reason='known_pixel_template_mismatch', template_score=score)
        result.update(template_score=score, template_source=deepcopy(pixel_template['source']))
    return result


def make_known_template(rgb, expected, *, source):
    """Retain actual pixels with externally supplied capture/slot provenance."""
    if not isinstance(source, dict) or not all(key in source for key in
            ('session_id', 'frame_id', 'frame_time', 'map_revision', 'cell_index')):
        raise ValueError('known_pixel_template_source_missing')
    accepted = match_known_icon(rgb, expected)
    if accepted['icon_id'] != expected or accepted['confidence'] < .70:
        raise ValueError('known_pixel_template_not_confirmed')
    shape = _shape(_known_colour_gate(rgb, expected)[0])
    if shape is None:
        raise ValueError('known_pixel_template_empty')
    shape.flags.writeable = False
    return dict(icon_id=expected, shape=shape, source=deepcopy(source),
                rgb_digest=hashlib.sha256(_rgb(rgb).tobytes()).hexdigest(),
                input_authorized=False, status='proposal_only')


def locate_known_glyph(rgb, item, expected, *, targets=(), pixel_template=None):
    """Actual same-RGB centre measurement, with conservative occlusion veto.

    Returns proposal evidence or None, never renews scanner state. The caller
    must retain original fit/support gates and source/session/Q verification.
    """
    from . import _deep_dive_layout_vision as vision
    rgb = np.asarray(rgb)
    if rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
        raise ValueError('known_glyph_requires_current_1280_rgb')
    q = np.asarray(item['quad'], np.float32)
    if q.shape != (4, 2) or not np.isfinite(q).all():
        return None
    if item['cosine'] < .30 or item['area'] < 900:
        return None
    # No HUD/partial-grid/weak-target pixels may renew evidence. Fail closed on
    # malformed boxes; box confidence is deliberately irrelevant to masking.
    roi = np.zeros((720, 1280), np.uint8)
    if np.any(q[:, 0] < 295) or np.any(q[:, 0] > 950) or np.any(q[:, 1] < 82) or np.any(q[:, 1] > 618):
        return None
    cv2.fillConvexPoly(roi, np.int32(q), 1)
    if np.any((roi > 0) & (vision._ui_mask() == 0)):
        return None
    for target in targets:
        try:
            x, y, w, h = np.asarray(target['box'], float)
        except (KeyError, TypeError, ValueError):
            return None
        if not np.isfinite([x, y, w, h]).all() or min(w, h) <= 0:
            return None
        left, right = max(0, int(np.floor(x))), min(1280, int(np.ceil(x+w)))
        top, bottom = max(0, int(np.floor(y))), min(720, int(np.ceil(y+h)))
        if left < right and top < bottom and np.any(roi[top:bottom, left:right]):
            return None
    crop = vision._crop(rgb, q)
    matched = match_known_icon(crop, expected, pixel_template=pixel_template)
    if matched['icon_id'] != expected or matched['confidence'] < .70:
        return None
    # Preserve the production centre extraction's exact colour support, even
    # when the classification warm mask spans overlapping hue ranges.
    mask = vision._icon_mask(crop, expected).copy()
    mask[:10] = False; mask[86:] = False; mask[:, :10] = False; mask[:, 86:] = False
    yy, xx = np.nonzero(mask)
    if len(xx) < 50:
        return None
    centre = np.float32([[(np.percentile(xx, 3)+np.percentile(xx, 97))/2,
                         (np.percentile(yy, 3)+np.percentile(yy, 97))/2]])
    inverse = cv2.getPerspectiveTransform(np.float32(((0,0),(95,0),(95,95),(0,95))), q)
    point = cv2.perspectiveTransform(centre.reshape(1,1,2), inverse).reshape(2)
    if np.linalg.norm(point-np.asarray(item['centre'])) > 28:
        return None
    return dict(matched, point=point.tolist(), index=item['index'], quad=q.tolist(),
                rgb_digest=hashlib.sha256(rgb.tobytes()).hexdigest(),
                point_kind='measured_current_glyph_midrange',
                requires_original_pose_support_and_same_frame_guards=True)
