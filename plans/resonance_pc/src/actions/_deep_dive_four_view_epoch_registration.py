"""Same-scan local registration from an automatically observed standard view.

No saved board template or semantic teacher enters this API. The caller creates
one instance only after the ordered standard pose has three real stable frames.
SIFT features come from that epoch's actual RGB inside detected panel quads.
Every visible face fits its own homography; this is local motion feedback, not
an independent face identity or a physical pose-separation measurement.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import time

import cv2
import numpy as np

from ._deep_dive_four_view_geometry import (
    VIEW_PANELS, FRAME_CONVENTION, canonical_cell, project_points,
    attach_current_surface_normals,
)
from ._deep_dive_four_view_reader import source_key


def _rgb(rgb):
    image = np.asarray(rgb)
    if image.shape != (720, 1280, 3) or image.dtype != np.uint8:
        raise ValueError('expected_actual_1280x720_uint8_rgb')
    return image


def _unknown(reason, view, epoch, source=None):
    return dict(status='unknown', proposal_accepted=False, cells=[], results=[],
                reason=reason, expected_view=view, scan_epoch=epoch,
                source=deepcopy(source), query_feature_count=0,
                identity_from_geometry=False, phase_ownership_validated_by_caller=False,
                formal_pose=False, frame_convention=FRAME_CONVENTION,
                feedback=dict(valid_diagnostic=False, signed_error_deg=None,
                              arrival_candidate=False, direction_up_error_sign=-1,
                              reason=reason, formal_pose=False, phase_identity=False))


def _exclude_dynamic_targets(mask, targets):
    """Mask current detector boxes plus 15 percent, without semantic teachers."""
    excluded = []
    for target in targets or []:
        box = target.get('box') if isinstance(target, dict) else None
        if box is None:
            continue
        coordinates = np.asarray(box, float)
        if coordinates.shape != (4,) or not np.isfinite(coordinates).all() or min(coordinates[2:]) <= 0:
            raise ValueError('invalid_actual_target_exclusion_box')
        x, y, w, h = coordinates
        px, py = max(4., .15*w), max(4., .15*h)
        left, top = max(0, int(math.floor(x-px))), max(0, int(math.floor(y-py)))
        right, bottom = min(1280, int(math.ceil(x+w+px))), min(720, int(math.ceil(y+h+py)))
        if right > left and bottom > top:
            mask[top:bottom, left:right] = 0
            excluded.append(dict(actual_detector_box=coordinates.tolist(),
                                 excluded_rectangle=[left, top, right, bottom], padding_fraction=.15))
    return excluded


def _fit(reference, query, cells):
    outcome = dict(homography=None, proposal_accepted=False, matched_pairs=[],
                   reason='fewer_than_4_actual_pairs', projection_failures=[])
    if len(reference) < 4:
        return outcome
    a, b = np.asarray(reference, np.float32), np.asarray(query, np.float32)
    h, mask = cv2.findHomography(a, b, cv2.RANSAC, 3., maxIters=2000, confidence=.995)
    if h is None or mask is None or not np.isfinite(h).all() or abs(h[2, 2]) < 1e-10:
        outcome['reason'] = 'no_finite_actual_homography'
        return outcome
    h /= h[2, 2]
    errors = np.linalg.norm(project_points(a, h)-b, axis=1)
    inliers = (mask.ravel() > 0) & (errors <= 3.)
    reference_span = np.ptp(a[inliers], axis=0) if np.any(inliers) else np.zeros(2)
    query_span = np.ptp(b[inliers], axis=0) if np.any(inliers) else np.zeros(2)
    covered = {cells[i] for i in np.flatnonzero(inliers)}
    failures = []
    if np.count_nonzero(inliers) < 12:
        failures.append('fewer_than_12_actual_inliers')
    if len(covered) < 3:
        failures.append('fewer_than_3_reference_cells')
    if min(reference_span) < 100 or min(query_span) < 100:
        failures.append('actual_support_span_below_100x100')
    pairs = [dict(reference_point=a[i].tolist(), query_point=b[i].tolist(),
                  reference_cell=cells[i], inlier=bool(inliers[i]), residual_px=float(errors[i]))
             for i in range(len(a))]
    outcome.update(homography=h.tolist(), matched_pairs=pairs, proposal_accepted=not failures,
                   structural_support_valid=not failures,
                   reason='actual_epoch_panel_features_supported' if not failures else '|'.join(failures),
                   actual_inliers=int(np.count_nonzero(inliers)),
                   reference_span_px=reference_span.tolist(), query_span_px=query_span.tolist(),
                   reference_cells=sorted(covered), actual_correspondence_type='same_epoch_SIFT_pixels')
    return outcome


def _jacobian_metrics(panel):
    values = []
    h = np.asarray(panel['homography'], float)
    for pair in panel['matched_pairs']:
        if not pair['inlier']:
            continue
        x, y = pair['reference_point']
        point = h@np.array([x, y, 1.])
        if abs(point[2]) < 1e-9:
            return None
        j = (h[:2, :2]*point[2]-point[:2, None]*h[2, :2])/point[2]**2
        if not np.isfinite(j).all() or np.linalg.det(j) <= 0:
            return None
        horizontal = math.degrees(math.atan2(j[1, 0], j[0, 0]))
        shape = math.degrees(math.atan2(np.linalg.norm(j[:, 1]), np.linalg.norm(j[:, 0])))-45
        roll = math.degrees(math.atan2(j[1, 0]-j[0, 1], j[0, 0]+j[1, 1]))
        values.append([horizontal, shape, roll])
    return np.median(np.asarray(values), axis=0) if values else None


def epoch_orientation_feedback(alignment, *, main_tolerance_deg=.35,
                               paired_tolerance_deg=.5, off_axis_tolerance_deg=1.5):
    view = alignment.get('expected_view')
    panels = {p['reference_panel']: p for p in alignment.get('results', [])}
    invalid = dict(valid_diagnostic=False, signed_error_deg=None, arrival_candidate=False,
                   direction_up_error_sign=-1, formal_pose=False, phase_identity=False)
    if view not in VIEW_PANELS or any(p not in panels or not panels[p].get('proposal_accepted') for p in VIEW_PANELS[view]):
        return dict(invalid, reason='epoch_actual_coverage_or_projection_missing')
    metrics = {}
    for name in VIEW_PANELS[view]:
        measured = _jacobian_metrics(panels[name])
        if measured is None:
            return dict(invalid, reason='invalid_local_projective_jacobian')
        metrics[name] = dict(horizontal_axis_delta_deg=float(measured[0]),
                             vertical_horizontal_shape_deg=float(measured[1]),
                             polar_roll_deg=float(measured[2]))
    if view in (1, 3):
        left, right = [metrics[name]['horizontal_axis_delta_deg'] for name in VIEW_PANELS[view]]
        signed, off_axis, tolerance = (left-right)/2, (left+right)/2, paired_tolerance_deg
    else:
        measured = metrics[VIEW_PANELS[view][0]]
        panel = panels[VIEW_PANELS[view][0]]
        current = np.asarray([c['center'] for c in panel.get('cells', [])], float)
        reference = np.asarray(panel.get('reference_panel_center_px'), float)
        span = panel.get('reference_panel_span_y_px', 0)
        if current.shape != (9, 2) or reference.shape != (2,) or span < 100:
            return dict(invalid, reason='actual_epoch_main_panel_centers_required')
        delta_y = float(np.mean(current[:, 1])-reference[1])
        signed = math.degrees(math.atan2(delta_y, span))
        off_axis, tolerance = measured['polar_roll_deg'], main_tolerance_deg
        measured.update(actual_panel_center_y_px=float(np.mean(current[:, 1])),
                        reference_panel_center_y_px=float(reference[1]),
                        reference_panel_span_y_px=span, actual_center_delta_y_px=delta_y,
                        method='same_epoch_actual_main_vertical_displacement_bearing')
    return dict(valid_diagnostic=abs(off_axis) <= 3., signed_error_deg=float(signed),
                arrival_candidate=bool(abs(signed) <= tolerance and abs(off_axis) <= off_axis_tolerance_deg),
                direction_up_error_sign=-1,
                reason='epoch_zero_requires_stopped_fresh_recheck', formal_pose=False, phase_identity=False,
                metrics=dict(unit=('image_vertical_displacement_bearing_degrees_not_physical_scan_angle'
                                   if view in (2, 4) else 'image_angular_shape_degrees_not_physical_scan_angle'),
                             reference_gauge='actual_same_epoch_standard_RGB_zero',
                             panels=metrics, off_axis_deg=float(off_axis), arrival_tolerance_deg=tolerance))


class EpochViewRegistration:
    def __init__(self, rgb, geometry_result, scan_epoch, source, *, stable_sources=None,
                 search_radius_px=60., provisional=False, exclusion_targets=None):
        rgb = _rgb(rgb)
        key = source_key(source)
        if not isinstance(scan_epoch, str) or not scan_epoch:
            raise ValueError('actual_scan_epoch_required')
        if source.get('scan_epoch', scan_epoch) != scan_epoch:
            raise ValueError('reference_source_epoch_disagrees')
        view = geometry_result.get('expected_view')
        if view not in VIEW_PANELS or geometry_result.get('proposal_accepted') is not True:
            raise ValueError('actual_standard_geometry_required')
        digest = hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest()
        if geometry_result.get('source_rgb_sha256') != digest:
            raise ValueError('standard_geometry_RGB_source_disagrees')
        if not 10 <= search_radius_px <= 80:
            raise ValueError('invalid_local_association_search_radius')
        if type(provisional) is not bool:
            raise ValueError('provisional_reference_flag_must_be_bool')
        if stable_sources is None and not provisional:
            raise ValueError('formal_reference_requires_three_stable_sources')
        if stable_sources is not None:
            if len(stable_sources) != 3:
                raise ValueError('three_real_standard_sources_required')
            keys = [source_key(s) for s in stable_sources]
            if (any(k[:2] != key[:2] for k in keys) or keys[-1] != key
                    or any(keys[i][2] >= keys[i+1][2] or keys[i][3] >= keys[i+1][3]
                           or stable_sources[i]['frame_id'] >= stable_sources[i+1]['frame_id'] for i in range(2))):
                raise ValueError('standard_stable_sources_disagree')
        self.scan_epoch, self.view, self.source = scan_epoch, view, deepcopy(source)
        self.last_key, self.last_frame_id = key, source['frame_id']
        self.reference_rgb_sha256 = digest
        self.standard_stability_sources = deepcopy(stable_sources)
        self.provisional = provisional
        self.reference_exclusion_targets = deepcopy(exclusion_targets or [])
        self.reference_excluded_rectangles = []
        self.search_radius = float(search_radius_px)
        self.sift, self.matcher = cv2.SIFT_create(nfeatures=2500), cv2.BFMatcher(cv2.NORM_L2)
        self.references = []
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        for panel in VIEW_PANELS[view]:
            cells = []
            mask = np.zeros((720, 1280), np.uint8)
            cell_map = np.full((720, 1280), -1, np.int16)
            for original in geometry_result.get('cells', []):
                if original.get('panel') != panel:
                    continue
                row, col = original['local_row'], original['local_col']
                identity = canonical_cell(panel, row, col)
                if any(original.get(k) != v for k, v in identity.items()):
                    raise ValueError('standard_canonical_cell_disagrees')
                quad = np.asarray(original['quad'], np.float32)
                if quad.shape != (4, 2) or not np.isfinite(quad).all() or not cv2.isContourConvex(quad):
                    raise ValueError('actual_standard_quad_required')
                cell = dict(identity, panel=panel, canonical_face=identity['face'],
                            local_row=row, local_col=col, quad=quad.tolist())
                cv2.fillConvexPoly(cell_map, np.rint(quad).astype(np.int32), len(cells))
                cv2.fillConvexPoly(mask, np.rint(quad).astype(np.int32), 255)
                cells.append(cell)
            if len(cells) != 9 or len({(c['row'], c['col']) for c in cells}) != 9:
                raise ValueError('nine_actual_cells_per_visible_face_required')
            mask = cv2.erode(mask, np.ones((3, 3), np.uint8))
            excluded = _exclude_dynamic_targets(mask, exclusion_targets)
            if not self.reference_excluded_rectangles:
                self.reference_excluded_rectangles = excluded
            points, descriptors = self.sift.detectAndCompute(gray, mask)
            ids = [str(int(cell_map[round(k.pt[1]), round(k.pt[0])])) for k in points]
            if (descriptors is None or len(points) < 12 or len(set(ids)) < 3
                    or min(np.ptp(np.asarray([k.pt for k in points]), axis=0)) < 100):
                raise ValueError('standard_actual_feature_coverage_insufficient')
            self.references.append(dict(panel=panel, cells=cells, points=points,
                                        descriptors=descriptors, cell_ids=ids))

    def locate(self, rgb, source, *, scan_epoch=None, exclusion_targets=None):
        started = time.perf_counter()
        try:
            rgb = _rgb(rgb)
            key = source_key(source)
        except (ValueError, TypeError) as exc:
            return _unknown(str(exc), self.view, self.scan_epoch, source)
        if ((scan_epoch is not None and scan_epoch != self.scan_epoch)
                or source.get('scan_epoch', self.scan_epoch) != self.scan_epoch):
            return _unknown('epoch_changed_requires_new_standard', self.view, self.scan_epoch, source)
        if key[:2] != self.last_key[:2]:
            return _unknown('capture_session_or_map_changed', self.view, self.scan_epoch, source)
        if key[2] <= self.last_key[2] or key[3] <= self.last_key[3] or source['frame_id'] <= self.last_frame_id:
            return _unknown('stale_or_repeated_epoch_source', self.view, self.scan_epoch, source)
        self.last_key, self.last_frame_id = key, source['frame_id']
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        mask = np.zeros((720, 1280), np.uint8)
        mask[90:620, 320:960] = 255
        try:
            query_excluded = _exclude_dynamic_targets(mask, exclusion_targets)
        except (ValueError, TypeError) as exc:
            return _unknown(str(exc), self.view, self.scan_epoch, source)
        points, descriptors = self.sift.detectAndCompute(gray, mask)
        results = []
        for ref in self.references:
            eligible = []
            if ref['descriptors'] is not None and descriptors is not None and len(descriptors) >= 2:
                rp = np.asarray([k.pt for k in ref['points']], np.float32)
                qp = np.asarray([k.pt for k in points], np.float32)
                association_mask = (np.max(abs(rp[:, None]-qp[None]), axis=2) <= self.search_radius).astype(np.uint8)
                for pair in self.matcher.knnMatch(ref['descriptors'], descriptors, k=2, mask=association_mask):
                    if len(pair) == 2 and pair[0].distance < .70*pair[1].distance:
                        eligible.append(pair[0])
            unique = {}
            for match in eligible:
                if match.trainIdx not in unique or match.distance < unique[match.trainIdx].distance:
                    unique[match.trainIdx] = match
            matches = list(unique.values())
            fit = _fit([ref['points'][m.queryIdx].pt for m in matches],
                       [points[m.trainIdx].pt for m in matches],
                       [ref['cell_ids'][m.queryIdx] for m in matches])
            for index, match in enumerate(matches):
                if index < len(fit['matched_pairs']):
                    fit['matched_pairs'][index].update(reference_index=match.queryIdx,
                        query_index=match.trainIdx, descriptor_distance=float(match.distance))
            cells, failures = [], []
            if fit['homography'] is not None:
                for original in ref['cells']:
                    quad = project_points(original['quad'], fit['homography'])
                    if (not np.isfinite(quad).all() or not cv2.isContourConvex(quad.astype(np.float32))
                            or cv2.contourArea(quad.astype(np.float32)) < 500):
                        failures.append('nonfinite_nonconvex_or_small_quad')
                    elif (quad[:, 0].min() < 330 or quad[:, 0].max() > 950
                          or quad[:, 1].min() < 95 or quad[:, 1].max() > 605):
                        failures.append('outside_cube_roi')
                    if cv2.pointPolygonTest(quad.astype(np.float32), (640., 530.), False) >= 0:
                        failures.append('overlaps_reset_hud')
                    cells.append(dict(original, quad=quad.tolist(), center=quad.mean(axis=0).tolist()))
            fit['projection_failures'] = failures
            fit['proposal_accepted'] = bool(fit['proposal_accepted'] and not failures)
            fit.update(view=self.view, reference_panel=ref['panel'],
                       reference_rgb_sha256=self.reference_rgb_sha256,
                       cells=cells if fit['proposal_accepted'] else [],
                       reference_panel_center_px=np.mean(np.asarray([c['quad'] for c in ref['cells']]), axis=(0, 1)).tolist(),
                       reference_panel_span_y_px=float(np.ptp(np.asarray([c['quad'] for c in ref['cells']])[:, :, 1])),
                       reference_feature_count=len(ref['points']))
            results.append(fit)
        accepted = bool(results and all(p['proposal_accepted'] for p in results))
        for panel in results:
            panel['cells'] = attach_current_surface_normals(rgb, panel['cells'])
        result = dict(status='actual_epoch_registration_supported' if accepted else 'unknown',
                      proposal_accepted=accepted, results=results,
                      cells=[c for p in results for c in p['cells']] if accepted else [],
                      expected_view=self.view, scan_epoch=self.scan_epoch, source=deepcopy(source),
                      source_rgb_sha256=hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest(),
                      reference_rgb_sha256=self.reference_rgb_sha256,
                      reference_source=deepcopy(self.source), query_feature_count=len(points),
                      standard_stability_checked_by_caller=self.standard_stability_sources is None,
                      standard_stability_sources=deepcopy(self.standard_stability_sources),
                      provisional_reference=self.provisional,
                      formal_reference=not self.provisional,
                      reference_status='provisional_not_formal_standard' if self.provisional else 'three_source_formal_reference',
                      independent_view_evidence=False,
                      reference_excluded_rectangles=deepcopy(self.reference_excluded_rectangles),
                      query_excluded_rectangles=query_excluded,
                      identity_from_geometry=False, formal_pose=False,
                      frame_convention=FRAME_CONVENTION, elapsed_sec=time.perf_counter()-started)
        result['feedback'] = epoch_orientation_feedback(result)
        return result

    @staticmethod
    def feedback(result):
        return epoch_orientation_feedback(result)
