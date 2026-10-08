"""Structural four-view geometry, independent of glyph labels and image texture.

The route caller supplies the ordered view identity after a real reset. Geometry
does not infer an identity from visually symmetric cube faces. Reference quads
are fixed glyph-bearing panel outlines; actual image line segments must support
their per-face projection before any quads are returned.
"""
from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path

import cv2
import numpy as np

from ._deep_dive_planner_rules import BASES, FACES


PANEL_FACES = {"view1left": "F", "view1right": "R", "view2main": "D",
               "view3left": "L", "view3right": "B", "view4main": "U"}
VIEW_PANELS = {1: ("view1left", "view1right"), 2: ("view2main",),
               3: ("view3left", "view3right"), 4: ("view4main",)}
FRAME_CONVENTION = "reset_player_U_first_pair_F_R_vertical_v1"


def canonical_cell(panel: str, row: int, col: int) -> dict:
    """Map screenshot-local rows/columns to the planner's explicit BASES."""
    if panel not in PANEL_FACES or type(row) is not int or type(col) is not int or not (0 <= row < 3 and 0 <= col < 3):
        raise ValueError("invalid_four_view_cell")
    if panel == "view2main":
        row, col = col, 2-row
    elif panel in ("view3left", "view3right"):
        row, col = 2-row, 2-col
    face = PANEL_FACES[panel]
    return dict(face=face, row=row, col=col, slot=FACES.index(face)*9+row*3+col)


def local_cell(face: str, row: int, col: int) -> dict:
    panel = next((p for p, f in PANEL_FACES.items() if f == face), None)
    if panel is None or type(row) is not int or type(col) is not int or not (0 <= row < 3 and 0 <= col < 3):
        raise ValueError("invalid_canonical_cell")
    if panel == "view2main":
        row, col = 2-col, row
    elif panel in ("view3left", "view3right"):
        row, col = 2-row, 2-col
    return dict(panel=panel, row=row, col=col,
                view=next(v for v, panels in VIEW_PANELS.items() if panel in panels))


def project_points(points, homography):
    points = np.asarray(points, np.float64)
    h = np.asarray(homography, np.float64)
    result = np.c_[points.reshape(-1, 2), np.ones(points.size//2)] @ h.T
    if np.any(abs(result[:, 2]) < 1e-9):
        raise ValueError("projection_pole")
    return (result[:, :2]/result[:, 2:]).reshape(points.shape)


def _segments(rgb):
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.shape != (720, 1280, 3):
        raise ValueError("expected_1280x720_rgb")
    gray = cv2.cvtColor(rgb[90:620, 320:960], cv2.COLOR_RGB2GRAY)
    found = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(gray)[0]
    return np.empty((0, 4)) if found is None else found.reshape(-1, 4).astype(float)+[320, 90, 320, 90]


def _choose_segment(segments, expected, radius):
    if not len(segments):
        return None
    p, q = np.asarray(expected, float)
    length = np.linalg.norm(q-p)
    u = (q-p)/length
    normal = np.array([-u[1], u[0]])
    a, b = segments[:, :2], segments[:, 2:]
    sizes = np.linalg.norm(b-a, axis=1)
    cosine = abs((b-a)@u)/np.maximum(sizes, 1e-9)
    along = np.column_stack(((a-p)@u, (b-p)@u))
    overlap = np.maximum(0, np.minimum(length, along.max(axis=1))-np.maximum(0, along.min(axis=1)))/length
    offsets = ((a+b)/2-(p+q)/2)@normal
    good = (sizes >= 22) & (cosine >= math.cos(math.radians(12))) & (overlap >= .30) & (abs(offsets) <= radius)
    indices = np.flatnonzero(good)
    if not len(indices):
        return None
    # Glyph-internal short lines receive no special treatment. Structural support
    # must span the face and both directions, rather than a single matching tile.
    score = abs(offsets[indices])+4*(1-overlap[indices])+12*(1-cosine[indices])
    i = indices[np.argmin(score)]
    tangent = (b[i]-a[i])/sizes[i]
    n = np.array([-tangent[1], tangent[0]])
    return dict(segment=segments[i].tolist(), normal=n.tolist(), offset=float(n@a[i]),
                overlap=float(overlap[i]), search_offset_px=float(offsets[i]))


def _affine(values):
    # Optimization centered/scaled to avoid a poorly conditioned 1280px system.
    a = np.array([[values[0], values[1], values[4]], [values[2], values[3], values[5]], [0, 0, 1.]])
    norm = np.array([[1/300, 0, -640/300], [0, 1/300, -360/300], [0, 0, 1.]])
    return np.linalg.inv(norm)@a@norm


def _fit_panel_once(segments, panel, radius, initial_translation=(0., 0.), initial_shear_deg=0., initial_h=None, center_pairs=None):
    center_x = np.mean([np.mean(c['quad'], axis=0)[0] for c in panel['cells']])
    shear = math.tan(math.radians(initial_shear_deg))
    values = np.array([1., 0, shear, 1., initial_translation[0]/300,
                       (initial_translation[1]+shear*(640-center_x))/300])
    if initial_h is not None:
        normalize = np.array([[1/300, 0, -640/300], [0, 1/300, -360/300], [0, 0, 1.]])
        local = normalize@initial_h@np.linalg.inv(normalize)
        values = np.array([local[0, 0], local[0, 1], local[1, 0], local[1, 1], local[0, 2], local[1, 2]])
    matches = []
    for _ in range(5):
        h = _affine(values)
        matches = []
        for anchor in panel['anchors']:
            expected = project_points(anchor['endpoints'], h)
            selected = _choose_segment(segments, expected, radius)
            if selected:
                matches.append(dict(anchor=anchor, selected=selected))
        if len(matches) < 4:
            break
        points = np.asarray([m['anchor']['endpoints'] for m in matches], float)
        normals = np.asarray([m['selected']['normal'] for m in matches])
        offsets = np.asarray([m['selected']['offset'] for m in matches])
        normalized = (points-[640., 360.])/300.
        nx, ny = normalized[:, :, 0], normalized[:, :, 1]
        ax, ay = normals[:, 0, None], normals[:, 1, None]
        design = np.stack((ax*nx, ax*ny, ay*nx, ay*ny,
                           np.broadcast_to(ax, nx.shape), np.broadcast_to(ay, nx.shape)), axis=-1).reshape(-1, 6)
        target = np.repeat((offsets-normals@np.array([640., 360.]))/300., 2)
        row_strength = np.ones(len(target))
        if center_pairs is not None:
            reference_centers, actual_centers = center_pairs
            normalized_centers = (np.asarray(reference_centers)-[640., 360.])/300.
            cx, cy = normalized_centers[:, 0], normalized_centers[:, 1]
            zeros, ones = np.zeros(len(cx)), np.ones(len(cx))
            x_equations = np.c_[cx, cy, zeros, zeros, ones, zeros]
            y_equations = np.c_[zeros, zeros, cx, cy, zeros, ones]
            center_design = np.stack((x_equations, y_equations), axis=1).reshape(-1, 6)
            center_target = ((np.asarray(actual_centers)-[640., 360.])/300.).ravel()
            design = np.vstack((design, center_design))
            target = np.r_[target, center_target]
            row_strength = np.r_[row_strength, np.full(len(center_target), .6)]
        if np.linalg.matrix_rank(design, tol=1e-5) < 6:
            break
        for _ in range(4):
            residual = (design@values-target)*300.
            weight = row_strength/np.sqrt(1+(residual/2.)**2)
            candidate = np.linalg.lstsq(design*weight[:, None], target*weight, rcond=None)[0]
            if np.linalg.norm(_affine(candidate)[:2, :2]-np.eye(2)) > .35:
                break
            values = candidate
    h = _affine(values)
    errors, pairs = [], []
    for match in matches:
        anchor, selected = match['anchor'], match['selected']
        reference = np.asarray(anchor['endpoints'], float)
        predicted = project_points(reference, h)
        normal = np.asarray(selected['normal'])
        distances = predicted@normal-selected['offset']
        actual = predicted-distances[:, None]*normal
        error = float(np.max(abs(distances)))
        match.update(residual_px=error, inlier=error <= 3.)
        if error <= 3.:
            errors.append(error)
        for i in range(2):
            pairs.append(dict(reference_point=reference[i].tolist(), query_point=actual[i].tolist(),
                              reference_cell=anchor.get('cell', anchor['id']), inlier=error <= 3.,
                              residual_px=float(abs(distances[i])), structural_line_id=anchor['id'],
                              actual_segment=selected['segment'],
                              correspondence_kind='projection_onto_observed_structural_line_not_texture_keypoint'))
    inliers = [m for m in matches if m['inlier']]
    span = np.ptp(np.asarray([m['selected']['segment'] for m in inliers]).reshape(-1, 2), axis=0) if inliers else np.zeros(2)
    directions = [np.asarray(m['selected']['normal']) for m in inliers]
    direction_rank = np.linalg.matrix_rank(np.asarray(directions), tol=.1) if directions else 0
    needed = 4 if len(panel['anchors']) <= 7 else 7
    valid = bool(len(inliers) >= needed and min(span) >= 100 and direction_rank == 2
                 and np.linalg.det(h[:2, :2]) > .5 and np.linalg.det(h[:2, :2]) < 1.6
                 and np.linalg.norm(h[:2, :2]-np.eye(2)) < .35
                 and len({m['anchor']['region'] for m in inliers}) >= 2)
    cells, failures = [], []
    for cell in panel['cells']:
        quad = project_points(cell['quad'], h)
        name = f"{cell['row']},{cell['col']}"
        if not cv2.isContourConvex(quad.astype(np.float32)) or cv2.contourArea(quad.astype(np.float32)) < 500:
            failures.append(name+':nonconvex_or_small')
        if not (quad[:, 0].min() >= 330 and quad[:, 0].max() <= 950 and quad[:, 1].min() >= 95 and quad[:, 1].max() <= 605):
            failures.append(name+':outside_cube_roi')
        if cv2.pointPolygonTest(quad.astype(np.float32), (640., 530.), False) >= 0:
            failures.append(name+':overlaps_reset_hud')
        cells.append(dict(panel=panel['panel'], local_row=cell['row'], local_col=cell['col'],
                          **canonical_cell(panel['panel'], cell['row'], cell['col']), quad=quad.tolist(),
                          center=np.mean(quad, axis=0).tolist()))
    return dict(view=panel['view'], reference_panel=panel['panel'],
                proposal_accepted=valid and not failures, structural_support_valid=valid,
                homography=h.tolist(), matched_pairs=pairs, structural_lines=matches,
                actual_inliers=len(inliers)*2, support_span_px=span.tolist(),
                projection_failures=failures, cells=cells if valid and not failures else [],
                reference_panel_center_px=np.mean(np.asarray([c['quad'] for c in panel['cells']]), axis=(0, 1)).tolist(),
                reference_panel_span_y_px=float(np.ptp(np.asarray([c['quad'] for c in panel['cells']])[:, :, 1])),
                reason='actual_structure_supported' if valid and not failures else 'insufficient_or_invalid_actual_structure')


def _complete_bright_centers(vivid):
    closed = cv2.morphologyEx(vivid.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    centers = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if not (18 <= w <= 105 and 25 <= h <= 115 and area >= 100 and area/(w*h) >= .08):
            continue
        if not (330 <= x and x+w <= 950 and 95 <= y and y+h <= 605):
            continue
        moments = cv2.moments(contour)
        if moments['m00'] <= 0:
            continue
        centers.append(dict(point=[moments['m10']/moments['m00'], moments['m01']/moments['m00']],
                            actual_component_box=[x, y, w, h], component_area=area))
    return centers


def _bright_panel_center_evidence(vivid, result, complete_centers=None):
    """Nonsemantic, actual high-contrast panel-center consistency diagnostic.

    All hues and white empty outlines are treated equally. No icon class,
    template, expected board content, or classification result is consumed.
    This evidence only rejects competing structural registrations; actual line
    support remains mandatory.
    """
    observations = []
    for cell in result['cells']:
        quad = np.asarray(cell['quad'], float)
        center = quad.mean(axis=0)
        # The actual glyph-bearing panels are outlined separately from the
        # adjoining colored sidewall, so their interior may supply centering.
        interior = center+(quad-center)*.85
        low = np.maximum(0, np.floor(interior.min(axis=0)).astype(int))
        high = np.minimum([1280, 720], np.ceil(interior.max(axis=0)+1).astype(int))
        if np.any(high-low <= 0):
            continue
        mask = np.zeros((high[1]-low[1], high[0]-low[0]), np.uint8)
        cv2.fillConvexPoly(mask, np.rint(interior-low).astype(np.int32), 1)
        ys, xs = np.nonzero(vivid[low[1]:high[1], low[0]:high[0]] & (mask > 0))
        area = cv2.contourArea(interior.astype(np.float32))
        if len(xs) < 90 or len(xs) > .55*area:
            continue
        point = np.array([np.mean(xs), np.mean(ys)])+low
        width = min(np.linalg.norm(quad[1]-quad[0]), np.linalg.norm(quad[3]-quad[0]))
        if complete_centers is not None:
            candidates = [(np.linalg.norm(np.asarray(c['point'])-center), c) for c in complete_centers]
            if not candidates:
                continue
            distance, complete = min(candidates, key=lambda x:x[0])
            if distance > width*.65:
                continue
            point = np.asarray(complete['point'])
        observations.append(dict(local_row=cell['local_row'], local_col=cell['local_col'],
                                 actual_bright_center=point.tolist(), actual_mask_pixels=len(xs),
                                 panel_center=center.tolist(), offset_px=float(np.linalg.norm(point-center)),
                                 offset_panel_units=float(np.linalg.norm(point-center)/width)))
    return observations


def _fit_panel_candidates(segments, panel, radius, vivid, complete_centers, *, extra_shear=False):
    results = [_fit_panel_once(segments, panel, radius, offset)
               for offset in ((0., 0.), (0., -16.), (0., 16.), (-12., 0.), (12., 0.),
                              (-12., -16.), (12., -16.), (-12., 16.), (12., 16.))]
    if extra_shear:
        results.extend(_fit_panel_once(segments, panel, radius, offset, shear)
                       for shear in (-8., 8.) for offset in ((0., 0.), (0., -16.), (0., 16.), (-12., 0.), (12., 0.)))
    references, actual, used = [], [], set()
    for cell in panel['cells']:
        center = np.mean(cell['quad'], axis=0)
        available = [(np.linalg.norm(np.asarray(c['point'])-center), i, c) for i, c in enumerate(complete_centers)
                     if i not in used and abs(c['point'][0]-center[0]) <= 22 and abs(c['point'][1]-center[1]) <= 55]
        if not available:
            continue
        _, index, observation = min(available, key=lambda x:x[0])
        used.add(index);references.append(center);actual.append(observation['point'])
    if len(references) >= 6 and min(np.ptp(np.asarray(references), axis=0)) >= 100:
        affine, inliers = cv2.estimateAffine2D(np.asarray(references), np.asarray(actual),
                                              method=cv2.RANSAC, ransacReprojThreshold=6., confidence=.995)
        if affine is not None and np.count_nonzero(inliers) >= 6:
            selected = inliers.ravel() > 0
            center_pairs = (np.asarray(references)[selected], np.asarray(actual)[selected])
            candidate = _fit_panel_once(segments, panel, radius, initial_h=np.vstack((affine, [0, 0, 1.])),
                                        center_pairs=center_pairs)
            candidate['automatic_center_initialization'] = dict(
                method='complete_unclassified_components_to_geometric_panel_centers',
                actual_points=actual, reference_geometric_centers=np.asarray(references).tolist(),
                inlier_count=int(np.count_nonzero(inliers)), coordinate_window_px=[22, 55],
                glyph_class_or_texture_consumed=False)
            results.append(candidate)
    def quality(result):
        matches = [m for m in result['structural_lines'] if m['inlier']]
        # Structural consensus uses actual line support across the complete face,
        # never the expected glyph class or the success of an icon classifier.
        coverage = len({m['anchor'].get('cell') for m in matches if m['anchor'].get('cell')})
        residual = np.median([m['residual_px'] for m in matches]) if matches else 100.
        centers = _bright_panel_center_evidence(vivid, result, complete_centers)
        result['actual_panel_center_evidence'] = centers
        centering = np.median([p['offset_panel_units'] for p in centers]) if len(centers) >= 5 else 10.
        result['selection_quality'] = dict(centering_panel_units=float(centering),
            complete_bright_center_count=len(centers), structural_cell_coverage=coverage,
            actual_supported_lines=len(matches), median_line_residual_px=float(residual))
        return (result['structural_support_valid'], -float(centering), coverage, len(matches), -float(residual))
    return sorted(results, key=quality, reverse=True)


def _paired_vertical_family_selection(left, right):
    candidates = []
    for a in left:
        if not a['proposal_accepted']:
            continue
        ha = np.asarray(a['homography'])
        tilt_a = math.degrees(math.atan2(ha[1, 0], ha[0, 0]))
        for b in right:
            if not b['proposal_accepted']:
                continue
            hb = np.asarray(b['homography'])
            tilt_b = math.degrees(math.atan2(hb[1, 0], hb[0, 0]))
            off_axis = (tilt_a+tilt_b)/2.
            if abs(off_axis) > 1.5:
                continue
            qa, qb = a['selection_quality'], b['selection_quality']
            centering = qa['centering_panel_units']+qb['centering_panel_units']
            support = qa['actual_supported_lines']+qb['actual_supported_lines']
            candidates.append((centering, -support, abs(off_axis), a, b))
    if candidates:
        best = min(candidates, key=lambda x:x[:3])
        result = [best[3], best[4]]
        for p in result:
            p['paired_motion_family_evidence'] = dict(method='joint_selection_of_independently_fitted_face_candidates',
                maximum_off_axis_deg=1.5, selected_off_axis_deg=best[2], homography_borrowed=False)
        return result
    results = [left[0], right[0]]
    for p in results:
        p['proposal_accepted'] = False
        p['structural_support_valid'] = False
        p['cells'] = []
        p['reason'] = 'no_independently_supported_paired_vertical_family'
    return results


def _normal_projections(cell, segments):
    """Keep planar branches and require actual adjoining side-wall witnesses.

    The projection is approximate: the outlined icon-bearing panel is assumed
    square of unit glyph-panel width. It is not a measured 6DoF
    cube pose, and is never used to certify physical view separation.
    """
    quad = np.asarray(cell['quad'], np.float64)
    camera = np.array([[360/math.tan(math.radians(6)), 0, 640.],
                       [0, 360/math.tan(math.radians(6)), 360.], [0, 0, 1.]])
    square = np.array([[-.5, -.5, 0], [.5, -.5, 0], [.5, .5, 0], [-.5, .5, 0]])
    fit = cv2.solvePnPGeneric(square, quad, camera, None, flags=cv2.SOLVEPNP_IPPE)
    branches = []
    for rotation, translation, residual in zip(fit[1], fit[2], fit[3]):
        r = cv2.Rodrigues(rotation)[0]
        depth = float(translation[2, 0])
        if depth <= 0 or not np.isfinite(r).all():
            continue
        normal = -r[:, 2]
        branches.append(dict(normal_camera=normal.tolist(),
                             normal_projection=(camera[0, 0]*normal[:2]/depth).tolist(),
                             reprojection_rms_px=float(residual.ravel()[0]), depth=depth))
    panel = cell['panel']
    if 'left' in panel:
        body_direction, corners = np.array([1., 0]), (1, 2)
    elif 'right' in panel:
        body_direction, corners = np.array([-1., 0]), (0, 3)
    else:
        body_direction, corners = np.array([0., 1]), (1, 2, 3)
    witnesses = []
    for a, b in segments.reshape(-1, 2, 2):
        vector = b-a
        length = np.linalg.norm(vector)
        if length < 20 or abs(vector@body_direction)/length < math.cos(math.radians(18)):
            continue
        # A visible strip must begin beside the observed panel and continue
        # away into the body; glyph-internal segments inside the quad cannot
        # disambiguate a floating marker's surface normal.
        for corner in corners:
            point = quad[corner]
            offsets = np.array([a-point, b-point])
            along = offsets@body_direction
            sideways = abs(offsets@np.array([-body_direction[1], body_direction[0]]))
            if min(abs(along)) <= 18 and max(along) >= 22 and max(sideways) <= 14 and min(along) >= -14:
                witnesses.append(dict(actual_segment=np.r_[a, b].tolist(), corner=corner,
                                      body_direction=body_direction.tolist()))
                break
    chosen = [b for b in branches if np.asarray(b['normal_projection'])@body_direction < -10]
    evidence = dict(status='pending_planar_branch_ambiguity',
                    method='IPPE_actual_quad_ordered_standard_surface_approximate_square_panel',
                    approximate_square_panel=True, square_width_panel_units=1.,
                    projection_unit='one_glyph_panel_width',
                    camera_fov_y_deg=12., physical_pose_verified=False,
                    actual_sidewall_witnesses=witnesses, planar_branch_count=len(branches))
    projection = None
    if witnesses and len(chosen) == 1:
        projection = chosen[0]['normal_projection']
        evidence['status'] = 'actual_sidewall_disambiguated_approximate_projection'
        evidence['selection'] = 'outward_opposes_actual_adjoining_body_extension'
    return projection, evidence, branches


def attach_current_surface_normals(rgb, cells):
    """Recompute each observed surface's approximate normals on this RGB.

    Used by both structural localization and same-epoch image registration.
    Old-frame normal projections are never carried through a homography.
    """
    segments = _segments(rgb)
    digest = hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest()
    output = []
    for original in cells:
        cell = dict(original)
        projection, evidence, branches = _normal_projections(cell, segments)
        cell.update(normal_projection=projection, normal_evidence=evidence,
                    normal_projection_branches=branches)
        cell['normal_evidence']['source_rgb_sha256'] = digest
        output.append(cell)
    for panel in {c['panel'] for c in output}:
        current = [c for c in output if c['panel'] == panel]
        witnesses = [dict(w, witness_cell=[c['local_row'], c['local_col']]) for c in current
                     for w in c['normal_evidence']['actual_sidewall_witnesses']]
        covered = {tuple(w['witness_cell']) for w in witnesses}
        if len(covered) < 2:
            continue
        body_direction = np.asarray(witnesses[0]['body_direction'])
        for cell in current:
            if cell['normal_projection'] is not None:
                continue
            candidates = [b for b in cell['normal_projection_branches']
                          if np.asarray(b['normal_projection'])@body_direction < -10]
            if len(candidates) == 1:
                cell['normal_projection'] = candidates[0]['normal_projection']
                cell['normal_evidence'].update(status='same_observed_face_sidewalls_disambiguated_approximate_projection',
                    selection='outward_opposes_actual_adjoining_body_extension',
                    actual_same_face_sidewall_witnesses=witnesses, sidewall_witness_cells=len(covered))
    return output


class FourViewGeometry:
    """Local standard-pose registration, with ordered identity supplied by caller.

    Symmetric main faces can register to both view 2 and view 4. Therefore
    ``arrival_candidate`` is a shape residual, never evidence that the requested
    logical face was reached. Real reset and continuous ordered motion belong
    to the controller. This API returns no hidden-face quad.
    """
    def __init__(self, data_path=None, *, search_radius_px=18.):
        if not 4 <= search_radius_px <= 32:
            raise ValueError('invalid_structural_search_radius')
        if data_path is None:
            data_path = Path(__file__).parents[2]/'data'/'four_views_geometry.json'
        self.data = json.loads(Path(data_path).read_text(encoding='utf-8'))
        self.panels = self.data['panels']
        self.search_radius = float(search_radius_px)
        if {p['panel'] for p in self.panels} != set(PANEL_FACES):
            raise ValueError('incomplete_six_face_geometry')
        for panel in self.panels:
            if len(panel['cells']) != 9 or any(set(c) != {'row', 'col', 'quad'} for c in panel['cells']):
                raise ValueError('geometry_must_not_contain_semantic_labels')

    def locate(self, rgb, *, expected_view):
        if expected_view not in VIEW_PANELS:
            raise ValueError('invalid_expected_view')
        segments = _segments(rgb)
        source_rgb_sha256 = hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest()
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        vivid = ((hsv[:, :, 1] >= 130) & (hsv[:, :, 2] >= 165)) | ((hsv[:, :, 1] < 65) & (hsv[:, :, 2] >= 180))
        complete_centers = _complete_bright_centers(vivid)
        choices = [_fit_panel_candidates(segments, p, self.search_radius, vivid, complete_centers)
                   for p in self.panels if p['view'] == expected_view]
        if len(choices) == 2:
            probe = _paired_vertical_family_selection(*choices)
            poor = any(not p['proposal_accepted'] or p.get('selection_quality', {}).get('centering_panel_units', 10) > .23
                       for p in probe)
            if poor:
                choices = [_fit_panel_candidates(segments, p, self.search_radius, vivid, complete_centers, extra_shear=True)
                           for p in self.panels if p['view'] == expected_view]
        results = _paired_vertical_family_selection(*choices) if len(choices) == 2 else [choices[0][0]]
        accepted = bool(results and all(p['proposal_accepted'] for p in results))
        for result in results:
            result['cells'] = attach_current_surface_normals(rgb, result['cells'])
            for cell in result['cells']:
                cell['canonical_face'] = cell['face']
        return dict(status='actual_structure_supported' if accepted else 'unknown',
                    proposal_accepted=accepted, query_feature_count=len(segments), results=results,
                    cells=[c for p in results for c in p['cells']] if accepted else [],
                    expected_view=expected_view, identity_from_geometry=False,
                    phase_ownership_validated_by_caller=False,
                    source_rgb_sha256=source_rgb_sha256, source_binding_checked_by_caller=False,
                    frame_convention=FRAME_CONVENTION, formal_pose=False)


def orientation_feedback(alignment_result, *, expected_view, main_tolerance_deg=.6,
                         paired_tolerance_deg=.8, off_axis_tolerance_deg=1.5):
    """Paired-axis tilt or main vertical-displacement bearing, never 3D pitch.

    Main bearing uses the current detected panel center relative to the standard
    center, normalized by standard panel height. Unlike a scale ratio it has
    opposite signs on both sides of the requested standard orientation.
    """
    panels = {p['reference_panel']: p for p in alignment_result.get('results', [])}
    invalid = dict(valid_diagnostic=False, signed_error_deg=None, arrival_candidate=False,
                   direction_up_error_sign=-1, formal_pose=False, phase_identity=False)
    if expected_view not in VIEW_PANELS or any(p not in panels or not panels[p]['structural_support_valid'] for p in VIEW_PANELS[expected_view]):
        return dict(invalid, reason='actual_structure_not_supported')
    metrics = {}
    for name in VIEW_PANELS[expected_view]:
        h = np.asarray(panels[name]['homography'], float)
        horizontal = math.degrees(math.atan2(h[1, 0], h[0, 0]))
        shape = math.degrees(math.atan2(np.linalg.norm(h[:2, 1]), np.linalg.norm(h[:2, 0])))-45
        roll = math.degrees(math.atan2(h[1, 0]-h[0, 1], h[0, 0]+h[1, 1]))
        metrics[name] = dict(horizontal_axis_delta_deg=horizontal, vertical_horizontal_shape_deg=shape, polar_roll_deg=roll)
    if expected_view in (1, 3):
        left, right = [metrics[p]['horizontal_axis_delta_deg'] for p in VIEW_PANELS[expected_view]]
        signed, off_axis, tolerance = (left-right)/2, (left+right)/2, paired_tolerance_deg
    else:
        metric = metrics[VIEW_PANELS[expected_view][0]]
        panel = panels[VIEW_PANELS[expected_view][0]]
        quality = panel.get('selection_quality', {})
        actual_centers = np.asarray([c['center'] for c in panel.get('cells', [])], float)
        reference_center = np.asarray(panel.get('reference_panel_center_px'), float)
        span = panel.get('reference_panel_span_y_px', 0)
        if (quality.get('complete_bright_center_count', 0) < 5
                or quality.get('centering_panel_units', 10) > .23
                or actual_centers.shape != (9, 2) or reference_center.shape != (2,) or span < 100):
            return dict(invalid, reason='main_actual_complete_center_support_required')
        delta_y = float(np.mean(actual_centers[:, 1])-reference_center[1])
        signed = math.degrees(math.atan2(delta_y, span))
        off_axis, tolerance = metric['polar_roll_deg'], main_tolerance_deg
        metric.update(actual_panel_center_y_px=float(np.mean(actual_centers[:, 1])),
                      reference_panel_center_y_px=float(reference_center[1]),
                      reference_panel_span_y_px=span, actual_center_delta_y_px=delta_y,
                      method='actual_panel_vertical_displacement_bearing_not_even_shape_ratio')
    arrival = alignment_result.get('proposal_accepted', False) and abs(signed) <= tolerance and abs(off_axis) <= off_axis_tolerance_deg
    center_support = all(panels[p].get('selection_quality', {}).get('complete_bright_center_count', 0) >= 5
                         and panels[p].get('selection_quality', {}).get('centering_panel_units', 10) <= .23
                         for p in VIEW_PANELS[expected_view])
    if not center_support:
        return dict(invalid, reason='paired_actual_complete_center_support_required',
                    diagnostic_signed_error_deg=float(signed),
                    metrics=dict(unit='image_angular_shape_degrees_not_physical_scan_angle',
                                 panels=metrics, off_axis_deg=float(off_axis),
                                 actual_complete_center_support=False,
                                 maximum_center_residual_panel_units=.23))
    arrival = bool(arrival and center_support)
    return dict(valid_diagnostic=abs(off_axis) <= 3., signed_error_deg=signed,
                arrival_candidate=bool(arrival), direction_up_error_sign=-1,
                reason='requires_stop_and_fresh_recheck' if arrival else 'outside_structural_tolerance',
                metrics=dict(unit=('image_vertical_displacement_bearing_degrees_not_physical_scan_angle'
                                   if expected_view in (2, 4) else 'image_angular_shape_degrees_not_physical_scan_angle'), panels=metrics,
                             off_axis_deg=off_axis, arrival_tolerance_deg=tolerance,
                             actual_complete_center_support=center_support,
                             maximum_center_residual_panel_units=.23),
                formal_pose=False, phase_identity=False)
