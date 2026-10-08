"""Input-free glyph reading and temporal consensus for actual four-view frames.

No annotation/expected class enters this API. Three stable captures are temporal
evidence from one view, never three independent viewpoints.
"""
from __future__ import annotations

import hashlib
import math

import cv2
import numpy as np

from ._deep_dive_layout_semantics import classify_icon

DEFAULT_SCALES = (1.0, .85, .70, .60)
NODE_KINDS = dict(white_diamond='empty', yellow_hex='item', blue_scales='shop',
                  green_burst='healing', purple_ring='vortex', red_single_eye='battle')


def read_node(crop_rgb: np.ndarray, *, scales=DEFAULT_SCALES) -> dict:
    rgb = np.asarray(crop_rgb)
    if rgb.shape != (96, 96, 3) or rgb.dtype != np.uint8:
        raise ValueError('expected_actual_96x96_uint8_rgb')
    if (not scales or len(scales) > 8 or any(isinstance(s, bool) or
            not isinstance(s, (int, float)) or not math.isfinite(s) or
            not 0 < s <= 1 for s in scales)):
        raise ValueError('invalid_normalization_scales')
    records = []
    for scale in scales:
        matrix = np.float32(((scale, 0, 95 * (1-scale)/2),
                            (0, scale, 95 * (1-scale)/2)))
        normalized = np.ascontiguousarray(rgb.copy()) if scale == 1 else cv2.warpAffine(
            rgb, matrix, (96, 96), flags=cv2.INTER_AREA,
            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        records.append(dict(scale=float(scale), input_sha256=hashlib.sha256(
            normalized.tobytes()).hexdigest(), reading=dict(classify_icon(normalized))))
    accepted = [r['reading'] for r in records if r['reading'].get('icon_id') is not None]
    classes = sorted({r['icon_id'] for r in accepted})
    icon = classes[0] if len(classes) == 1 else None
    return dict(icon_id=icon, node_kind=NODE_KINDS.get(icon, 'unknown'),
                node_status='known' if icon is not None else 'unknown',
                confidence=min(float(r['confidence']) for r in accepted) if icon else 0.,
                conflict=len(classes) > 1, normalization_readings=records,
                unknown_reason='conflicting_normalizations' if len(classes) > 1 else
                'no_confirmed_class' if not classes else None,
                crop_sha256=hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest())


def source_key(source: dict) -> tuple:
    """Reject synthetic timestamps and stale/non-atomic capture identifiers."""
    if (not isinstance(source, dict) or source.get('generation_source') != 'atomic_wgc'
            or source.get('capture_backend') != 'wgc'):
        raise ValueError('actual_atomic_wgc_source_required')
    for key in ('session_id', 'generation', 'frame_id', 'map_revision'):
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('invalid_capture_' + key)
    timestamp = source.get('frame_time')
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError('invalid_capture_frame_time')
    return source['session_id'], source['map_revision'], source['generation'], timestamp


def crop_node(rgb: np.ndarray, quad) -> np.ndarray:
    image = np.asarray(rgb)
    points = np.asarray(quad, np.float32)
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError('expected_actual_uint8_rgb')
    if points.shape != (4, 2) or not np.isfinite(points).all() or not cv2.isContourConvex(points):
        raise ValueError('invalid_cell_quad')
    h, w = image.shape[:2]
    if np.any(points < 0) or np.any(points[:, 0] >= w) or np.any(points[:, 1] >= h):
        raise ValueError('cell_quad_outside_source')
    matrix = cv2.getPerspectiveTransform(points, np.float32(((0, 0), (95, 0), (95, 95), (0, 95))))
    return cv2.warpPerspective(image, matrix, (96, 96))


def read_view_nodes(rgb, cells, *, source, view_group, scan_epoch) -> list[dict]:
    source_key(source)
    if type(view_group) is not int or view_group < 0 or not isinstance(scan_epoch, str) or not scan_epoch:
        raise ValueError('invalid_view_or_scan_identity')
    result = []
    rgb_sha256 = hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest()
    for cell in cells:
        # Only explicit coordinates and actual geometry are consumed.
        result.append(dict(face=cell['face'], row=int(cell['row']), col=int(cell['col']),
            quad=np.asarray(cell['quad'], float).tolist(), reading=read_node(crop_node(rgb, cell['quad'])),
            source=dict(source), source_rgb_sha256=rgb_sha256, view_group=view_group, scan_epoch=scan_epoch))
    return result


def fuse_node_readings(samples: list[dict]) -> dict:
    """Exactly three new stable frames, at least two agreeing, zero conflicts."""
    unknown = dict(icon_id=None, node_kind='unknown', node_status='unknown', confidence=0.,
                   temporal_frame_count=0, independent_view_count=0, evidence=[])
    if len(samples) != 3:
        return dict(unknown, unknown_reason='three_stable_frames_required')
    try:
        keys = [source_key(s['source']) for s in samples]
        groups = {s['view_group'] for s in samples}
        epochs = {s['scan_epoch'] for s in samples}
    except (KeyError, TypeError, ValueError):
        return dict(unknown, unknown_reason='invalid_actual_source')
    if any(s.get('stable') is not True for s in samples):
        return dict(unknown, unknown_reason='stable_capture_required')
    if any(type(s['view_group']) is not int or s['view_group'] < 0 or
           not isinstance(s['scan_epoch'], str) or not s['scan_epoch'] for s in samples):
        return dict(unknown, unknown_reason='invalid_view_or_scan_identity')
    if any('face' in s for s in samples) and len({(s.get('face'), s.get('row'), s.get('col')) for s in samples}) != 1:
        return dict(unknown, unknown_reason='mixed_cell_identity')
    if (len(groups) != 1 or len(epochs) != 1 or len({k[:2] for k in keys}) != 1
            or any(keys[i][2] >= keys[i+1][2] or keys[i][3] >= keys[i+1][3] for i in range(2))):
        return dict(unknown, unknown_reason='stale_or_mixed_capture_sequence')
    readings = [s['reading'] for s in samples]
    accepted = [r for r in readings if r.get('icon_id') is not None]
    classes = {r['icon_id'] for r in accepted}
    base = dict(unknown, temporal_frame_count=3, independent_view_count=1,
                evidence=[dict(source=dict(s['source']), group=s['view_group'],
                    scan_epoch=s['scan_epoch'], icon_id=s['reading'].get('icon_id')) for s in samples])
    if len(classes) > 1 or any(r.get('conflict') for r in readings):
        return dict(base, conflict=True, unknown_reason='confirmed_class_conflict')
    if len(accepted) < 2:
        return dict(base, conflict=False, unknown_reason='two_confirmed_frames_required')
    icon = next(iter(classes))
    return dict(base, icon_id=icon, node_kind=NODE_KINDS.get(icon, 'unknown'), node_status='known',
                confidence=min(float(r['confidence']) for r in accepted), conflict=False, unknown_reason=None)
