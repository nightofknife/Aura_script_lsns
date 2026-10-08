"""Signed local IMAGE orientation feedback for the ordered vertical route.

Units are image angular shape degrees, not physical cube rotation degrees.
No accumulated mouse displacement or homography-decomposition branch is used.
The caller still owns reset/phase continuity, fresh source and stop settling.
"""
from __future__ import annotations

import math
import numpy as np


_FACES = {1: ('view1left', 'view1right'), 2: ('view2main',),
          3: ('view3left', 'view3right'), 4: ('view4main',)}


def _invalid(reason, metrics=None):
    return dict(valid_diagnostic=False, signed_error_deg=None,
                direction_up_error_sign='unknown', arrival_candidate=False,
                reason=reason, metrics=metrics or {}, formal_pose=False,
                full_cube=False, phase_identity=False)


def _panel_metric(panel):
    pairs = [p for p in panel.get('matched_pairs', []) if p.get('inlier')
             and p.get('residual_px') is not None and 0 <= p['residual_px'] <= 3.]
    if len(pairs) < 12:
        return None, 'fewer_than_12_actual_inliers'
    reference = np.asarray([p['reference_point'] for p in pairs], np.float64)
    query = np.asarray([p['query_point'] for p in pairs], np.float64)
    if (not np.isfinite(reference).all() or not np.isfinite(query).all()
            or min(np.ptp(reference, axis=0)) < 100.
            or min(np.ptp(query, axis=0)) < 100.):
        return None, 'actual_support_span_below_100x100'
    if len(set(p['reference_cell'] for p in pairs)) < 3:
        return None, 'fewer_than_3_reference_cells'
    if panel.get('homography') is None:
        return None, 'no_actual_homography'
    h = np.asarray(panel['homography'], np.float64)
    if h.shape != (3, 3) or not np.isfinite(h).all():
        return None, 'invalid_homography'
    # HUD overlap can retain a motion diagnostic; other geometric failures cannot.
    failures = panel.get('projection_failures', [])
    if any(not item.endswith(':overlaps_reset_hud') for item in failures):
        return None, 'projected_geometry_invalid'
    values = []
    for x, y in reference:
        projected = h @ (x, y, 1.)
        denominator = projected[2]
        if abs(denominator) < 1e-9:
            return None, 'homography_pole'
        jacobian = (h[:2, :2]*denominator-projected[:2, None]*h[2, :2])/denominator**2
        if not np.isfinite(jacobian).all() or np.linalg.det(jacobian) <= 0:
            return None, 'nonpositive_local_geometry'
        horizontal = math.degrees(math.atan2(jacobian[1, 0], jacobian[0, 0]))
        vertical = math.degrees(math.atan2(jacobian[1, 1], jacobian[0, 1]))-90.
        shape = math.degrees(math.atan2(np.linalg.norm(jacobian[:, 1]),
                                       np.linalg.norm(jacobian[:, 0])))-45.
        roll = math.degrees(math.atan2(jacobian[1, 0]-jacobian[0, 1],
                                      jacobian[0, 0]+jacobian[1, 1]))
        values.append((horizontal, vertical, shape, roll))
    array = np.asarray(values)
    medians = np.median(array, axis=0)
    return dict(horizontal_axis_delta_deg=float(medians[0]),
                vertical_axis_delta_deg=float(medians[1]),
                vertical_horizontal_shape_deg=float(medians[2]),
                polar_roll_deg=float(medians[3]),
                actual_inliers=len(pairs), reference_cells=len(set(p['reference_cell'] for p in pairs)),
                angular_p10_p90_deg=np.percentile(array, (10, 90), axis=0).tolist(),
                image_grid_readable=bool(panel.get('proposal_accepted')),
                projection_failures=failures), None


def orientation_feedback(alignment_result, *, expected_view,
                         main_tolerance_deg=.25, paired_tolerance_deg=.35,
                         off_axis_tolerance_deg=.75):
    """Return a local diagnostic and an unsettled arrival CANDIDATE.

    Main faces: atan(vertical/horizontal relative scale)-45 degrees.
    Paired faces: half(left horizontal direction delta - right delta).
    Upward input decreases both measured residuals on the actual 2026-10-06
    calibration neighborhoods. This local sign is not a global camera model.
    """
    if expected_view not in _FACES:
        return _invalid('unknown_expected_view')
    if alignment_result.get('query_feature_count', 0) == 0:
        return _invalid('no_actual_query_features')
    selected = {p['reference_panel']: p for p in alignment_result.get('results', [])
                if p.get('view') == expected_view}
    if any(face not in selected for face in _FACES[expected_view]):
        return _invalid('wrong_view_or_missing_reference_panel')
    metrics = dict(unit='image_angular_shape_degrees_not_physical_scan_angle',
                   expected_view=expected_view, panels={},
                   direction_evidence='actual_vertical_calibration_neighborhoods_20261006',
                   thresholds_provisional=True)
    for face in _FACES[expected_view]:
        value, reason = _panel_metric(selected[face])
        if value is None:
            return _invalid(face+':'+reason, metrics)
        metrics['panels'][face] = value
    if expected_view in (1, 3):
        left, right = [metrics['panels'][face] for face in _FACES[expected_view]]
        signed = (left['horizontal_axis_delta_deg']-right['horizontal_axis_delta_deg'])/2.
        off_axis = (left['horizontal_axis_delta_deg']+right['horizontal_axis_delta_deg'])/2.
        tolerance = paired_tolerance_deg
        metrics['method'] = 'paired_opposite_horizontal_axis_direction_delta'
    else:
        panel = next(iter(metrics['panels'].values()))
        signed = panel['vertical_horizontal_shape_deg']
        off_axis = panel['polar_roll_deg']
        tolerance = main_tolerance_deg
        metrics['method'] = 'main_vertical_horizontal_relative_shape_angle'
    metrics.update(signed_image_orientation_deg=float(signed), off_axis_deg=float(off_axis),
                   arrival_tolerance_deg=tolerance, off_axis_tolerance_deg=off_axis_tolerance_deg,
                   stop_settled=False, fresh_source_checked_by_caller=False)
    if abs(off_axis) > 1.5:
        return _invalid('outside_validated_vertical_motion_family', metrics)
    readable = all(p['image_grid_readable'] for p in metrics['panels'].values())
    arrival = readable and abs(signed) <= tolerance and abs(off_axis) <= off_axis_tolerance_deg
    return dict(valid_diagnostic=True, signed_error_deg=float(signed),
                direction_up_error_sign=-1, arrival_candidate=bool(arrival),
                reason='arrival_candidate_requires_caller_stop_recheck' if arrival else
                ('grid_not_readable_diagnostic_only' if not readable else 'outside_image_orientation_tolerance'),
                metrics=metrics, formal_pose=False, full_cube=False, phase_identity=False)
