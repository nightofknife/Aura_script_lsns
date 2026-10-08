"""Research-only independent planar tracking for animated cube faces.

Face translations can absorb non-rigid breathing. They are *not* a rigid cube
center, a reconstructed 54-cell atlas, or target-association evidence.
"""
import math

import cv2
import numpy as np

from plans.resonance_pc.src.actions._deep_dive_layout_vision import (
    BASES, DISTANCE, K, LayoutScanner, _angle)


def _rotation_valid(value):
    return (value.shape == (3, 3) and np.isfinite(value).all()
            and np.allclose(value.T @ value, np.eye(3), atol=1e-5)
            and abs(np.linalg.det(value)-1.) < 1e-5)


def _support(points):
    span = np.ptp(points, axis=0)
    area = float(cv2.contourArea(cv2.convexHull(np.asarray(points, np.float32))))
    return span, area


def fit_face_plane(objects, image_points, face, previous_rotation,
                   previous_translation, camera_matrix=K, *, limit=30.,
                   min_points=25, min_span_px=60., min_area_px=1500.,
                   max_error_px=2., min_fraction=.75, plane_tolerance=1e-4,
                   ambiguity_angle_gap_deg=1., ambiguity_error_gap_px=.15,
                   min_depth_ratio=.7, max_depth_ratio=1.3):
    """Fit one bound face using RANSAC support and explicit planar branches.

    Input object coordinates retain the scanner's original body coordinates.
    Success only supports a local planar geometry hypothesis. ``translation``
    applies to this face alone; callers must not project the other five faces
    with it. Near-equal distinct branches are rejected, not silently guessed.
    """
    report = dict(success=False, reason='invalid_input', face=face)
    try:
        obj = np.asarray(objects, float).reshape(-1, 3)
        pixels = np.asarray(image_points, float).reshape(-1, 2)
        previous = np.asarray(previous_rotation, float)
        translation = np.asarray(previous_translation, float).reshape(3, 1)
        camera = np.asarray(camera_matrix, float)
        normal, right, down = [np.asarray(item, float) for item in BASES[face]]
        values = np.asarray([limit, min_points, min_span_px, min_area_px,
            max_error_px, min_fraction, plane_tolerance, ambiguity_angle_gap_deg,
            ambiguity_error_gap_px, min_depth_ratio, max_depth_ratio], float)
        if (len(obj) != len(pixels) or not _rotation_valid(previous)
                or camera.shape != (3, 3) or not np.isfinite(values).all()
                or not all(np.isfinite(value).all() for value in (obj, pixels, translation, camera))
                or translation[2, 0] <= 0 or camera[0, 0] <= 0 or camera[1, 1] <= 0):
            return report
        if not (min_points >= 25 and min_span_px >= 60 and min_area_px >= 1500
                and 0 < max_error_px <= 2 and .75 <= min_fraction < 1
                and 0 < limit <= 90 and 0 < plane_tolerance <= .01
                and 0 < min_depth_ratio <= 1 <= max_depth_ratio
                and ambiguity_angle_gap_deg >= 0 and ambiguity_error_gap_px >= 0):
            report['reason'] = 'invalid_configuration'
            return report
        original_indices = np.flatnonzero(np.abs(obj @ normal-DISTANCE) <= plane_tolerance)
        obj, pixels = obj[original_indices], pixels[original_indices]
        report['supplied_face_points'] = len(obj)
        if len(obj) < min_points:
            report['reason'] = 'insufficient_face_points'
            return report
        span, area = _support(pixels)
        report.update(span_px=span.tolist(), convex_area_px=area)
        if min(span) < min_span_px or area < min_area_px:
            report['reason'] = 'face_support_too_local'
            return report
        plane_xy = np.column_stack((obj @ right, obj @ down))
        _, mask = cv2.findHomography(plane_xy, pixels, cv2.RANSAC, max_error_px,
                                    maxIters=200, confidence=.995)
        if mask is None:
            report['reason'] = 'homography_ransac_failed'
            return report
        inside = np.flatnonzero(mask.ravel())
        if len(inside) < min_points or len(inside)/len(obj) <= min_fraction:
            report['reason'] = 'insufficient_ransac_support'
            return report
        # Generic IPPE's internal conversion of offset planes can produce
        # severely wrong poses for reprojected clouds with floating residuals
        # around 1e-15. Give it a literal z=0 plane and compose back explicitly.
        # The original objects/pixels and every acceptance gate remain intact.
        canonical = np.column_stack((plane_xy, np.zeros(len(plane_xy))))
        basis = np.column_stack((right, down, np.cross(right, down)))
        origin = normal*DISTANCE
        solved = cv2.solvePnPGeneric(np.ascontiguousarray(canonical[inside]),
            np.ascontiguousarray(pixels[inside]), camera, None, flags=cv2.SOLVEPNP_IPPE)
        report['solver_coordinates'] = 'explicit_face_plane_uv0'
        branches = []
        branch_diagnostics = []
        for plane_rv, plane_tv in zip(solved[1], solved[2]):
            rotation = cv2.Rodrigues(plane_rv)[0] @ basis.T
            tv = plane_tv-rotation @ origin.reshape(3, 1)
            rv = cv2.Rodrigues(rotation)[0]
            predicted = cv2.projectPoints(obj, rv, tv, camera, None)[0].reshape(-1, 2)
            errors = np.linalg.norm(predicted-pixels, axis=1)
            camera_points = obj @ rotation.T + tv.reshape(1, 3)
            support = np.flatnonzero(errors <= max_error_px)
            jump = _angle(rotation, previous)
            depth_ratio = float(tv[2, 0]/translation[2, 0])
            valid = (_rotation_valid(rotation) and np.isfinite(tv).all()
                     and np.all(camera_points[:, 2] > 0) and jump <= limit
                     and (rotation @ normal) @ -(tv.reshape(3)+rotation @ (normal*DISTANCE)) > 0
                     and min_depth_ratio <= depth_ratio <= max_depth_ratio
                     and len(support) >= min_points and len(support)/len(obj) > min_fraction)
            median = float(np.median(errors))
            branch_diagnostics.append(dict(prior_angle_deg=jump, median_error_px=median,
                inliers=len(support), valid=bool(valid), depth_ratio=depth_ratio))
            if valid:
                branches.append((jump, median, rv, tv, rotation, support))
        report['branches'] = branch_diagnostics
        if not branches:
            report['reason'] = 'no_valid_planar_branch'
            return report
        # A clearly better image fit disambiguates before the continuity prior.
        # Within the error envelope, only the previous physical pose can help.
        best_error = min(branch[1] for branch in branches)
        branches = [branch for branch in branches
                    if branch[1] <= best_error+ambiguity_error_gap_px]
        branches.sort(key=lambda item: (item[0], item[1]))
        selected = branches[0]
        if len(branches) > 1:
            other = branches[1]
            separation = _angle(selected[4], other[4])
            angle_gap = abs(selected[0]-other[0])
            error_gap = abs(selected[1]-other[1])
            report.update(branch_separation_deg=separation, branch_prior_gap_deg=angle_gap,
                          branch_error_gap_px=error_gap)
            if separation > .1 and angle_gap < ambiguity_angle_gap_deg and error_gap < ambiguity_error_gap_px:
                report['reason'] = 'ambiguous_planar_branches'
                return report
        _, _, rv, tv, _, support = selected
        rv, tv = cv2.solvePnPRefineLM(np.ascontiguousarray(obj[support]),
            np.ascontiguousarray(pixels[support]), camera, None, rv.copy(), tv.copy(),
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 1e-7))
        rotation = cv2.Rodrigues(rv)[0]
        predicted = cv2.projectPoints(obj, rv, tv, camera, None)[0].reshape(-1, 2)
        errors = np.linalg.norm(predicted-pixels, axis=1)
        support = np.flatnonzero(errors <= max_error_px)
        if len(support) < min_points or len(support)/len(obj) <= min_fraction:
            report['reason'] = 'refined_support_insufficient'
            return report
        span, area = _support(pixels[support])
        depth_ratio = float(tv[2, 0]/translation[2, 0])
        if (not _rotation_valid(rotation) or not np.isfinite(tv).all()
                or _angle(rotation, previous) > limit or min(span) < min_span_px or area < min_area_px
                or not min_depth_ratio <= depth_ratio <= max_depth_ratio
                or np.any((obj @ rotation.T + tv.reshape(1, 3))[:, 2] <= 0)):
            report['reason'] = 'refined_pose_invalid'
            return report
        report.update(success=True, reason='local_face_plane_geometry_only',
            rotation=rotation.tolist(), rvec=rv.reshape(3).tolist(), translation=tv.reshape(3).tolist(),
            face_normal_camera=(rotation @ normal).tolist(),
            translation_delta_camera=(tv-translation).reshape(3).tolist(),
            normal_translation_delta=float((tv-translation).reshape(3) @ (rotation @ normal)),
            inlier_indices=original_indices[support].tolist(), inliers=len(support),
            fraction=len(support)/len(obj), median_error_px=float(np.median(errors[support])),
            max_error_px=float(np.max(errors[support])), span_px=span.tolist(), convex_area_px=area,
            prior_angle_deg=_angle(rotation, previous), depth_ratio=depth_ratio)
    except (ValueError, TypeError, KeyError, IndexError, cv2.error) as exc:
        report.update(reason='plane_fit_exception', detail=str(exc))
    return report


class FacePlaneScanner(LayoutScanner):
    """Research adapter; accepted translation belongs only to selected face.

    Original object coordinates are retained for optical-flow correspondences.
    No full-cube physical reconstruction or classification proof is produced.
    """
    def __init__(self, *args, preferred_face='U', **kwargs):
        super().__init__(*args, **kwargs)
        self.preferred_face = preferred_face
        self.face_plane_diagnostic = {}
        self._plane_projection_face = None

    def visible(self, rotation=None):
        items = super().visible(rotation=rotation)
        face = getattr(self, '_plane_projection_face', None)
        if face is None or not getattr(self, 'ready', False):
            return items
        # An accepted independent translation is valid only for this surface.
        # This also constrains feature seeding/unprojection and drag selection.
        return [item for item in items if self.cells[item['index']]['face'] == face]

    def _fit_pose(self, objects, points, limit=30):
        reports = {}
        order = [self.preferred_face] + [face for face in BASES if face != self.preferred_face]
        selected = None
        for face in order:
            report = fit_face_plane(objects, points, face, self.rotation, self.tvec, limit=limit)
            reports[face] = report
            if report['success']:
                selected = report
                break
        self.face_plane_diagnostic = dict(geometry_only=True, faces=reports,
            selected_face=None if selected is None else selected['face'],
            full_cube_coordinates_valid=False, target_evidence=False)
        if selected is None:
            self.last_error = 'independent_face_plane_not_supported'
            return False
        indices = selected['inlier_indices']
        self.rotation = np.asarray(selected['rotation'], float)
        self.rvec = np.asarray(selected['rvec'], float).reshape(3, 1)
        self.tvec = np.asarray(selected['translation'], float).reshape(3, 1)
        self.quality = min(.97, selected['fraction'])
        self._plane_projection_face = selected['face']
        self.objects = np.asarray(objects, float)[indices]
        self.points = np.asarray(points, np.float32)[indices]
        return True
