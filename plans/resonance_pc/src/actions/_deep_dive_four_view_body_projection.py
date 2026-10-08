"""Visible rigid-body fit for side-peek candidate explanation only.

Projected unobserved cells are never glyph observations, negative occupancy
proof, or target assignments. Noncoplanar pairs may estimate face distance.
A single face requires a same-epoch prior from an accepted noncoplanar fit and
keeps all planar branches; actual side-wall normal evidence must disambiguate.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math

import cv2
import numpy as np

from ._deep_dive_planner_rules import BASES, FACES, cell_to_slot
from ._deep_dive_four_view_reader import source_key

FOV_Y_DEG = 12.
FOCAL = 360/math.tan(math.radians(FOV_Y_DEG/2))
CAMERA = np.array([[FOCAL, 0, 640.], [0, FOCAL, 360.], [0, 0, 1.]])
INITIAL_FACE_DISTANCE = 2.30268665
HALF_PANEL = .43


def _point(cell, distance):
    n, u, v = map(np.asarray, BASES[cell['face']])
    return n*distance+u*(cell['col']-1)+v*(cell['row']-1)


def _quad(cell, distance):
    _, u, v = map(np.asarray, BASES[cell['face']])
    point = _point(cell, distance)
    return np.asarray([point+u*x*HALF_PANEL+v*y*HALF_PANEL
                       for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))], float)


def _unknown(reason, source, epoch, evidence=None):
    return dict(status='unknown', body_pose_supported=False, reason=reason,
                source=deepcopy(source), scan_epoch=epoch, candidate_cells=[],
                fit_evidence=evidence or {}, hidden_cells_observed=False,
                usable_for_node_reading=False, negative_occupancy_proof=False,
                target_assignment_confirmed=False, formal_pose=False)


def _pose_candidate(cells, query, distance, rotation, translation):
    objects = np.asarray([_point(c, distance) for c in cells], float)
    r = cv2.Rodrigues(rotation)[0]
    camera_points = objects@r.T+translation.reshape(1, 3)
    projected = cv2.projectPoints(objects, rotation, translation, CAMERA, None)[0][:, 0]
    residuals = np.linalg.norm(projected-query, axis=1)
    inliers = residuals <= 4.5
    rms = float(np.sqrt(np.mean(residuals[inliers]**2))) if np.any(inliers) else float('inf')
    return dict(rotation_vector=rotation.ravel().tolist(), rotation_matrix=r.tolist(),
                translation=translation.ravel().tolist(), distance=float(distance),
                residuals_px=residuals.tolist(), rms_inlier_px=rms,
                rms_all_px=float(np.sqrt(np.mean(residuals**2))),
                inlier_indices=np.flatnonzero(inliers).tolist(),
                all_depth_positive=bool(np.all(camera_points[:, 2] > 1)),
                visible_normals={f:(r@np.asarray(BASES[f][0])).tolist() for f in {c['face'] for c in cells}},
                actual_observation_type='centers_of_current_RGB_supported_visible_panel_quads')


def _paired_fit(cells, query):
    trials = []
    for distance in np.linspace(1.7, 3.4, 81):
        objects = np.asarray([_point(c, distance) for c in cells], float)
        ok, rotation, translation = cv2.solvePnP(objects, query, CAMERA, None, flags=cv2.SOLVEPNP_EPNP)
        if not ok:
            continue
        ok, rotation, translation = cv2.solvePnP(objects, query, CAMERA, None, rotation,
                                                translation, True, flags=cv2.SOLVEPNP_ITERATIVE)
        if ok:
            fit = _pose_candidate(cells, query, distance, rotation, translation)
            if fit['all_depth_positive'] and all(n[2] < -.1 for n in fit['visible_normals'].values()):
                trials.append(fit)
    if not trials:
        return None, []
    best = min(trials, key=lambda x:x['rms_all_px'])
    # Refine the actually supported profile around its minimum, not a fixed
    # historical DISTANCE. All trials are retained for uncertainty inspection.
    low, high = max(1.7, best['distance']-.022), min(3.4, best['distance']+.022)
    for distance in np.linspace(low, high, 15):
        objects = np.asarray([_point(c, distance) for c in cells], float)
        rotation = np.asarray(best['rotation_vector'], float).reshape(3, 1)
        translation = np.asarray(best['translation'], float).reshape(3, 1)
        ok, rotation, translation = cv2.solvePnP(objects, query, CAMERA, None, rotation,
                                                translation, True, flags=cv2.SOLVEPNP_ITERATIVE)
        if ok:
            fit = _pose_candidate(cells, query, distance, rotation, translation)
            if fit['all_depth_positive']:
                trials.append(fit)
    best = min(trials, key=lambda x:x['rms_all_px'])
    return best, [dict(distance=x['distance'], rms_all_px=x['rms_all_px']) for x in trials]


def _planar_fits(cells, query, distance):
    objects = np.asarray([_point(c, distance) for c in cells], float)
    result = cv2.solvePnPGeneric(objects, query, CAMERA, None, flags=cv2.SOLVEPNP_IPPE)
    return [_pose_candidate(cells, query, distance, r, t) for r, t in zip(result[1], result[2])]


def project_sidepeek_candidates(rgb, geometry, *, source, scan_epoch, distance_prior=None):
    try:
        key = source_key(source)
    except ValueError as exc:
        return _unknown(str(exc), source, scan_epoch)
    image = np.asarray(rgb)
    if image.shape != (720, 1280, 3) or image.dtype != np.uint8:
        return _unknown('actual_1280x720_RGB_required', source, scan_epoch)
    digest = hashlib.sha256(np.ascontiguousarray(image).tobytes()).hexdigest()
    if not isinstance(scan_epoch, str) or not scan_epoch or source.get('scan_epoch', scan_epoch) != scan_epoch:
        return _unknown('actual_scan_epoch_required', source, scan_epoch)
    if geometry.get('scan_epoch', scan_epoch) != scan_epoch:
        return _unknown('geometry_scan_epoch_disagrees', source, scan_epoch)
    if geometry.get('source') is not None:
        try:
            if source_key(geometry['source']) != key:
                return _unknown('geometry_capture_source_disagrees', source, scan_epoch)
        except ValueError:
            return _unknown('invalid_geometry_capture_source', source, scan_epoch)
    if geometry.get('proposal_accepted') is not True or geometry.get('source_rgb_sha256') != digest:
        return _unknown('current_RGB_supported_geometry_required', source, scan_epoch)
    cells = geometry.get('cells', [])
    try:
        slots = [cell_to_slot(c) for c in cells]
        if len(set(slots)) != len(slots):
            return _unknown('duplicate_actual_cell_identity', source, scan_epoch)
        if any(np.asarray(c['quad']).shape != (4, 2) for c in cells):
            return _unknown('actual_quad_required', source, scan_epoch)
    except (KeyError, ValueError, TypeError):
        return _unknown('invalid_actual_cell_identity_or_quad', source, scan_epoch)
    faces = {c.get('face') for c in cells}
    if not faces or not faces.issubset(set(FACES)) or len(faces) not in (1, 2):
        return _unknown('one_or_two_observed_faces_required', source, scan_epoch)
    query = np.asarray([np.mean(c['quad'], axis=0) for c in cells], float)
    if query.shape != (len(cells), 2) or not np.isfinite(query).all():
        return _unknown('invalid_current_observed_centers', source, scan_epoch)
    for face in faces:
        current = [c for c in cells if c['face'] == face]
        centers = np.asarray([np.mean(c['quad'], axis=0) for c in current])
        if len(current) != 9 or len({(c['row'], c['col']) for c in current}) != 9 or min(np.ptp(centers, axis=0)) < 100:
            return _unknown('actual_face_coverage_below_9_cells_100x100', source, scan_epoch)
    evidence = dict(camera_fov_y_deg=FOV_Y_DEG, camera_matrix=CAMERA.tolist(),
                    initial_face_distance_only=INITIAL_FACE_DISTANCE,
                    source_rgb_sha256=digest, observed_cells=len(cells), observed_faces=sorted(faces),
                    actual_matched_data=[dict(face=c['face'], row=c['row'], col=c['col'],
                        source_supported_quad=c['quad'], source_supported_center=query[i].tolist(),
                        source=deepcopy(source)) for i, c in enumerate(cells)],
                    cube_mesh_model_is_approximate=True, physical_pose_verified=False)
    if len(faces) == 2:
        normals = [np.asarray(BASES[f][0]) for f in faces]
        if abs(normals[0]@normals[1]) != 0:
            return _unknown('actual_pair_not_nonparallel', source, scan_epoch, evidence)
        fit, profile = _paired_fit(cells, query)
        evidence.update(non_coplanar=True, distance_profile=profile, distance_source='actual_noncoplanar_visible_pair')
    else:
        if not isinstance(distance_prior, dict) or not distance_prior.get('body_pose_supported'):
            return _unknown('single_face_distance_unobservable_requires_pair_prior', source, scan_epoch, evidence)
        try:
            prior_key = source_key(distance_prior['source'])
        except (KeyError, ValueError):
            return _unknown('invalid_observed_pair_prior', source, scan_epoch, evidence)
        if (distance_prior.get('scan_epoch') != scan_epoch or prior_key[:2] != key[:2]
                or prior_key[2] >= key[2] or distance_prior.get('fit_evidence', {}).get('non_coplanar') is not True):
            return _unknown('pair_prior_epoch_session_or_source_disagrees', source, scan_epoch, evidence)
        distance = distance_prior['distance']
        if not isinstance(distance, (float, int)) or not math.isfinite(distance) or not 1.7 <= distance <= 3.4:
            return _unknown('invalid_pair_estimated_distance', source, scan_epoch, evidence)
        branches = _planar_fits(cells, query, distance)
        witnessed = [c for c in cells if c.get('normal_projection') is not None
                     and c.get('normal_evidence', {}).get('source_rgb_sha256') == digest
                     and (c['normal_evidence'].get('actual_sidewall_witnesses')
                          or c['normal_evidence'].get('actual_same_face_sidewall_witnesses'))]
        candidates = []
        for branch in branches:
            face = next(iter(faces))
            n = np.asarray(branch['visible_normals'][face])
            agrees = []
            for c in witnessed:
                observed = np.asarray(c['normal_projection'])
                agrees.append(float(n[:2]@observed/(max(np.linalg.norm(n[:2])*np.linalg.norm(observed), 1e-8))))
            if len(agrees) >= 3 and min(agrees) > .5 and branch['all_depth_positive'] and n[2] < -.1:
                candidates.append(branch)
        evidence.update(non_coplanar=False, planar_branches=branches,
                        actual_sidewall_witness_cells=len(witnessed),
                        distance_source='same_epoch_actual_pair_prior',
                        distance_prior_source=deepcopy(distance_prior['source']),
                        branch_selection='actual_current_sidewall_outward_sign_not_lowest_residual')
        if len(candidates) != 1:
            return _unknown('planar_branches_not_uniquely_supported_by_actual_sidewall', source, scan_epoch, evidence)
        fit = candidates[0]
    if fit is None:
        return _unknown('no_finite_actual_body_fit', source, scan_epoch, evidence)
    minimum = 12 if len(faces) == 2 else 7
    inliers = fit['inlier_indices']
    if (len(inliers) < minimum or fit['rms_inlier_px'] > 3. or fit['rms_all_px'] > 3.2
            or max(fit['residuals_px']) > 6. or not fit['all_depth_positive']):
        evidence['rejected_fit'] = fit
        return _unknown('actual_body_reprojection_gate_failed', source, scan_epoch, evidence)
    if len(faces) == 2 and any(sum(cells[i]['face'] == f for i in inliers) < 5 for f in faces):
        return _unknown('body_inliers_not_supported_on_both_actual_faces', source, scan_epoch, evidence)
    evidence['accepted_visible_reprojection'] = fit
    rotation, translation = np.asarray(fit['rotation_vector']).reshape(3, 1), np.asarray(fit['translation']).reshape(3, 1)
    matrix, distance = np.asarray(fit['rotation_matrix']), fit['distance']
    candidates = []
    for face in FACES:
        normal = matrix@np.asarray(BASES[face][0])
        if face in faces or normal[2] > .18:
            continue
        for row in range(3):
            for col in range(3):
                cell = dict(face=face, row=row, col=col)
                point = _point(cell, distance)
                camera_point = matrix@point+translation.ravel()
                if camera_point[2] <= 1:
                    continue
                quad = cv2.projectPoints(_quad(cell, distance), rotation, translation, CAMERA, None)[0][:, 0]
                center = cv2.projectPoints(point[None].astype(float), rotation, translation, CAMERA, None)[0].ravel()
                if not np.isfinite(quad).all() or not (300 <= center[0] <= 960 and 55 <= center[1] <= 680):
                    continue
                # Full glyph-panel width is .86 existing cell-spacing units.
                outward = (CAMERA[:2, :2]@normal[:2]/camera_point[2]
                           - CAMERA[:2, :2]@camera_point[:2]*normal[2]/camera_point[2]**2)*.86
                candidates.append(dict(cell, slot=cell_to_slot(cell), quad=quad.tolist(), center=center.tolist(),
                    canonical_face=face,
                    projection_evidence=dict(status='visible_body_fit_supported_hidden_candidate_only',
                        actual_fit_supported=True, source_rgb_sha256=digest,
                        visible_fit_rms_px=fit['rms_inlier_px'], inlier_count=len(fit['inlier_indices']),
                        actual_matched_data=evidence['actual_matched_data'],
                        physically_observed_hidden_cell=False, negative_occupancy_proof=False),
                    normal_projection=outward.tolist(), normal_evidence=dict(
                        method='actual_visible_rigid_body_fit_projected_sidepeek_candidate',
                        source_rgb_sha256=digest, projection_unit='one_glyph_panel_width',
                        visible_fit_rms_px=fit['rms_inlier_px'], physical_pose_verified=False),
                    visibility='front_or_tangent_candidate_only', actually_observed=False,
                    candidate_only=True, usable_for_node_reading=False, negative_occupancy_proof=False))
    return dict(status='sidepeek_candidates_only', body_pose_supported=True,
                source=deepcopy(source), scan_epoch=scan_epoch, distance=distance,
                candidate_cells=candidates, fit_evidence=evidence,
                hidden_cells_observed=False, usable_for_node_reading=False,
                negative_occupancy_proof=False, target_assignment_confirmed=False, formal_pose=False)
