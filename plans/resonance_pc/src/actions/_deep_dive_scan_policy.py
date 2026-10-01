"""Stateful, geometry-driven view selection for the experimental cube scanner.

Response axes are camera-frame rotation vectors in radians per mouse pixel.
Only fresh, successfully tracked observations should be passed to ``choose``.
The policy never declares the map complete from camera motion alone.
"""

import math
import time

import numpy as np


_NORMALS = {
    "U": (0., -1., 0.), "R": (1., 0., 0.), "F": (0., 0., -1.),
    "D": (0., 1., 0.), "L": (-1., 0., 0.), "B": (0., 0., 1.),
}
_VIEWS = ((0., 0., -1.), (.20, 0., -1.), (0., .20, -1.),
          (-.20, 0., -1.), (0., -.20, -1.))


def _unit(value):
    vector = np.asarray(value, dtype=float)
    return vector / max(float(np.linalg.norm(vector)), 1e-12)


def _angle(left, right):
    return math.acos(float(np.clip(np.dot(_unit(left), _unit(right)), -1., 1.)))


def _rotated(vector, rotvec):
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-12:
        return vector.copy()
    axis = rotvec / theta
    return (vector * math.cos(theta) + np.cross(axis, vector) * math.sin(theta)
            + axis * np.dot(axis, vector) * (1. - math.cos(theta)))


class FaceScanPolicy:
    """Lock a face, approach it, then obtain up to five distinct normal views.

    Unseen faces precede incomplete observed faces. A face is released only on
    completion, after the view budget, or after a bounded approach budget. Visits
    provide round-robin fairness when ambiguous evidence cannot be resolved.
    ``insufficient`` retains diagnostics; it does not prevent later revisits.
    """

    def __init__(self, base_step_px=300, max_step_px=450):
        self.base_step_px = max(30., float(base_step_px))
        self.max_step_px = max(self.base_step_px, float(max_step_px))
        self.target_face = None
        self.view_index = 0
        self.moves_at_view = 0
        self.visits = {face: 0 for face in _NORMALS}
        self.insufficient = []
        self.accepted_normals = []
        self.last_direction = (1, 0)
        self.max_moves_at_view = 18
        self.goal_tolerance_deg = 2.5
        self.allow_diagonal = False
        self.view_goals = _VIEWS

    @staticmethod
    def _summary(cells):
        result = {}
        for face in _NORMALS:
            rows = [cell for cell in cells if cell.get("face") == face]
            complete = sum(cell.get("occupant_status") == "confirmed" and
                           cell.get("node_status") in ("known", "not_required_target")
                           for cell in rows)
            observed = any(cell.get("evidence") or
                           cell.get("occupant_status") == "confirmed" for cell in rows)
            result[face] = (complete, observed)
        return result

    def _release(self, summary, reason):
        face = self.target_face
        if face is not None:
            self.visits[face] += 1
            if summary[face][0] < 9:
                self.insufficient.append(dict(face=face, known_cells=summary[face][0],
                                              reason=reason, visit=self.visits[face]))
        self.target_face = None
        self.accepted_normals = []
        self.view_index = 0
        self.moves_at_view = 0

    def choose(self, rotation, cells, response_axes, quality=1.0, observed_rotation=None):
        rotation = np.asarray(rotation, dtype=float)
        observed_rotation = rotation if observed_rotation is None else np.asarray(observed_rotation, dtype=float)
        if (rotation.shape != (3, 3) or not np.all(np.isfinite(rotation))
                or not math.isfinite(float(quality)) or quality < .25):
            return dict(direction=None, reason="pose_not_reliable", phase="recover")
        summary = self._summary(cells)
        if all(count >= 9 for count, _ in summary.values()):
            return dict(direction=None, reason="all_cells_confirmed", phase="complete")

        axes = {}
        for axis in (0, 1):
            value = np.asarray(response_axes.get(axis, []), dtype=float)
            if (value.shape == (3,) and np.all(np.isfinite(value))
                    and float(np.linalg.norm(value)) > 1e-7):
                axes[axis] = value
        for axis in (0, 1):
            if axis not in axes:
                return dict(direction=(1, 0) if axis == 0 else (0, 1),
                            distance_px=150., target_face=self.target_face,
                            target_angle_deg=None, predicted_angle_deg=None,
                            phase="calibrate", reason="calibrate_axis_%s" % axis)

        # A reached viewpoint is consumed once; the next call targets another
        # orientation, even if evidence has not yet become a confirmed label.
        for _ in range(8):
            if self.target_face and summary[self.target_face][0] >= 9:
                self._release(summary, "face_complete")
            if self.target_face is None:
                candidates = [face for face in _NORMALS if summary[face][0] < 9]
                self.target_face = min(candidates, key=lambda face: (
                    self.visits[face], summary[face][1], summary[face][0],
                    _angle(rotation @ np.asarray(_NORMALS[face]), (0., 0., -1.))))
            normal = _unit(observed_rotation @ np.asarray(_NORMALS[self.target_face]))
            goal = _unit(self.view_goals[self.view_index])
            remaining = _angle(normal, goal)
            reached = remaining <= math.radians(self.goal_tolerance_deg)
            distinct = all(_angle(normal, previous) >= math.radians(8.)
                           for previous in self.accepted_normals)
            if reached and distinct and quality >= .5:
                self.accepted_normals.append(normal.copy())
                self.view_index += 1
                self.moves_at_view = 0
                if self.view_index >= len(self.view_goals):
                    self._release(summary, "view_budget_exhausted")
                continue
            if self.moves_at_view >= self.max_moves_at_view:
                # Allow other faces a turn even if one orientation is unreachable
                # or tracking repeatedly underestimates the actual movement.
                self._release(summary, "approach_budget_exhausted")
                continue
            break

        normal = _unit(rotation @ np.asarray(_NORMALS[self.target_face]))
        goal = _unit(self.view_goals[self.view_index])
        remaining = _angle(normal, goal)
        degrees = math.degrees(remaining)
        # Far approaches can be broad; final adjustments shrink continuously.
        cap = (self.max_step_px if degrees > 60. else
               self.base_step_px if degrees > 25. else
               max(30., self.base_step_px * degrees / 25.))
        cap *= max(.35, min(1., float(quality)))
        desired_axis = np.cross(normal, goal)
        options = []
        for axis, response in axes.items():
            tangent_gain = float(np.linalg.norm(np.cross(response, normal)))
            if tangent_gain < 1e-8:
                continue
            alignment = float(np.dot(response, _unit(desired_axis)))
            gain = max(abs(alignment), .2 * tangent_gain)
            step = min(cap, max(20., remaining * .85 / gain),
                       math.radians(100.) / tangent_gain)
            for sign in (-1, 1):
                direction = (sign, 0) if axis == 0 else (0, sign)
                # Use the actual proposed length in every prediction. Shorter
                # candidates handle coupled axes and avoid final-view overshoot.
                for fraction in (1., .75, .5, .25):
                    # Subpixel/very short drags can become clicks or be smaller
                    # than idle pose jitter. Predict the executable integer
                    # distance, including this lower bound.
                    distance = max(20, int(round(step * fraction)))
                    predicted = _rotated(normal, response * sign * distance)
                    after = _angle(predicted, goal)
                    continuity = direction == self.last_direction
                    options.append((after, not continuity, -distance, direction, distance,
                                    _angle(normal, predicted)))
        if self.allow_diagonal and np.linalg.norm(desired_axis) > 1e-7:
            # Continuous control can combine both calibrated input axes in one
            # motion instead of alternating horizontal/vertical corrections.
            pixels = np.linalg.lstsq(np.column_stack((axes[0], axes[1])),
                _unit(desired_axis)*remaining, rcond=None)[0]
            magnitude = float(np.linalg.norm(pixels))
            if magnitude > 1.:
                direction = tuple(float(x) for x in pixels/magnitude)
                step = min(cap, magnitude*.85)
                rate = axes[0]*direction[0] + axes[1]*direction[1]
                for fraction in (1., .75, .5, .25):
                    distance = max(20, int(round(step*fraction)))
                    predicted = _rotated(normal, rate*distance)
                    options.append((_angle(predicted, goal), direction != self.last_direction,
                                    -distance, direction, distance, _angle(normal, predicted)))
        if not options:
            return dict(direction=None, reason="response_axes_cannot_turn_target",
                        target_face=self.target_face, goal_normal=goal.tolist(), phase="recover")
        best = min(options, key=lambda item: (round(item[0], 8), item[1], item[2]))
        after, _, _, direction, distance, movement = best
        self.last_direction = direction
        self.moves_at_view += 1
        return dict(direction=direction, distance_px=float(distance),
                    target_face=self.target_face, target_angle_deg=degrees,
                    goal_normal=goal.tolist(),
                    predicted_angle_deg=math.degrees(movement),
                    predicted_remaining_deg=math.degrees(after),
                    phase="approach" if self.view_index == 0 else "detail",
                    reason="locked_face_%s_view_%s" % (self.target_face, self.view_index))


def _matrix(rotvec):
    """Rodrigues without an OpenCV dependency in the controller hot path."""
    return np.column_stack([_rotated(column, rotvec) for column in np.eye(3)])


def _pose_angle(left, right):
    return math.acos(float(np.clip((np.trace(left @ right.T) - 1.) * .5, -1., 1.)))


def _pose_vector(left, right):
    delta = left @ right.T
    angle = _pose_angle(left, right)
    skew = np.array((delta[2, 1]-delta[1, 2], delta[0, 2]-delta[2, 0],
                     delta[1, 0]-delta[0, 1]))
    if angle < 1e-8:
        return np.zeros(3)
    if math.pi-angle < 1e-5:
        values, vectors = np.linalg.eigh((delta+np.eye(3))*.5)
        return vectors[:, np.argmax(values)] * angle
    return skew * angle / (2.*math.sin(angle))


class CellScanPolicy:
    """Choose reachable two-drag poses by projecting the actual missing cells.

    Unlike a face-normal target, each candidate retains roll and a concrete
    two-axis route. Geometry only predicts readability; confirmation remains
    wholly owned by the scanner. Replanning is bounded by observed pose/time,
    never by the number of high-frequency controller calls.
    """

    def __init__(self, base_step_px=300, max_step_px=450, *,
                 recognition_goal='full', expected_inspirations=None):
        from ._deep_dive_layout_vision import BASES, K, DISTANCE, HALF_TILE, SEED_T
        if recognition_goal not in ('full', 'layout', 'targets'):
            raise ValueError('unsupported_recognition_goal')
        if expected_inspirations is not None and (isinstance(expected_inspirations, bool)
                or not isinstance(expected_inspirations, (int, np.integer))
                or not 0 <= expected_inspirations <= 54):
            raise ValueError('invalid_expected_inspirations')
        self.recognition_goal = recognition_goal
        self.expected_inspirations = expected_inspirations
        self._current_target_deficits = set()
        self._observed_motion_rate = None
        self._motion_pose = None
        self._motion_time = None
        self.base_step_px = float(base_step_px)
        self.max_step_px = float(max_step_px)
        self.allow_diagonal = True
        self.goal_tolerance_deg = 4.
        self.target_face = None
        self.insufficient = []
        self.visits = {face: 0 for face in _NORMALS}
        self._K = K.copy()
        self._tile_span = 2.*HALF_TILE
        self.tvec = SEED_T.copy()
        self._keys = [(face, row, col) for face in _NORMALS
                      for row in range(3) for col in range(3)]
        self._normals = np.array([BASES[face][0] for face, _, _ in self._keys], float)
        self._points = np.array([np.array(BASES[face][0])*DISTANCE
            + np.array(BASES[face][1])*(col-1) + np.array(BASES[face][2])*(row-1)
            for face, row, col in self._keys])
        self._quads = np.array([[self._points[i] + np.array(BASES[face][1])*x*HALF_TILE
            + np.array(BASES[face][2])*y*HALF_TILE
            for x, y in ((-1,-1),(1,-1),(1,1),(-1,1))]
            for i, (face, _, _) in enumerate(self._keys)])
        self._corridors = np.stack((self._points, self._points+self._normals*.85), axis=1)
        self._route = []
        self._final_rotation = None
        self._target_indices = []
        self._started = None
        self._at_goal_since = None
        self._arrival_revision = None
        self._last_plan_time = -1e9
        self._reached = []
        self._failed_views = []
        self._cell_failures = []
        self._latest_votes = np.zeros(54, dtype=float)
        self._plan_votes = np.zeros(54, dtype=float)
        self._last_gain = None
        self._last_attempt = None
        self.planning_time_ms = 0.
        self._last_direction = (1., 0.)
        self._plan_serial = 0
        self.stats = dict(replans=0, no_descent=0, stagnant_replans=0)
        self._progress_goal = None
        self._progress_at = None
        self._progress_angle = None
        self._progress_votes = 0.
        self._no_descent_since = None
        self._pending_replan_reason = None
        self._anchor_recovery = None
        self._anchor_recovery_count = 0
        self._anchor_failed_frames = 0
        self._anchor_feedback_frame = None
        self._calibrator = FaceScanPolicy(base_step_px, max_step_px)

    def request_replan(self, reason='controller_requested_replan'):
        """Reject a route, e.g. a tiny command when the mouse is not held.

        The next fresh choose call has the observations needed for feedback.
        """
        self._pending_replan_reason = str(reason)

    def _rows(self, cells):
        lookup = {(c.get('face'), c.get('row'), c.get('col')): c for c in cells}
        return [lookup.get(key, {}) for key in self._keys]

    @staticmethod
    def _observed(row):
        # A label or camera-facing prediction alone is not face coverage.
        return any(isinstance(item, dict) and isinstance(item.get('frame_id'), (int, np.integer))
                   and not isinstance(item.get('frame_id'), bool)
                   and isinstance(item.get('group'), (int, np.integer))
                   and not isinstance(item.get('group'), bool)
                   and item['frame_id'] >= 0 and item['group'] >= 0
                   for item in row.get('evidence', ()))

    def _objective(self, rows):
        """Planning deficits only; this never changes atlas classes or votes."""
        if self.recognition_goal != 'targets':
            complete = np.array([r.get('occupant_status') == 'confirmed' and
                r.get('node_status') in ('known', 'not_required_target') for r in rows])
            seen = np.array([bool(r.get('evidence') or r.get('occupant_evidence_counts')
                                 or r.get('occupant_status') == 'confirmed') for r in rows])
            return complete, seen
        seen = np.array([self._observed(row) for row in rows])
        complete = np.array([r.get('occupant_status') == 'confirmed' for r in rows])
        positive = {}
        for kind in ('player', 'singularity', 'inspiration'):
            positive[kind] = []
            for index, row in enumerate(rows):
                if row.get('occupant') != kind or not complete[index]:
                    continue
                groups = {entry.get('group') for entry in row.get('evidence', ())
                          if isinstance(entry, dict) and self._observed(dict(evidence=[entry]))
                          and entry.get('occupant', kind) == kind}
                if (row.get('occupant_evidence_counts', {}).get(kind, 0) >= 2
                        and len(groups) >= 2 and row.get('confidence', 0.) >= .70):
                    positive[kind].append(index)
                else:
                    complete[index] = False
        closed = (self.expected_inspirations is not None
                  and len(positive['player']) == len(positive['singularity']) == 1
                  and len(positive['inspiration']) == self.expected_inspirations)
        if closed:
            # HUD closure excludes another target, but does not assign 'none'
            # to any cell. Unresolved positive votes still require real reads.
            for index, row in enumerate(rows):
                if not complete[index] and not any(row.get('occupant_evidence_counts', {}).get(kind, 0)
                        for kind in ('player', 'singularity', 'inspiration')):
                    complete[index] = True
        # Multiple confirmed unique entities are a contradiction, not success.
        for kind in ('player', 'singularity'):
            if len(positive[kind]) > 1:
                complete[positive[kind]] = False
        if (self.expected_inspirations is not None
                and len(positive['inspiration']) > self.expected_inspirations):
            complete[positive['inspiration']] = False
        if self._current_target_deficits:
            complete[list(self._current_target_deficits)] = False
        return complete, seen

    def _candidate_deficits(self, rows, feedback):
        """Relevant current detections need another real view if not attached."""
        from ._deep_dive_target_readiness import _relevant
        targets = ('player', 'singularity', 'inspiration')
        deficits = set()
        associations = feedback.get('target_candidate_associations', feedback.get('mask_observation', ())) or ()
        clues = feedback.get('target_clues', ()) or ()
        relevant = [entry for entry in associations if isinstance(entry, dict) and _relevant(entry)]
        for entry in relevant:
            index, kind = entry.get('cell_index'), entry.get('kind')
            valid_index = isinstance(index, (int, np.integer)) and not isinstance(index, bool) and 0 <= index < 54
            if (valid_index and rows[index].get('occupant_status') == 'confirmed'
                    and rows[index].get('occupant') == kind and kind in targets):
                continue
            if valid_index:
                deficits.add(int(index))
            else:
                deficits.update(i for i, row in enumerate(rows) if row.get('occupant') == kind)
        for kind in targets:
            if (sum(isinstance(entry, dict) and _relevant(entry) and entry.get('kind') == kind for entry in clues)
                    > sum(entry.get('kind') == kind for entry in relevant)):
                deficits.update(i for i, row in enumerate(rows) if row.get('occupant') == kind)
        return deficits

    @staticmethod
    def _votes(rows):
        # Vote counts represent independent accepted observations, whereas
        # semantic revision alone also advances on entirely unreadable frames.
        return np.array([sum(row.get('occupant_evidence_counts', {}).values())
                         if row.get('occupant_evidence_counts') else
                         len(row.get('evidence', [])) for row in rows], dtype=float)

    def _feedback(self, rows, pose, now, reason, force_failure=False):
        votes = self._votes(rows)
        complete, _ = self._objective(rows)
        failed = [i for i in self._target_indices if force_failure or
                  (votes[i] <= self._plan_votes[i] and not complete[i])]
        if self._target_indices:
            self._last_attempt[self._target_indices] = now
        if failed:
            self._cell_failures.append((pose.copy(), failed, now))
            self._cell_failures = self._cell_failures[-64:]
            self.insufficient.append(dict(face=self.target_face, reason=reason,
                target_cells=[self._keys[i] for i in failed]))

    def _sprite_footprint(self, row):
        """Infer a billboard size from accepted same-image sprite/tile evidence.

        Tile area divided by facing cosine estimates an unforeshortened pixel
        scale. Sprite rectangles are screen-facing, so their extent must not
        shrink with the candidate face's cosine. These are planning bounds,
        never fresh image evidence, labels or renewed calibration.
        """
        occupant = row.get('occupant')
        limits = {'singularity': (.20, 1.), 'inspiration': (.05, .65),
                  'player': (.06, .85)}.get(occupant)
        if limits is None:
            return None
        radii, heights = [], []
        for evidence in row.get('evidence', [])[-12:]:
            if (evidence.get('occupant') != occupant or
                    evidence.get('confidence', 0.) < .60 or
                    evidence.get('quality', 1.) < .50):
                continue
            try:
                box = np.asarray(evidence.get('target_box', []), float)
                quad = np.asarray(evidence.get('quad', []), float)
                cosine = float(evidence.get('cosine', 0.))
            except (TypeError, ValueError):
                continue
            if (box.shape != (4,) or quad.shape != (4, 2) or
                    not np.isfinite(box).all() or not np.isfinite(quad).all() or
                    not math.isfinite(cosine) or not .20 <= cosine <= 1. or
                    min(box[2:]) < 8. or max(box[2:]) > 400.):
                continue
            area = .5*abs(np.sum(quad[:, 0]*np.roll(quad[:, 1], -1)
                                -quad[:, 1]*np.roll(quad[:, 0], -1)))
            if area < 400.:
                continue
            scale = math.sqrt(area/cosine)/self._tile_span
            radius = (box[2:]*.5+8.)/scale
            if max(radius) > 2.:
                continue
            radii.append(radius)
            # Near a face-normal view the projected normal is too short to
            # estimate height. Keep the finite physical prior in that case.
            if occupant != 'player' and cosine < .95:
                try:
                    point = np.asarray(evidence.get('target_point', box[:2]+box[2:]*.5), float)
                except (TypeError, ValueError):
                    continue
                if (point.shape == (2,) and np.isfinite(point).all() and
                        (point >= box[:2]-15.).all() and
                        (point <= box[:2]+box[2:]+15.).all()):
                    height = np.linalg.norm(point-quad.mean(axis=0))/(scale*math.sqrt(1.-cosine*cosine))
                    if limits[0]-.15 <= height <= limits[1]+.15:
                        heights.append(float(np.clip(height, *limits)))
        if not radii:
            return None
        # Keep the largest observed animation footprint. A player's head box
        # alone does not cover the body, so retain its full normal-height band.
        height_range = (max(limits[0], min(heights)-.10),
                        min(limits[1], max(heights)+.10)) if heights else limits
        return np.max(radii, axis=0), height_range

    def _sprite_occlusion_bounds(self, rotations, index, row, pixel_scale, corridor):
        profile = self._sprite_footprint(row)
        if profile is None:
            return corridor.min(axis=1)-14., corridor.max(axis=1)+14.
        radius, heights = profile
        points = self._points[index]+self._normals[index]*np.asarray(heights)[:, None]
        camera = np.einsum('bij,nj->bni', rotations, points)+np.asarray(self.tvec).reshape(3)
        projected = camera[..., :2]/camera[..., 2, None]*self._K[0, 0]+self._K[:2, 2]
        extent = np.maximum(14., pixel_scale[:, None]*radius[None, :])
        return projected.min(axis=1)-extent, projected.max(axis=1)+extent

    def _readability(self, rotations, rows, *, anchors=False, known_only=True):
        """Vectorized, conservative counterpart of the scanner's image gates."""
        translations = np.asarray(self.tvec, float).reshape(3)
        def project(points):
            camera = np.einsum('bij,...j->b...i', rotations, points) + translations
            return camera[..., :2]/camera[..., 2, None]*self._K[0, 0]+self._K[:2, 2]
        q = project(self._quads)
        centres = project(self._points)
        corridor = project(self._corridors)
        camera_points = np.einsum('bij,nj->bni', rotations, self._points)+translations
        normals = np.einsum('bij,nj->bni', rotations, self._normals)
        cosine = -np.sum(normals*camera_points, axis=2)/np.linalg.norm(camera_points, axis=2)
        area = .5*np.abs(np.sum(q[...,0]*np.roll(q[...,1], -1, axis=2)
                                -q[...,1]*np.roll(q[...,0], -1, axis=2), axis=2))
        valid = ((q[...,0] >= 300).all(axis=2) & (q[...,0] <= 950).all(axis=2)
                 & (q[...,1] >= 84).all(axis=2) & (q[...,1] <= 615).all(axis=2)
                 & (area >= (900 if anchors else 800)) & (cosine >= (.30 if anchors else .48)))
        # Convex projected tile contains the fixed reset button centre.
        edge = np.roll(q, -1, axis=2)-q
        delta = np.array((640., 534.))-q
        cross = edge[...,0]*delta[...,1]-edge[...,1]*delta[...,0]
        contains_reset = (cross >= 0).all(axis=2) | (cross <= 0).all(axis=2)
        valid &= ~contains_reset
        valid &= ~((centres[...,0] > 594) & (centres[...,0] < 687)
                   & (centres[...,1] > 485) & (centres[...,1] < 587))
        clear_corridor = (((corridor[...,0] > 305) & (corridor[...,0] < 940)
                          & (corridor[...,1] > 95) & (corridor[...,1] < 600)
                          & ~((corridor[...,0] > 590) & (corridor[...,0] < 698)
                              & (corridor[...,1] > 480) & (corridor[...,1] < 590))).all(axis=2))
        target = np.array([row.get('occupant') in ('player','inspiration','singularity')
                           for row in rows])
        if anchors and known_only:
            known = np.array([row.get('occupant') == 'none' and
                row.get('node_status') == 'known' and row.get('confidence', 0) >= .70
                for row in rows])
            valid &= known[None, :]
        valid &= clear_corridor | target[None, :]
        # Predict the observed sprite's full screen-facing footprint over a
        # finite normal-height band. Actual current-frame rectangles remain
        # authoritative in the semantic worker; no-box rows keep the old prior.
        qlo, qhi = q.min(axis=2), q.max(axis=2)
        pixel_scale = np.sqrt(area/np.clip(cosine, .20, 1.))/self._tile_span
        for index in np.flatnonzero(target):
            lo, hi = self._sprite_occlusion_bounds(rotations, index, rows[index],
                pixel_scale[:, index], corridor[:, index])
            overlap = np.maximum(0., np.minimum(qhi, hi[:, None])-np.maximum(qlo, lo[:, None]))
            blocked = (overlap[...,0]*overlap[...,1] > area*.16) & (cosine[:,index,None] > .2)
            blocked[:,index] = False
            valid &= ~blocked
        quality = np.clip((cosine-.40)/.50, .15, 1.)
        return valid * quality

    def _anchor_support(self, rotations, rows):
        visible = self._readability(rotations, rows, anchors=True) > 0
        counts = visible.sum(axis=1)
        faces = np.stack([visible[:, i*9:(i+1)*9].sum(axis=1) for i in range(6)], axis=1)
        # This predicts support only. Actual pixels and residual gates still
        # decide whether the scanner can renew calibration.
        return counts, (counts >= 6) & ((faces >= 2).sum(axis=1) >= 2)

    @staticmethod
    def _short_waypoints(observed, route):
        """Preserve an axis route while bounding each tracked approach to 30°.

        The controller can rotate more slowly when current semantics require
        overlap. A 110° leg must not consume the whole approach watchdog before
        it reaches its first checkpoint. These checkpoints neither accept map
        labels nor introduce extra semantic dwell.
        """
        waypoints = []
        previous = observed
        for destination in route:
            delta = _pose_vector(destination, previous)
            count = max(1, int(math.ceil(np.linalg.norm(delta)/math.radians(30.))))
            waypoints.extend(_matrix(delta*(index/count)) @ previous
                             for index in range(1, count))
            waypoints.append(destination)
            previous = destination
        return waypoints

    def _planning_anchor_support(self, rotations, rows, *, route=False):
        """Predict overlap without requiring every incoming face to be learned.

        A new grid can renew the real fitter only with independent support on
        more than one face. Such views get a discounted planning prior; a view
        containing only an unlearned face keeps its original low support.
        Actual pixels, residuals and semantic confirmation remain authoritative.
        """
        visible = self._readability(rotations, rows, anchors=True, known_only=False) > 0
        regular = np.array([row.get('occupant') not in ('player', 'inspiration', 'singularity')
                            for row in rows])
        known = np.array([row.get('occupant') == 'none' and row.get('node_status') == 'known'
                          and row.get('confidence', 0) >= .70 for row in rows])
        known_visible = visible & known[None, :]
        counts = known_visible.sum(axis=1)
        if route:
            support = np.clip(counts/4., .12, 1.)
        else:
            faces = np.stack([known_visible[:, i*9:(i+1)*9].sum(axis=1) for i in range(6)], axis=1)
            support = np.clip(counts/6., .12, 1.)
            support[(counts >= 6) & ((faces >= 2).sum(axis=1) >= 2)] = 1.25
        potential = visible & regular[None, :]
        potential_faces = np.stack([potential[:, i*9:(i+1)*9].sum(axis=1) for i in range(6)], axis=1)
        incoming = (potential & ~known[None, :]).any(axis=1)
        independent = ((potential.sum(axis=1) >= 7) &
                       ((potential_faces >= 2).sum(axis=1) >= 2) & incoming)
        support[independent] = np.maximum(support[independent], .85)
        return support

    def _route_anchor_support(self, paths, rows, *, allow_potential=False):
        # Project the whole candidate batch once. Rebuilding the same cell
        # arrays for each one/two-pose path stalls the live input controller.
        lengths = np.array([len(path) for path in paths], dtype=int)
        poses = np.asarray([pose for path in paths for pose in path])
        offsets = np.r_[0, np.cumsum(lengths)[:-1]]
        if allow_potential:
            return np.minimum.reduceat(self._planning_anchor_support(poses, rows, route=True), offsets)
        counts, _ = self._anchor_support(poses, rows)
        minima = np.minimum.reduceat(counts, offsets)
        return np.clip(minima/4., .12, 1.)

    def _recovery_search_goal(self, anchor, axes, rows, tried):
        """Choose a local view that preserves unoccluded known glyphs.

        The signs of the measured input axes depend on the current camera.
        Search both signs and prefer support on two faces instead of assuming
        that a positive 12° turn makes the last verified view easier to read.
        This only chooses an input goal; current image fits finish recovery.
        """
        candidates = np.array([_matrix(_unit(axes[axis])*math.radians(sign*angle)) @ anchor
            for axis in (0, 1) for sign in (-1, 1) for angle in (6., 12.)])
        readable = self._readability(candidates, rows, anchors=True)
        counts = (readable > 0).sum(axis=1)
        face_counts = np.stack([(readable[:, index*9:(index+1)*9] > 0).sum(axis=1)
                                for index in range(6)], axis=1)
        second_face = np.sort(face_counts, axis=1)[:, -2]
        multi_face = (counts >= 6) & (second_face >= 2)
        eligible = [index for index, pose in enumerate(candidates)
                    if all(_pose_angle(pose, prior) > math.radians(4.) for prior in tried)]
        if not eligible:
            return None
        best = max(eligible, key=lambda index: (multi_face[index], second_face[index],
            readable[index].sum(), counts[index], -_pose_angle(candidates[index], anchor)))
        return candidates[best]

    def _coverage_prefixes(self, observed, paths):
        """Offer the first useful checkpoint, not just a remote final view.

        Every candidate remains an executable prefix of the same two-axis
        feedback route. Repeated first-leg checkpoints share one candidate;
        different noncommuting paths retain their own actual rotations.
        """
        unique = {}
        for path in paths:
            prefix = []
            previous = observed
            travel = 0.
            for pose in self._short_waypoints(observed, path):
                prefix.append(pose)
                travel += _pose_angle(pose, previous)
                previous = pose
                key = tuple(np.round(pose.ravel(), 6))
                if key not in unique or travel < unique[key][2]:
                    unique[key] = (pose, list(prefix), travel)
        values = list(unique.values())
        return (np.asarray([value[0] for value in values]),
                [value[1] for value in values],
                np.array([value[2] for value in values]))

    def _coverage_gain(self, readable, weights, unseen_faces):
        # A first view of three usable cells is already a face-observation
        # opportunity. Nine cells at a remote endpoint must not outbid that
        # shorter view merely because their ordinary pages remain unknown.
        gain = np.zeros(len(readable))
        for face in unseen_faces:
            indices = [i for i, key in enumerate(self._keys) if key[0] == face]
            evidence = readable[:, indices]
            saturation = np.minimum(evidence.sum(axis=1)/3., 1.)
            saturation[(evidence > 0).sum(axis=1) < 3] = 0.
            gain += saturation * float(weights[indices].mean())
        return gain

    def _plan(self, observed, rows, axes, now):
        plan_started = time.perf_counter()
        complete, seen = self._objective(rows)
        unseen_faces = {face for face in _NORMALS
                        if not any(seen[i] for i, key in enumerate(self._keys) if key[0] == face)}
        pending = ~complete
        if self.recognition_goal == 'targets' and unseen_faces:
            # Coverage first: observed-face page deficits cannot win another
            # route while an entire target-bearing face remains unobserved.
            pending = np.array([key[0] in unseen_faces for key in self._keys])
        weights = np.array([0. if not pending[i] else
            (3. if key[0] in unseen_faces else 1.) *
            (1.6 if rows[i].get('occupant_status') == 'conflict' or
             (self.recognition_goal != 'targets' and rows[i].get('node_status') == 'conflict') else 1.) *
            (1.+min(4., max(0., now-self._last_gain[i])/8.)) *
            (.25+.75*min(1., max(0., now-self._last_attempt[i])/6.))
            for i, key in enumerate(self._keys)])
        # Both action orders matter: their noncommutation provides different
        # image roll and moves corner nodes away from the reset-button region.
        angles = np.radians((-110., -75., -45., -22., 0., 22., 45., 75., 110.))
        turns = {axis: [_matrix(_unit(axes[axis])*angle) for angle in angles]
                 for axis in (0,1)}
        candidates, paths, costs = [], [], []
        for first, second in ((0,1),(1,0)):
            for i, a in enumerate(angles):
                intermediate = turns[first][i] @ observed
                for j, b in enumerate(angles):
                    if abs(a)+abs(b) < math.radians(10.):
                        continue
                    final = turns[second][j] @ intermediate
                    candidates.append(final)
                    paths.append(([intermediate] if abs(a) > .01 else [])
                                 + ([final] if abs(b) > .01 else []))
                    costs.append(abs(a)+abs(b))
        candidates = np.asarray(candidates)
        prefix_coverage = self.recognition_goal == 'targets' and bool(unseen_faces)
        if prefix_coverage:
            candidates, paths, costs = self._coverage_prefixes(observed, paths)
        readable = self._readability(candidates, rows)
        # A geometrically plausible view that produced no accepted votes is
        # evidence against that pose-cell pair, not evidence against the cell.
        # Penalize its neighbourhood, permitting a substantially different roll
        # or angle to try the same cell again without weakening confirmation.
        adjusted = readable.copy()
        for failed_pose, indices, failed_at in self._cell_failures:
            traces = np.einsum('bij,ij->b', candidates, failed_pose)
            angles_from_failure = np.arccos(np.clip((traces-1.)*.5, -1., 1.))
            local_penalty = (.92*np.exp(-.5*(angles_from_failure/math.radians(28.))**2)
                             * math.exp(-max(0.,now-failed_at)/45.))
            adjusted[:,indices] *= 1.-local_penalty[:,None]
        # Every accepted viewpoint must contribute a genuinely new orientation.
        # A recently reached pose cannot repeatedly collect independent votes.
        novelty = np.ones(len(candidates), dtype=bool)
        for prior in self._reached[-48:] + self._failed_views[-12:] + [observed]:
            traces = np.einsum('bij,ij->b', candidates, prior)
            novelty &= traces < 1.+2.*math.cos(math.radians(9.))
        gain = (self._coverage_gain(adjusted, weights, unseen_faces)
                if prefix_coverage else adjusted @ weights)
        if prefix_coverage:
            # Observed angular travel includes the real frame/semantic speed
            # ceiling. Calibration provides a conservative prior until enough
            # fresh tracked movement is available; no input speed is changed.
            rate = self._observed_motion_rate or min(
                math.radians(24.), max(np.linalg.norm(value) for value in axes.values())*450.)
            score = gain / (.65+np.asarray(costs)/max(math.radians(3.), rate))
        else:
            score = gain / (1.+np.asarray(costs)/math.radians(100.))
        if any(r.get('node_status') == 'known' for r in rows):
            # Retain routes whose intermediate pose also preserves overlap.
            score *= self._planning_anchor_support(candidates, rows)*self._route_anchor_support(
                paths, rows, allow_potential=True)
        score[~novelty] = -1.
        best = int(np.argmax(score))
        if score[best] <= 0:
            # Widened exploration still preserves a real, executable target;
            # no map label is inferred from failure to find a readable pose.
            best = int(np.argmax(gain-.02*np.asarray(costs)))
        self._route = self._short_waypoints(observed, paths[best])
        self._final_rotation = candidates[best]
        self._target_indices = list(np.flatnonzero((readable[best] > 0) & pending))
        self._plan_votes = self._votes(rows)
        if self._target_indices:
            anchor = max(self._target_indices, key=lambda i: weights[i]*readable[best,i])
            self.target_face = self._keys[anchor][0]
            self.visits[self.target_face] += 1
        else:
            self.target_face = next(key[0] for i,key in enumerate(self._keys) if pending[i])
        self._started = now
        self._at_goal_since = None
        self._last_plan_time = now
        self._plan_serial += 1
        self.stats['replans'] = max(0, self._plan_serial-1)
        self._progress_goal = None
        self._no_descent_since = None
        self.planning_time_ms = (time.perf_counter()-plan_started)*1000.

    def choose(self, rotation, cells, response_axes, quality=1.0,
               observed_rotation=None, tvec=None, elapsed=None, semantic_revision=None,
               anchor_feedback=None):
        rotation = np.asarray(rotation, float)
        observed = rotation if observed_rotation is None else np.asarray(observed_rotation, float)
        now = time.monotonic() if elapsed is None else float(elapsed)
        if (rotation.shape != (3,3) or observed.shape != (3,3)
                or not np.isfinite(rotation).all() or not np.isfinite(observed).all()
                or not math.isfinite(float(quality)) or quality < .25):
            return dict(direction=None, phase='recover', reason='pose_not_reliable')
        if tvec is not None:
            value = np.asarray(tvec, float)
            if value.size == 3 and np.isfinite(value).all():
                self.tvec = value.reshape(3,1)
        axes = {key: np.asarray(value, float) for key, value in response_axes.items()
                if key in (0,1) and np.asarray(value).shape == (3,)
                and np.isfinite(value).all() and np.linalg.norm(value) > 1e-7}
        if len(axes) < 2:
            return self._calibrator.choose(rotation, cells, axes, quality, observed)
        rows = self._rows(cells)
        feedback = anchor_feedback or {}
        if self.recognition_goal == 'targets':
            self._current_target_deficits = self._candidate_deficits(rows, feedback)
            if self._motion_pose is not None:
                duration = now-self._motion_time
                movement = _pose_angle(observed, self._motion_pose)
                if .03 <= duration <= .7 and math.radians(.4) <= movement <= math.radians(20.):
                    measured = min(math.radians(70.), movement/duration)
                    self._observed_motion_rate = (measured if self._observed_motion_rate is None
                        else .8*self._observed_motion_rate+.2*measured)
            self._motion_pose, self._motion_time = observed.copy(), now
        votes = self._votes(rows)
        if self._last_gain is None:
            self._last_gain = np.full(54, now, dtype=float)
            self._last_attempt = np.full(54, now-6., dtype=float)
        self._last_gain[votes > self._latest_votes] = now
        self._latest_votes = votes
        complete, seen = self._objective(rows)
        six_faces = all(any(seen[i] for i, key in enumerate(self._keys) if key[0] == face)
                        for face in _NORMALS)
        objective_covered = all(complete) and (self.recognition_goal != 'targets' or six_faces)
        if objective_covered and self.recognition_goal != 'targets':
            return dict(direction=None, phase='complete', reason='all_cells_confirmed')
        anchor_age = feedback.get('glyph_anchor_age_sec', 0.) or 0.
        anchor_at = feedback.get('glyph_anchor_at')
        diagnostic = feedback.get('refine_diagnostic')
        if diagnostic is not None:
            source_frame = diagnostic.get('source_frame_id')
            if source_frame != self._anchor_feedback_frame:
                self._anchor_feedback_frame = source_frame
                self._anchor_failed_frames = 0 if diagnostic.get('renewed') else self._anchor_failed_frames+1
        elif anchor_feedback is not None:
            return dict(direction=None,phase='observe',reason='await_first_current_glyph_evidence')
        if (self._anchor_recovery is None and anchor_age >= 1.25 and
                feedback.get('glyph_anchor_rotation') is not None and
                (anchor_feedback is None or self._anchor_failed_frames >= 2)):
            if self._anchor_recovery_count >= 6:
                return dict(direction=None, phase='recover', reason='anchor_recovery_budget_exhausted')
            goal = np.asarray(feedback['glyph_anchor_rotation'], float)
            if goal.shape == (3,3) and np.isfinite(goal).all():
                self._anchor_recovery_count += 1
                self.stats['anchor_recoveries'] = self._anchor_recovery_count
                self._anchor_recovery = dict(started=now, anchor_at=anchor_at,
                    goal=goal.copy(), anchor=goal.copy(), tried=[goal.copy()],
                    attempt=0, at_goal=None)
                self._route = []
                self._at_goal_since = None
        if self._anchor_recovery is None and anchor_age >= 2.:
            return dict(direction=None,phase='observe',reason='await_fresh_glyph_evidence')
        recovery = self._anchor_recovery
        if recovery is not None:
            renewed = (anchor_at is not None and recovery['anchor_at'] is not None and
                       anchor_at > recovery['anchor_at'] and anchor_age < .9)
            if renewed:
                self._anchor_recovery = None
                self._route = []
                self._pending_replan_reason = None
            else:
                if now-recovery['started'] > 4.:
                    return dict(direction=None, phase='recover', reason='anchor_recovery_failed')
                if _pose_angle(recovery['goal'], observed) <= math.radians(4.):
                    if recovery['at_goal'] is None:
                        recovery['at_goal'] = now
                    if now-recovery['at_goal'] > .65:
                        recovery['attempt'] += 1
                        if recovery['attempt'] > 2:
                            return dict(direction=None, phase='recover', reason='anchor_recovery_no_current_evidence')
                        # Search near the last verified pose, not an arbitrary
                        # reset. Only new current-frame glyphs can finish recovery.
                        goal = self._recovery_search_goal(recovery['anchor'], axes, rows, recovery['tried'])
                        if goal is None:
                            return dict(direction=None, phase='recover', reason='anchor_recovery_no_current_evidence')
                        recovery['goal'] = goal
                        recovery['tried'].append(goal.copy())
                        recovery['at_goal'] = None
                    else:
                        return dict(direction=None, phase='observe', reason='await_current_anchor_evidence')
                self._route = [recovery['goal']]
                self._final_rotation = recovery['goal']
                self._target_indices = []
                self.target_face = max(_NORMALS, key=lambda face: float(-(observed @ _NORMALS[face])[2]))
                self._started = now
                self._progress_goal = None
                self._pending_replan_reason = None
        if objective_covered and self._anchor_recovery is None:
            # Root's public readiness remains the only success authority. A
            # current pose can finish its queued semantics; stale anchors above
            # still take the existing bounded recovery route instead of waiting.
            return dict(direction=None, phase='observe', reason='target_objective_covered_await_readiness')
        if self._route and self._pending_replan_reason is not None:
            self._feedback(rows, self._final_rotation, now, self._pending_replan_reason, True)
            self._route = []
        self._pending_replan_reason = None
        # Never advance from the controller's extrapolated pose: only a tracked
        # observation can satisfy a waypoint and start semantic dwell time.
        if self._route and self._anchor_recovery is None:
            observed_angle = _pose_angle(self._route[0], observed)
            # Once stopped for semantics, tolerate tracking jitter before moving
            # again. This concerns the camera target, never label confidence.
            tolerance = 6. if self._at_goal_since is not None else self.goal_tolerance_deg
            reached = observed_angle <= math.radians(tolerance)
            if reached and quality >= .5:
                if len(self._route) > 1:
                    self._route.pop(0)
                    # Only a fresh tracked arrival renews the next approach
                    # deadline. Repeated calls or extrapolated poses cannot.
                    self._started = now
                else:
                    if self._at_goal_since is None:
                        self._at_goal_since = now
                        self._arrival_revision = semantic_revision
                    fresh_semantics = (semantic_revision is None or
                                       semantic_revision != self._arrival_revision)
                    if now-self._at_goal_since >= .25 and fresh_semantics:
                        self._feedback(rows, observed, now, 'readable_pose_no_new_votes')
                        self._reached.append(observed.copy())
                        self._route = []
                    elif now-self._at_goal_since > .6:
                        self.insufficient.append(dict(face=self.target_face,
                                                      reason='semantic_dwell_timeout'))
                        self._failed_views.append(observed.copy())
                        self._feedback(rows, observed, now, 'semantic_dwell_timeout', True)
                        self._route = []
                    else:
                        return dict(direction=None, phase='observe', reason='await_semantic_frame',
                                    target_face=self.target_face,
                                    replans=self.stats['replans'], no_descent=self.stats['no_descent'],
                                    goal_rotation=self._route[0].tolist(),
                                    goal_normal=(self._route[0] @ _NORMALS[self.target_face]).tolist())
            elif self._at_goal_since is not None:
                self._at_goal_since = None
                self._progress_goal = None
            if self._route and self._at_goal_since is None:
                goal_changed = (self._progress_goal is None or
                                not np.array_equal(self._progress_goal, self._route[0]))
                current_angle = _pose_angle(self._route[0], observed)
                target_votes = float(votes[self._target_indices].sum())
                if goal_changed:
                    self._progress_goal = self._route[0].copy()
                    self._progress_at = now
                    self._progress_angle = current_angle
                    self._progress_votes = target_votes
                elif (self._progress_angle-current_angle >= math.radians(.4)
                      or target_votes > self._progress_votes):
                    self._progress_at = now
                    self._progress_angle = current_angle
                    self._progress_votes = target_votes
                elif now-self._progress_at >= .5:
                    self._feedback(rows, self._final_rotation, now, 'observed_pose_stagnant', True)
                    self.stats['stagnant_replans'] += 1
                    self._route = []
            if self._route and now-self._started > 7.:
                self.insufficient.append(dict(face=self.target_face, reason='pose_approach_timeout',
                                              target_cells=[self._keys[i] for i in self._target_indices]))
                self._feedback(rows, self._final_rotation, now, 'pose_approach_timeout', True)
                self._route = []
            elif self._route and self._target_indices and all(complete[i] for i in self._target_indices) and (
                    self.recognition_goal != 'targets' or
                    all(any(seen[j] for j, key in enumerate(self._keys) if key[0] == self._keys[i][0])
                        for i in self._target_indices)):
                self._route = []
        if not self._route:
            self._plan(observed, rows, axes, now)
        goal = self._route[0]
        error = _pose_vector(goal, rotation)
        remaining = float(np.linalg.norm(error))
        degrees = math.degrees(remaining)
        cap = self.max_step_px if degrees > 60 else self.base_step_px if degrees > 25 else max(30., self.base_step_px*degrees/25.)
        cap *= max(.35, min(1., quality))
        directions = [(1.,0.),(-1.,0.),(0.,1.),(0.,-1.)]
        pixels = np.linalg.lstsq(np.column_stack((axes[0],axes[1])), error, rcond=None)[0]
        if self.allow_diagonal and np.linalg.norm(pixels) > 1.:
            directions.append(tuple(pixels/np.linalg.norm(pixels)))
        options = []
        for direction in directions:
            rate = axes[0]*direction[0]+axes[1]*direction[1]
            gain = max(1e-7, np.linalg.norm(rate))
            # Full SO(3) error may contain roll outside the two input axes'
            # instantaneous span. Its magnitude must not force a 20 px motion.
            # The projected least-squares amplitude can be much smaller.
            projected_pixels = float(np.dot(error,rate)/max(1e-14,np.dot(rate,rate)))
            steps = [min(cap, max(2., remaining*.90/gain))]
            if projected_pixels > 0:
                steps.append(min(cap, max(2., projected_pixels*.90)))
            for step in steps:
                for fraction in (1., .5, .25):
                    distance = max(2, int(round(step*fraction)))
                    predicted = _matrix(rate*distance) @ rotation
                    options.append((_pose_angle(goal,predicted),direction != self._last_direction,
                                    -distance,direction,distance,np.linalg.norm(rate*distance)))
        after, _, _, direction, distance, movement = min(options, key=lambda x: (x[0],x[1],x[2]))
        if remaining-after < math.radians(.10):
            if self._no_descent_since is None:
                self._no_descent_since = now
                self.stats['no_descent'] += 1
            # First stop and accept another tracked frame. The observed-progress
            # watchdog above, not an extrapolated residual, decides to replan.
            return dict(direction=None, phase='observe', reason='no_descent',
                        target_face=self.target_face, target_angle_deg=degrees,
                        predicted_remaining_deg=math.degrees(after),
                        goal_rotation=goal.tolist(), goal_normal=(goal @ _NORMALS[self.target_face]).tolist(),
                        plan_revision=self._plan_serial, replans=self.stats['replans'],
                        no_descent=self.stats['no_descent'])
        self._no_descent_since = None
        self._last_direction = direction
        return dict(direction=direction, distance_px=float(distance), target_face=self.target_face,
                    target_angle_deg=degrees, predicted_angle_deg=math.degrees(movement),
                    predicted_remaining_deg=math.degrees(after), goal_rotation=goal.tolist(),
                    final_goal_rotation=self._final_rotation.tolist(),
                    goal_normal=(goal @ _NORMALS[self.target_face]).tolist(),
                    target_cells=[dict(zip(('face','row','col'),self._keys[i])) for i in self._target_indices],
                    phase='anchor_recovery' if self._anchor_recovery is not None else 'coverage' if any(not r.get('evidence') and not r.get('occupant_evidence_counts')
                        and r.get('occupant_status') != 'confirmed' for r in rows) else 'detail',
                    plan_revision=self._plan_serial, planning_time_ms=self.planning_time_ms,
                    replans=self.stats['replans'], no_descent=self.stats['no_descent'],
                    failed_pose_cells=sum(len(indices) for _,indices,_ in self._cell_failures),
                    reason='missing_cells_projected_readable')
