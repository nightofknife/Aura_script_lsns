"""Conservative floating-entity ownership using real geometry and RGB clues.

Detection centre is never a contact point. Caller supplies camera-derived
surface-normal projections, including hidden faces when observable; unsupported
side peeks remain unresolved. Temporal frames retain their one actual view group.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import math

import cv2
import numpy as np

from ._deep_dive_four_view_reader import source_key
from ._deep_dive_planner_rules import cell_to_slot

KINDS = ('player', 'singularity', 'inspiration')
# Broad geometric priors, expressed in panel-width units, not screen pixels.
# They establish candidate rays, never substitute for a second observed view.
LIFT_RANGES = dict(singularity=(.35, 1.65), inspiration=(.08, .85), player_head=(.20, 1.70))


class VerticalTargetTracker:
    """Measured consecutive trajectory identity, without guessing hidden slots.

    Source changes invalidate the tracking epoch. Long gaps start an unlinked
    segment with a new ID; competing tracks stay unassigned. Category uniqueness
    is never a matching rule.
    """
    def __init__(self, *, max_gap_sec=4.0):
        self.max_gap_sec = float(max_gap_sec)
        self._tracks = {}
        self._next_id = 1
        self._last_source = None
        self._blocked = False

    def update(self, targets, source, *, vertical_verified=True):
        key = source_key(source)
        if self._last_source is not None and key[:2] != self._last_source[:2]:
            self._tracks.clear()
            self._blocked = False
        if self._last_source is not None and key[:2] == self._last_source[:2] and (
                key[2] <= self._last_source[2] or key[3] <= self._last_source[3]):
            return [dict(t, track_id=None, tracking_evidence=dict(reason='stale_capture')) for t in targets]
        if not vertical_verified:
            self._tracks.clear()
            self._blocked = True
        self._last_source = key
        if self._blocked:
            return [dict(t, track_id=None, tracking_evidence=dict(reason='unverified_rotation_epoch')) for t in targets]
        packets = [dict(t, track_id=None) for t in targets]
        duplicates = set()
        for i, first in enumerate(targets):
            for j, second in enumerate(targets[:i]):
                if first.get('kind') != second.get('kind'):
                    continue
                a, b = np.asarray(first.get('point'), float), np.asarray(second.get('point'), float)
                if a.shape == b.shape == (2,) and np.linalg.norm(a-b) < 20:
                    duplicates.update((i, j))
        links = []
        for index, target in enumerate(targets):
            point = np.asarray(target.get('point'), float)
            box = np.asarray(target.get('box'), float)
            if (target.get('kind') not in KINDS or point.shape != (2,) or box.shape != (4,)
                    or not np.isfinite(point).all() or not np.isfinite(box).all()):
                packets[index]['tracking_evidence'] = dict(reason='invalid_target_geometry')
                continue
            if index in duplicates:
                for track in self._tracks.values():
                    if track['kind'] == target.get('kind') and np.linalg.norm(point-track['point']) < 80:
                        track['ambiguous'] = True
                packets[index]['tracking_evidence'] = dict(reason='same_kind_duplicate_detection')
                continue
            if target.get('confirmable') is not True and target.get('confidence', 0) < .25:
                packets[index]['tracking_evidence'] = dict(reason='irrelevant_weak_detection')
                continue
            candidates = []
            expired_ids = []
            active_ambiguity = False
            for track_id, track in self._tracks.items():
                if track['kind'] != target['kind']:
                    continue
                dt = key[3]-track['time']
                if track.get('ambiguous'):
                    if dt <= self.max_gap_sec:
                        active_ambiguity = True
                    else:
                        expired_ids.append(track_id)
                    continue
                if dt > self.max_gap_sec:
                    expired_ids.append(track_id)
                    continue
                delta = point-track['point']
                width = max(box[2], track['box'][2], 25)
                height = max(box[3], track['box'][3], 25)
                if abs(delta[0]) > max(40., width*.9) or abs(delta[1]) > max(120., height*3.):
                    continue
                size_change = abs(math.log(max(box[2]*box[3], 1)/max(track['box'][2]*track['box'][3], 1)))
                if size_change > 1.8:
                    continue
                prediction = track['point']+track.get('velocity', np.zeros(2))*min(dt, 1.)
                error = point-prediction
                cost = abs(error[0])/30+abs(error[1])/120+size_change*.35
                candidates.append((float(cost), track_id))
            candidates.sort()
            if len(candidates) > 1 and candidates[1][0]-candidates[0][0] < .5:
                for _, track_id in candidates:
                    self._tracks[track_id]['ambiguous'] = True
                packets[index]['tracking_evidence'] = dict(reason='ambiguous_same_kind_trajectory', candidates=candidates)
                continue
            if not candidates:
                if active_ambiguity:
                    packets[index]['tracking_evidence'] = dict(reason='active_competition_requires_new_identity_evidence')
                    continue
                links.append((index, None, point, box, candidates, expired_ids))
            else:
                links.append((index, candidates[0][1], point, box, candidates, []))
        claimed = defaultdict(list)
        for link in links:
            if link[1] is not None:
                claimed[link[1]].append(link[0])
        for index, track_id, point, box, candidates, expired_ids in links:
            if track_id is not None and len(claimed[track_id]) > 1:
                self._tracks[track_id]['ambiguous'] = True
                packets[index]['tracking_evidence'] = dict(reason='competing_detections_for_track', candidates=candidates)
                continue
            if track_id is None:
                track_id = self._next_id
                self._next_id += 1
                history = []
                velocity = np.zeros(2)
                segment_start_reason = 'new_segment_after_observation_gap' if expired_ids else 'new_visible_segment'
                previous_ids_not_linked = expired_ids
            else:
                track = self._tracks[track_id]
                history = list(track['history'])
                velocity = (point-track['point'])/max(key[3]-track['time'], .05)
                segment_start_reason = track['segment_start_reason']
                previous_ids_not_linked = track['previous_ids_not_linked']
            witness = dict(source=dict(source), point=point.tolist(), box=box.tolist(),
                           vertical_verified=True, match_candidates=candidates)
            history.append(witness)
            self._tracks[track_id] = dict(kind=targets[index]['kind'], point=point,
                box=box, time=key[3], velocity=velocity, history=history,
                segment_start_reason=segment_start_reason, previous_ids_not_linked=previous_ids_not_linked)
            packets[index].update(track_id=track_id, tracking_evidence=dict(
                reason='measured_consecutive_vertical_trajectory', track_id=track_id,
                segment_start_reason=segment_start_reason, previous_ids_not_linked=previous_ids_not_linked,
                actual_observation_count=len(history), trajectory=history))
        return packets


def _foot_clue(rgb, target):
    """Follow the detected pink head's connected pawn pixels to its flared foot."""
    if target.get('anchor_type') != 'head':
        return None
    x, y = map(float, target['point'])
    box = target.get('circle_box') or target.get('box')
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    diameter = max(float(box[2]), float(box[3]))
    h, w = rgb.shape[:2]
    x0, x1 = max(0, int(x-diameter)), min(w, int(x+diameter)+1)
    y0, y1 = max(0, int(y-diameter/2)), min(h, int(y+diameter*2.8)+1)
    hsv = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2HSV)
    mask = ((hsv[..., 0] >= 128) & (hsv[..., 0] <= 177) &
            (hsv[..., 1] > 35) & (hsv[..., 2] > 60)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    sx, sy = int(round(x))-x0, int(round(y))-y0
    if not (0 <= sy < labels.shape[0] and 0 <= sx < labels.shape[1]):
        return None
    # The native pawn has a dark neck separating the spherical head and body.
    # Require the adjacent lower component's waist/base silhouette rather than
    # fabricating a fixed head-to-foot offset through the occluding cube wall.
    choices = []
    for label in range(1, count):
        bx, by, bw, bh, area = stats[label]
        ys, xs = np.nonzero(labels == label)
        if label == labels[sy, sx] and bh >= diameter*1.25:
            body = ys >= sy+diameter*.48
            ys, xs = ys[body], xs[body]
            if len(xs) < 12:
                continue
            bx, by = int(xs.min()), int(ys.min())
            bw, bh = int(np.ptp(xs)+1), int(np.ptp(ys)+1)
            area = len(xs)
        if not (.45*diameter <= bw <= 1.65*diameter and .65*diameter <= bh <= 2*diameter
                and y+.28*diameter <= by+y0 <= y+.85*diameter
                and abs(bx+bw/2+x0-x) <= .5*diameter and area >= diameter*4):
            continue
        neck = xs[ys <= ys.min()+max(2, bh*.20)]
        base = xs[(ys >= ys.min()+bh*.55) & (ys <= ys.min()+bh*.85)]
        bottom = xs[ys >= ys.max()-2]
        if len(neck) < 3 or len(base) < 3 or len(bottom) < 3 or np.ptp(base) < np.ptp(neck)*1.08:
            continue
        choices.append(dict(point=[float(np.median(bottom)+x0), float(ys.max()+y0)],
            evidence_type='observed_head_adjacent_waist_flared_foot', component_pixels=len(xs),
            head_diameter=diameter, pawn_height=int(bh)))
    return choices[0] if len(choices) == 1 else None


def _partial_ring(mask, origin, target_point):
    """Fit a curved ring even when its star connects it to a yellow glyph."""
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    points = np.concatenate(contours).reshape(-1, 2).astype(np.float32)
    if len(points) < 30:
        return None
    rng = np.random.default_rng(0)
    fits = []
    for _ in range(80):
        sample = points[rng.choice(len(points), min(15, len(points)), replace=False)]
        try:
            (cx, cy), axes, angle = cv2.fitEllipse(sample)
        except cv2.error:
            continue
        if min(axes) < 12 or max(axes) > max(mask.shape)*1.8 or min(axes)/max(axes) < .28:
            continue
        if np.linalg.norm(np.array([cx, cy])+origin-target_point) > max(mask.shape)*.35:
            continue
        radians = math.radians(angle)
        rotation = np.array([[math.cos(radians), math.sin(radians)],
                             [-math.sin(radians), math.cos(radians)]])
        local = (points-[cx, cy]) @ rotation.T / (np.asarray(axes)/2)
        radius = np.linalg.norm(local, axis=1)
        on_curve = abs(radius-1) < .085
        arc = np.arctan2(local[on_curve, 1], local[on_curve, 0])
        sectors = len(set(np.floor((arc+math.pi)*12/(2*math.pi)).astype(int)))
        if sectors < 9 or int(on_curve.sum()) < 45:
            continue
        fits.append((int(on_curve.sum()), sectors, [cx+origin[0], cy+origin[1]], axes, angle))
    if not fits:
        return None
    support, sectors, point, axes, angle = max(fits, key=lambda f: (f[1], f[0]))
    return point, dict(evidence_type='actual_partial_yellow_ring_curve', curve_pixels=support,
                       observed_arc_sectors=sectors, ellipse_axes=list(axes), ellipse_angle=angle)


def _anchor(rgb, target):
    kind = target['kind']
    if kind == 'player':
        foot = _foot_clue(rgb, target)
        return foot['point'] if foot else None, foot
    box = target.get('box')
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None, None
    x, y, w, h = map(float, box)
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(rgb.shape[1], int(x+w)+1), min(rgb.shape[0], int(y+h)+1)
    if x1-x0 < 5 or y1-y0 < 5:
        return None, None
    hsv = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2HSV)
    if kind == 'inspiration':
        mask = ((hsv[..., 0] >= 18) & (hsv[..., 0] <= 42) &
                (hsv[..., 1] > 120) & (hsv[..., 2] > 155)).astype(np.uint8)
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        choices = []
        for i, contour in enumerate(contours):
            if hierarchy is None or hierarchy[0, i, 3] < 0 or len(contour) < 10:
                continue
            (_, _), axes, _ = cv2.fitEllipse(contour)
            if min(axes) < 4 or cv2.contourArea(contour) < 25:
                continue
            (cx, cy), axes, angle = cv2.fitEllipse(contour)
            choices.append((cv2.contourArea(contour), [cx+x0, cy+y0], axes, angle))
        if not choices:
            partial = _partial_ring(mask, np.array([x0, y0]), np.asarray(target['point']))
            return partial if partial else (None, None)
        area, point, axes, angle = max(choices)
        return point, dict(evidence_type='actual_yellow_ring_inner_ellipse', inner_area=area,
                           ellipse_axes=list(axes), ellipse_angle=angle)
    # Star centre is the dark radial core within the model's red effect box.
    # A glow's box centre is not used to choose a cell. Without a native core
    # keypoint supplied by the detector this remains a broad lift-ray candidate.
    point = target.get('core_point')
    if point is not None and target.get('core_evidence'):
        return list(map(float, point)), dict(evidence_type='detected_star_core',
                                            detector_evidence=target['core_evidence'])
    return list(map(float, target['point'])), dict(evidence_type='model_star_box_centre_lift_ray',
                                                  anchor_uncertainty_fraction=.25)


def _projected_head_anchor(rgb, target):
    """Actual visible head pixels for hidden-face candidates, never foot proof."""
    if (target.get('anchor_type') != 'head' or target.get('confirmable') is not True
            or target.get('source') != 'deep_dive_local_pink_head'):
        return None, None
    point = np.asarray(target.get('point'), float)
    box = np.asarray(target.get('circle_box'), float)
    if point.shape != (2,) or box.shape != (4,) or not np.isfinite(point).all() or not np.isfinite(box).all():
        return None, None
    radius = min(box[2:])/2
    if not 5 <= radius <= 24:
        return None, None
    x0, y0 = max(0, int(point[0]-radius)), max(0, int(point[1]-radius))
    x1, y1 = min(rgb.shape[1], int(point[0]+radius)+1), min(rgb.shape[0], int(point[1]+radius)+1)
    if x1 <= x0 or y1 <= y0:
        return None, None
    hsv = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2HSV)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    disk = (xx-point[0])**2+(yy-point[1])**2 <= (radius*.70)**2
    pink = ((hsv[..., 0] >= 128) & (hsv[..., 0] <= 177) &
            (hsv[..., 1] > 35) & (hsv[..., 2] > 60))
    if not disk.any() or float(pink[disk].mean()) < .65:
        return None, None
    return point.tolist(), dict(evidence_type='actual_confirmed_pink_head_projected_lift_candidate',
        actual_head_pink_fraction=float(pink[disk].mean()), head_circle_box=box.tolist(),
        never_contact_or_direct_ownership=True)


def associate_targets(rgb, targets, cells, *, source, view_group, scan_epoch):
    """Associate actual detection packets with extruded per-cell candidate rays.

    ``normal_projection`` is the current camera projection of one panel-width
    outward lift. ``normal_evidence`` must describe its actual geometry source.
    No expected target slots, HUD counts or reference labels are accepted.
    A ``candidate_only`` cell is a fitted hidden-surface proposal. Even a unique
    proposal is never a positive ownership observation or a negative witness.
    """
    source_key(source)
    if type(view_group) is not int or view_group < 0 or not isinstance(scan_epoch, str) or not scan_epoch:
        raise ValueError('invalid_view_or_scan_identity')
    image = np.asarray(rgb)
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError('expected_actual_uint8_rgb')
    rgb_sha256 = hashlib.sha256(np.ascontiguousarray(image).tobytes()).hexdigest()
    associations = []
    for target in targets:
        kind = target.get('kind')
        if kind not in KINDS:
            continue
        confidence = target.get('confidence', 0.)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            confidence = 0.
        anchor, clue = _anchor(image, target)
        projected_head, head_clue = _projected_head_anchor(image, target) if kind == 'player' and anchor is None else (None, None)
        candidates = []
        rejected_projections = []
        for cell in cells:
            candidate_only = cell.get('candidate_only') is True
            projection_evidence = cell.get('projection_evidence')
            if candidate_only:
                fit_rms = projection_evidence.get('visible_fit_rms_px') if isinstance(projection_evidence, dict) else None
                inliers = projection_evidence.get('inlier_count') if isinstance(projection_evidence, dict) else None
                matched = projection_evidence.get('actual_matched_data') if isinstance(projection_evidence, dict) else None
                if (not isinstance(projection_evidence, dict) or
                        projection_evidence.get('source_rgb_sha256') != rgb_sha256 or
                        projection_evidence.get('actual_fit_supported') is not True or
                        isinstance(fit_rms, bool) or not isinstance(fit_rms, (int, float)) or
                        not math.isfinite(fit_rms) or fit_rms < 0 or
                        type(inliers) is not int or inliers <= 0 or
                        not isinstance(matched, list) or not matched):
                    rejected_projections.append(dict(face=cell.get('face'), row=cell.get('row'),
                        col=cell.get('col'), reason='current_actual_projection_fit_required',
                        projection_evidence=projection_evidence))
                    continue
            quad = np.asarray(cell.get('quad'), float)
            if quad.shape != (4, 2) or not np.isfinite(quad).all():
                if candidate_only:
                    rejected_projections.append(dict(face=cell.get('face'), row=cell.get('row'),
                        col=cell.get('col'), reason='invalid_projected_quad', projection_evidence=projection_evidence))
                continue
            if not cv2.isContourConvex(quad.astype(np.float32)):
                if candidate_only:
                    rejected_projections.append(dict(face=cell.get('face'), row=cell.get('row'),
                        col=cell.get('col'), reason='nonconvex_projected_quad', projection_evidence=projection_evidence))
                continue
            center = quad.mean(axis=0)
            width = float(np.mean(np.linalg.norm(np.roll(quad, -1, axis=0)-quad, axis=1)))
            head_only_projected = candidate_only and anchor is None and projected_head is not None
            candidate_anchor = projected_head if head_only_projected else anchor
            if candidate_anchor is None or width < 8:
                if candidate_only and width < 8:
                    rejected_projections.append(dict(face=cell.get('face'), row=cell.get('row'),
                        col=cell.get('col'), reason='projected_panel_too_small', projection_evidence=projection_evidence))
                continue
            if kind == 'player' and not head_only_projected:
                # Actual foot/support pixels, not a head/bounding-box centre.
                distance = float(np.linalg.norm(np.asarray(candidate_anchor)-center)/width)
                lift = 0.
                geometry = dict(evidence_type='actual_foot_to_panel_support')
            else:
                normal = np.asarray(cell.get('normal_projection'), float)
                evidence = cell.get('normal_evidence')
                if normal.shape != (2,) or not np.isfinite(normal).all() or not evidence:
                    if candidate_only:
                        rejected_projections.append(dict(face=cell.get('face'), row=cell.get('row'),
                            col=cell.get('col'), reason='projected_normal_unavailable', projection_evidence=projection_evidence))
                    continue
                if isinstance(evidence, dict) and evidence.get('source_rgb_sha256', rgb_sha256) != rgb_sha256:
                    if candidate_only:
                        rejected_projections.append(dict(face=cell.get('face'), row=cell.get('row'),
                            col=cell.get('col'), reason='projected_normal_from_other_rgb', projection_evidence=projection_evidence))
                    continue
                if np.linalg.norm(normal) < width*.035:
                    if candidate_only:
                        rejected_projections.append(dict(face=cell.get('face'), row=cell.get('row'),
                            col=cell.get('col'), reason='projected_lift_unobservable', projection_evidence=projection_evidence))
                    continue
                lift_kind = 'player_head' if head_only_projected else kind
                low, high = LIFT_RANGES[lift_kind]
                delta = np.asarray(candidate_anchor)-center
                lift = float(np.clip(delta @ normal/(normal @ normal), low, high))
                distance = float(np.linalg.norm(delta-normal*lift)/width)
                geometry = dict(evidence_type='outward_surface_normal_lift_ray',
                    normal_projection=normal.tolist(), normal_evidence=evidence,
                    lift_range=list(LIFT_RANGES[lift_kind]), inferred_lift_panel_units=lift,
                    projected_head_only=head_only_projected,
                    candidate_anchor_point=candidate_anchor,
                    candidate_anchor_evidence=head_clue if head_only_projected else clue)
            limit = .32 if kind == 'singularity' else .26
            if distance <= limit:
                candidates.append(dict(face=cell['face'], row=int(cell['row']), col=int(cell['col']),
                    cell_index=cell_to_slot(cell), normalized_residual=distance,
                    candidate_only=candidate_only, adds_ownership_group=False if candidate_only else None,
                    projection_evidence=projection_evidence,
                    geometry=geometry))
        candidates.sort(key=lambda row: row['normalized_residual'])
        unique = (len(candidates) == 1 or len(candidates) > 1 and
                  candidates[1]['normalized_residual']-candidates[0]['normalized_residual'] >= .18)
        chosen = candidates[0] if (candidates and unique and target.get('confirmable') is True
                                  and candidates[0]['candidate_only'] is not True) else None
        associations.append(dict(kind=kind, confidence=float(confidence),
            track_id=target.get('track_id'), tracking_evidence=target.get('tracking_evidence'),
            confirmable=target.get('confirmable') is True, source=dict(source),
            source_rgb_sha256=rgb_sha256,
            frame_id=source['frame_id'], group=view_group, scan_epoch=scan_epoch,
            target_packet=dict(target), anchor_point=anchor, anchor_evidence=clue,
            candidates=candidates, cell_index=chosen['cell_index'] if chosen else None,
            rejected_candidate_projections=rejected_projections,
            candidate_projection_complete=not rejected_projections,
            cell={key: chosen[key] for key in ('face', 'row', 'col')} if chosen else None,
            ownership_status='candidate' if chosen else 'unknown',
            unknown_reason=None if chosen else 'ambiguous_or_unsupported_contact_geometry'))
    return associations


def fuse_target_associations(observations):
    """Deduplicate same canonical slot; require two actual independent groups.

    Unassigned side peeks are retained. They cannot be silently paired with the
    nearest confirmed entity, so readiness stays false until geometry explains
    every relevant detection.
    """
    buckets = defaultdict(list)
    unresolved = []
    epochs, sessions, revisions = set(), set(), set()
    for row in observations:
        if row.get('confirmable') is not True and row.get('confidence', 0) < .25:
            continue
        source = row.get('source')
        try:
            session, revision, _, _ = source_key(source)
        except (ValueError, TypeError):
            unresolved.append(dict(row, unknown_reason='invalid_actual_source'))
            continue
        epochs.add(row.get('scan_epoch')); sessions.add(session); revisions.add(revision)
        if type(row.get('group')) is not int or row['group'] < 0 or not isinstance(row.get('scan_epoch'), str) or not row['scan_epoch']:
            unresolved.append(dict(row, unknown_reason='invalid_view_or_scan_identity'))
            continue
        if row.get('cell_index') is None:
            unresolved.append(dict(row))
        else:
            try:
                if cell_to_slot(row.get('cell')) != row['cell_index']:
                    raise ValueError('cell_slot_mismatch')
                if not any(candidate.get('cell_index') == row['cell_index'] and
                           candidate.get('candidate_only') is not True
                           for candidate in row.get('candidates', ())):
                    raise ValueError('direct_visible_candidate_required')
            except (ValueError, TypeError, KeyError, AttributeError):
                unresolved.append(dict(row, unknown_reason='cell_slot_or_direct_candidate_mismatch'))
                continue
            buckets[(row['kind'], row['cell_index'])].append(row)
    if len(epochs) > 1 or len(sessions) > 1 or len(revisions) > 1:
        return dict(targets=[], unassociated_target_candidates=list(observations),
                    targets_ready=False, reason='mixed_scan_target_evidence')
    targets = []
    occupied = defaultdict(set)
    track_slots = defaultdict(set)
    for (kind, slot), rows in buckets.items():
        for row in rows:
            if type(row.get('track_id')) is int:
                track_slots[(kind, row['track_id'])].add(slot)
    for (kind, slot), rows in buckets.items():
        groups = {r['group'] for r in rows}
        accepted = [r for r in rows if r.get('confirmable') is True and r.get('confidence', 0) >= .70]
        supported = {r['group'] for r in accepted}
        status = 'confirmed' if len(supported) >= 2 else 'unknown'
        occupied[slot].add(kind)
        targets.append(dict(kind=kind, cell_index=slot, cell=rows[0]['cell'],
            occupant_status=status, confidence=min((r['confidence'] for r in accepted), default=0),
            independent_view_count=len(supported), observed_groups=sorted(groups),
            track_ids=sorted({r['track_id'] for r in rows if type(r.get('track_id')) is int}),
            evidence=[dict(group=r['group'], frame_id=r['frame_id'], source=r['source'],
                occupant=kind, scan_epoch=r['scan_epoch'], anchor_evidence=r['anchor_evidence'],
                candidates=r['candidates']) for r in rows]))
    conflicting_slots = sorted(slot for slot, kinds in occupied.items() if len(kinds) > 1)
    for row in targets:
        if row['cell_index'] in conflicting_slots or any(
                len(track_slots[(row['kind'], track_id)]) > 1 for track_id in row['track_ids']):
            row['occupant_status'] = 'conflict'
    # A known trajectory can explain side peeks, but those peeks add zero
    # ownership groups. Conflicting geometric owners of a track remain unknown.
    track_owners = defaultdict(set)
    for target in targets:
        if target['occupant_status'] == 'confirmed':
            for track_id in target['track_ids']:
                track_owners[(target['kind'], track_id)].add(target['cell_index'])
    explained, pending = [], []
    for row in unresolved:
        key = (row.get('kind'), row.get('track_id'))
        owners = track_owners.get(key, set())
        if len(owners) == 1 and (row.get('tracking_evidence') or {}).get('reason') == 'measured_consecutive_vertical_trajectory':
            explained.append(dict(row, cell_index=next(iter(owners)),
                ownership_status='identity_explained_side_peek', adds_ownership_group=False))
        else:
            pending.append(row)
    unresolved = pending
    # Unique player/Boss is a game invariant, not a selection rule.
    unique = all(sum(t['kind'] == kind for t in targets) == 1 for kind in ('player', 'singularity'))
    ready = unique and bool(targets) and not unresolved and not conflicting_slots and all(
        t['occupant_status'] == 'confirmed' for t in targets)
    return dict(targets=targets, unassociated_target_candidates=unresolved,
                identity_explained_candidates=explained,
                conflicting_slots=conflicting_slots, targets_ready=ready,
                reason='target_geometry_supported' if ready else 'independent_target_ownership_required')
