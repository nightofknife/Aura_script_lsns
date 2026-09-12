"""Locate one cached love-bento identity in an already captured RGB frame."""
from __future__ import annotations

import math

import cv2
import numpy as np


def _target_row(catalog, section, key, value):
    rows = [row for row in catalog[section] if row.get(key) == value]
    if len(rows) != 1 or not rows[0].get('resolved'):
        raise ValueError(f'love-bento requested {section} metadata missing or ambiguous')
    return rows[0]


def _match(vision_method, **kwargs):
    try:
        result = vision_method(**kwargs)
    except Exception as exc:
        raise ValueError('love-bento target matching failed') from exc
    if result is None or (getattr(result, 'debug_info', None) or {}).get('error'):
        raise ValueError('love-bento target matching failed')
    return result


def _confident(hit, threshold):
    try:
        score = float(hit.confidence)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError('love-bento target matching failed: invalid score') from exc
    if not math.isfinite(score):
        raise ValueError('love-bento target matching failed: non-finite score')
    return bool(hit.found) and score >= threshold


def _visible_boxes(frame, point, geometry, cfg):
    rx, ry, _, _ = cfg['capture_roi']
    vx, vy, vw, vh = cfg['viewport']
    margin = cfg['roi_margin']
    x, y = point
    boxes = []
    for key in ('food_crop_xyxy', 'role_crop_xyxy', 'days_crop_xyxy'):
        x1, y1, x2, y2 = geometry[key]
        left, top = x+x1-margin, y+y1-margin
        right, bottom = x+x2+margin, y+y2+margin
        if (left < 0 or top < 0 or right > frame.shape[1] or bottom > frame.shape[0]
                or left+rx < vx or top+ry < vy
                or right+rx > vx+vw or bottom+ry > vy+vh):
            return None
        boxes.append((left, top, right, bottom))
    return boxes


def find_target_on_frame(frame, meal, catalog, vision) -> list[int] | None:
    """Return a capture-local card anchor, never an inventory or click position.

    ``meal`` is a cached identity; ``catalog`` is already resolved by
    ``load_love_bento_catalog``. Incomplete cards are ignored. No visible match
    returns None; duplicate targets and unusable matching evidence raise
    ValueError. Only the requested food, role and day templates are evaluated.
    """
    if (not isinstance(frame, np.ndarray) or frame.ndim != 3
            or frame.shape[2] != 3 or not frame.size):
        raise ValueError('love-bento target capture failed: expected a nonempty RGB frame')
    try:
        food = _target_row(catalog, 'items', 'id', meal['food_id'])
        role = _target_row(catalog, 'roles', 'id', meal['role_id'])
        day = _target_row(catalog, 'days', 'value', meal['remaining_days'])
        if not day.get('resolved_mask') or food['id'] not in role['food_ids']:
            raise ValueError('love-bento requested metadata missing or incompatible')
        cfg, geometry = catalog['scanner'], catalog['geometry']
        if not cfg['anchor_template'] or not cfg['anchor_mask']:
            raise ValueError('love-bento anchor metadata missing')
        thresholds = [cfg[key+'_threshold'] for key in ('food', 'role', 'days')]
        if any(not math.isfinite(t) or not 0 <= t <= 1
               for t in [cfg['anchor_threshold'], *thresholds]):
            raise ValueError('love-bento matching threshold invalid')
    except (KeyError, TypeError) as exc:
        raise ValueError('love-bento requested metadata missing or invalid') from exc

    multi = _match(
        vision.find_all_templates, source_image=frame,
        template_image=cfg['anchor_template'], mask_image=cfg['anchor_mask'],
        threshold=cfg['anchor_threshold'], use_grayscale=True,
        match_method=cv2.TM_SQDIFF_NORMED, nms_threshold=0.3, preprocess='none')
    if not hasattr(multi, 'matches'):
        raise ValueError('love-bento card detection failed')
    target = None
    for anchor in multi.matches or []:
        if not _confident(anchor, cfg['anchor_threshold']):
            continue
        point = [int(v) for v in anchor.top_left]
        boxes = _visible_boxes(frame, point, geometry, cfg)
        if boxes is None:
            continue
        for index, (row, box, threshold) in enumerate(zip((food, role, day), boxes, thresholds)):
            left, top, right, bottom = box
            hit = _match(
                vision.find_template, source_image=frame[top:bottom, left:right],
                template_image=row['resolved'],
                mask_image=day['resolved_mask'] if index == 2 else None,
                threshold=threshold, use_grayscale=index != 0,
                match_method=cv2.TM_CCOEFF_NORMED, preprocess='none')
            if not _confident(hit, threshold):
                break
        else:
            # Keep checking visible anchors: an identical tuple is not unique.
            if target is not None:
                raise ValueError('love-bento target ambiguous: multiple visible matches')
            target = point
    return target
