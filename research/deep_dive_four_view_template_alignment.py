"""Independent reference-panel pixel homographies. Research proposals only.

This module reads no expected_icon/canonical-face labels, performs no capture,
input, neural inference or 3D reconstruction, and never establishes view identity.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np


def _rgb(rgb):
    rgb = np.asarray(rgb)
    if rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
        raise ValueError("requires_1280x720_uint8_rgb")
    return rgb


def position_search_mask(reference_points, query_points, radius=(60., 60.)):
    """Association search only; this is NOT an arrival tolerance or pose prior."""
    reference = np.asarray(reference_points, np.float32).reshape(-1, 2)
    query = np.asarray(query_points, np.float32).reshape(-1, 2)
    radius = np.asarray(radius, np.float32)
    if radius.shape != (2,) or not np.isfinite(radius).all() or min(radius) <= 0:
        raise ValueError('invalid_position_search_radius')
    difference = np.abs(reference[:, None, :] - query[None, :, :])
    return np.all(difference <= radius, axis=2).astype(np.uint8)


def camera_branches(homography):
    """All mathematical branches under an ASSUMED 12-degree vertical FOV.

    Plane/image correspondences do not establish the actual camera model,
    cheirality, the selected physical branch or the accumulated scan angle.
    """
    focal = 360. / math.tan(math.radians(6.))
    intrinsic = np.array([[focal, 0., 640.], [0., focal, 360.], [0., 0., 1.]])
    count, rotations, translations, normals = cv2.decomposeHomographyMat(
        np.asarray(homography, np.float64), intrinsic)
    branches = []
    for index in range(count):
        rotation = rotations[index]
        angle = math.degrees(math.acos(float(np.clip((np.trace(rotation)-1.)/2., -1., 1.))))
        branches.append(dict(index=index, rotation=rotation.tolist(),
                             reference_relative_rotation_deg=angle,
                             translation_over_plane_distance=translations[index].ravel().tolist(),
                             plane_normal=normals[index].ravel().tolist()))
    return dict(intrinsic=intrinsic.tolist(), assumed_vertical_fov_deg=12.,
                branches=branches, ambiguous=count != 1, selected_branch=None,
                physical_branch_verified=False, formal_pose=False)


def fit_pairs(reference_points, query_points, cell_ids):
    """Fit one panel's actual matched pixels and expose failed coverage gates."""
    reference = np.asarray(reference_points, np.float32).reshape(-1, 2)
    query = np.asarray(query_points, np.float32).reshape(-1, 2)
    if len(reference) != len(query) or len(reference) != len(cell_ids):
        raise ValueError("pair_lengths_disagree")
    if not np.isfinite(reference).all() or not np.isfinite(query).all():
        raise ValueError("nonfinite_actual_pairs")
    result = dict(proposal_accepted=False, reason="too_few_ratio_matches",
                  homography=None, inlier_indices=[], residual_px=[], coverage={},
                  phase_identity=False, targets_ready=False, full_cube=False,
                  formal_recognition=False, formal_pose=False)
    if len(reference) < 4:
        return result
    h, mask = cv2.findHomography(reference, query, cv2.RANSAC, 3.,
                                maxIters=2000, confidence=.995)
    if h is None or mask is None or not np.isfinite(h).all() or abs(h[2, 2]) < 1e-12:
        result['reason'] = 'no_finite_homography'
        return result
    h /= h[2, 2]
    predicted = cv2.perspectiveTransform(reference[None], h)[0]
    residual = np.linalg.norm(predicted-query, axis=1)
    inliers = (mask.ravel() != 0) & np.isfinite(residual) & (residual <= 3.)
    indices = np.flatnonzero(inliers)
    rspan = np.ptp(reference[inliers], axis=0) if len(indices) else np.zeros(2)
    qspan = np.ptp(query[inliers], axis=0) if len(indices) else np.zeros(2)
    cells = sorted(set(str(cell_ids[i]) for i in indices))
    failures = []
    if len(indices) < 12:
        failures.append('fewer_than_12_actual_inliers')
    if min(rspan) < 100. or min(qspan) < 100.:
        failures.append('actual_support_span_below_100x100')
    if len(cells) < 3:
        failures.append('fewer_than_3_reference_cells')
    result.update(proposal_accepted=not failures, reason='proposal_only' if not failures else '|'.join(failures),
                  homography=h.tolist(), inlier_indices=indices.tolist(), residual_px=residual.tolist(),
                  coverage=dict(inliers=len(indices), ratio_matches=len(reference), cells=cells,
                                reference_span_px=rspan.tolist(), query_span_px=qspan.tolist(),
                                median_inlier_residual_px=float(np.median(residual[inliers])) if len(indices) else None),
                  decomposition=camera_branches(h))
    return result


class FourViewTemplateAligner:
    def __init__(self, annotation_path, repo_root, *, query_roi=(300, 84, 950, 615),
                 search_radius=(60., 60.)):
        self.repo_root = Path(repo_root)
        payload = json.loads(Path(annotation_path).read_text(encoding='utf-8'))
        self.query_roi = query_roi
        self.search_radius = search_radius
        self.sift = cv2.SIFT_create(nfeatures=2500)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.references = []
        for group in payload['views']:
            image_path = self.repo_root / group['image']
            bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
            if bgr is None or bgr.shape != (720, 1280, 3):
                raise ValueError('reference_image_missing_or_wrong_shape')
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            cell_map = np.full(gray.shape, -1, np.int16)
            cells = []
            for cell in group['cells']:
                # Intentionally enumerate ONLY visible geometry and local indices.
                cells.append(dict(row=cell['row'], col=cell['col'], quad=cell['quad'],
                                  occluded=bool(cell['occluded'])))
                if cell['occluded']:
                    continue
                cv2.fillConvexPoly(cell_map, np.rint(cell['quad']).astype(np.int32), len(cells)-1)
            mask = (cell_map >= 0).astype(np.uint8)*255
            mask = cv2.erode(mask, np.ones((3, 3), np.uint8))
            keypoints, descriptors = self.sift.detectAndCompute(gray, mask)
            ids = []
            for keypoint in keypoints:
                x, y = np.rint(keypoint.pt).astype(int)
                index = int(cell_map[y, x])
                ids.append(f"{cells[index]['row']},{cells[index]['col']}")
            self.references.append(dict(view=group['view'], face=group['face'], cells=cells,
                                        keypoints=keypoints, descriptors=descriptors, cell_ids=ids,
                                        source_file_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest()))

    def align(self, rgb, *, reference_view=None):
        rgb = _rgb(rgb)
        started = time.perf_counter()
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        mask = np.zeros(gray.shape, np.uint8)
        x1, y1, x2, y2 = self.query_roi
        mask[y1:y2, x1:x2] = 255
        query_keypoints, query_descriptors = self.sift.detectAndCompute(gray, mask)
        results = []
        for ref in self.references:
            if reference_view is not None and ref['view'] != reference_view:
                continue
            candidates, eligible = [], []
            excluded_prior_count = 0
            if ref['descriptors'] is not None and query_descriptors is not None and len(query_descriptors) >= 2:
                association_mask = None
                if self.search_radius is not None:
                    association_mask = position_search_mask(
                        [keypoint.pt for keypoint in ref['keypoints']],
                        [keypoint.pt for keypoint in query_keypoints], self.search_radius)
                    excluded_prior_count = int(association_mask.size-np.count_nonzero(association_mask))
                for match_pair in self.matcher.knnMatch(ref['descriptors'], query_descriptors, k=2,
                                                       mask=association_mask):
                    if len(match_pair) != 2:
                        continue
                    first, second = match_pair
                    passed = first.distance < .70*second.distance
                    row = dict(reference_index=first.queryIdx, query_index=first.trainIdx,
                               reference_point=list(ref['keypoints'][first.queryIdx].pt),
                               query_point=list(query_keypoints[first.trainIdx].pt),
                               reference_cell=ref['cell_ids'][first.queryIdx],
                               distance=float(first.distance), competitor_distance=float(second.distance),
                               ratio=float(first.distance/max(second.distance, 1e-12)), ratio_pass=passed)
                    candidates.append(row)
                    if passed:
                        eligible.append(row)
            # A query descriptor cannot supply independent support twice.
            unique = {}
            for row in eligible:
                key = row['query_index']
                if key not in unique or row['distance'] < unique[key]['distance']:
                    unique[key] = row
            pairs = list(unique.values())
            fit = fit_pairs([p['reference_point'] for p in pairs], [p['query_point'] for p in pairs],
                            [p['reference_cell'] for p in pairs])
            inliers = set(fit['inlier_indices'])
            for index, pair in enumerate(pairs):
                pair['inlier'] = index in inliers
                pair['residual_px'] = fit['residual_px'][index] if fit['residual_px'] else None
            projected = []
            if fit['homography'] is not None:
                h = np.asarray(fit['homography'])
                for cell in ref['cells']:
                    quad = cv2.perspectiveTransform(np.asarray(cell['quad'], np.float32)[None], h)[0]
                    projected.append(dict(row=cell['row'], col=cell['col'],
                                          reference_occluded=cell['occluded'], quad=quad.tolist()))
            projection_failures = []
            reset_quad = np.float32([[605,494],[677,494],[677,579],[605,579]])
            for cell in projected:
                quad = np.asarray(cell['quad'], np.float32)
                name = f"{cell['row']},{cell['col']}"
                if not np.isfinite(quad).all() or not cv2.isContourConvex(quad):
                    projection_failures.append(name+':nonfinite_or_nonconvex')
                    continue
                if (quad[:,0].min()<x1 or quad[:,0].max()>x2 or
                        quad[:,1].min()<y1 or quad[:,1].max()>y2):
                    projection_failures.append(name+':outside_cube_roi')
                if cv2.intersectConvexConvex(quad, reset_quad)[0] > 0:
                    projection_failures.append(name+':overlaps_reset_hud')
            if projection_failures:
                fit['proposal_accepted'] = False
                fit['reason'] += '|projected_grid_geometry_rejected'
            results.append(dict(view=ref['view'], reference_panel=ref['face'], **fit,
                                reference_source_file_sha256=ref['source_file_sha256'],
                                reference_feature_count=len(ref['keypoints']),
                                all_descriptor_candidates=candidates, matched_pairs=pairs,
                                association_search_radius_px=None if self.search_radius is None else list(self.search_radius),
                                excluded_prior_count=excluded_prior_count,
                                spatial_prior_role='search_only_not_arrival',
                                projection_failures=projection_failures,
                                duplicate_query_matches_dropped=len(eligible)-len(pairs),
                                projected_reference_cells=projected))
        return dict(query_rgb_sha256=hashlib.sha256(rgb.tobytes()).hexdigest(),
                    query_feature_count=len(query_keypoints), query_roi=list(self.query_roi),
                    all_actual_query_keypoints=[dict(point=list(k.pt), size=k.size,
                                                      angle=k.angle, response=k.response)
                                                for k in query_keypoints],
                    results=results, elapsed_seconds=time.perf_counter()-started,
                    geometry_proposal_only=True, phase_identity=False, targets_ready=False,
                    full_cube=False, formal_pose=False, formal_recognition=False)
