"""Opt-in vertex itinerary; geometry schedules views, never grants votes.

The default policies are unchanged. This prototype needs a real known-multiface
atlas and may safely refuse a route. Bootstrap reuses the cells controller;
navigation never creates unknown anchor labels.
"""
from __future__ import annotations

import math
import time
from copy import deepcopy

import numpy as np

from ._deep_dive_scan_policy import (
    CellScanPolicy, MixedFaceScanPolicy, _NORMALS, _matrix, _pose_angle, _pose_vector, _unit,
)


def _view(rotation, translation):
    return _unit(-np.asarray(rotation).T @ np.asarray(translation).reshape(3))


def _between(left, right):
    left, right = _unit(left), _unit(right)
    angle = math.acos(float(np.clip(np.dot(left, right), -1., 1.)))
    axis = np.cross(left, right)
    if np.linalg.norm(axis) < 1e-8:
        if angle < 1e-8:
            return np.eye(3)
        axis = np.cross(left, np.eye(3)[int(np.argmin(np.abs(left)))])
    return _matrix(_unit(axis)*angle)


def _samples(observed, path):
    """Dense planning checkpoints are predictions, never source evidence."""
    result = [observed]
    previous = observed
    for goal in path:
        delta = _pose_vector(goal, previous)
        count = max(1, int(math.ceil(np.linalg.norm(delta)/math.radians(10.))))
        result.extend(_matrix(delta*j/count) @ previous for j in range(1, count+1))
        previous = goal
    return np.asarray(result)


def _bounded_fit(residual, initial, limit):
    """Small deterministic two-parameter solve; no optional runtime dependency."""
    values = np.clip(np.asarray(initial, float), -limit, limit)
    for _ in range(24):
        error = residual(values)
        if not np.isfinite(error).all():
            return None
        if np.linalg.norm(error) < 1e-6:
            break
        epsilon = 1e-5
        jacobian = np.column_stack([
            (residual(values + np.eye(2)[axis]*epsilon)-error)/epsilon
            for axis in range(2)])
        step = np.linalg.solve(jacobian.T @ jacobian + np.eye(2)*1e-5,
                               -jacobian.T @ error)
        step *= min(1., .5/max(float(np.linalg.norm(step)), 1e-12))
        improved = False
        for scale in (1., .5, .25, .125):
            candidate = np.clip(values + step*scale, -limit, limit)
            if np.linalg.norm(residual(candidate)) < np.linalg.norm(error)-1e-10:
                values, improved = candidate, True
                break
        if not improved:
            break
    return values


class VertexScanPolicy(MixedFaceScanPolicy):
    """Four connected physical vertices, current support handoff, local pairs.

    All motion/recovery/time limits belong to the inherited controller and host.
    Completing this itinerary never means target readiness or scan success.
    """

    def __init__(self, base_step_px=300, max_step_px=450, *,
                 recognition_goal='targets', expected_inspirations=None):
        super().__init__(base_step_px, max_step_px, recognition_goal=recognition_goal,
                         expected_inspirations=expected_inspirations)
        self._vertex_context = None
        self._vertices = []
        self._vertex_faces = []
        self._vertex_index = 0
        self._vertex_views = {i: [] for i in range(4)}
        self._pair_goal_base = None
        self._vertex_block = None
        self._vertex_retry_key = None
        self._bootstrap_active = True
        self._pair_attempts = {i: [] for i in range(4)}
        self._vertex_failure_reason = None
        self._vertex_failure_evidence = None

    def summary(self):
        result = super().summary()
        result['vertex'] = dict(index=self._vertex_index,
            bootstrap_active=self._bootstrap_active,
            cells_fallback=self._vertex_failure_reason is not None,
            failure_reason=self._vertex_failure_reason,
            failure_evidence=deepcopy(self._vertex_failure_evidence),
            exhausted=self._vertex_index == 4, blocked_reason=self._vertex_block,
            physical_directions=[v.tolist() for v in self._vertices],
            faces=[list(faces) for faces in self._vertex_faces],
            actual_views={str(i): [dict(frame_id=row['frame_id'], frame_time=row['frame_time'])
                                    for row in values] for i, values in self._vertex_views.items()},
            completion_authority='public_targets_readiness_only')
        return result

    def _proof(self):
        source = self._support_current
        feedback = self._mixed_feedback
        age = feedback.get('glyph_anchor_age_sec')
        if (source is None or type(age) not in (int, float) or not math.isfinite(age)
                or age >= .9 or source['context'] != self._vertex_context):
            return None
        known = source.get('known_faces') or {}
        if sum(known.values()) < 6 or sum(count >= 2 for count in known.values()) < 2:
            return None
        return source

    def _initialize(self, observed, basis):
        if self._vertex_failure_reason is not None:
            return  # Once abandoned, navigation cannot restart in this run.
        self._bootstrap_active = False
        direction = _view(observed, self.tvec)
        signs = np.where(direction >= 0, 1., -1.)
        vertices = [signs.copy()]
        # Adjacent Gray path: bring the opposite Y face in first, never aim at D.
        for axis in (1, 0, 2):
            signs = signs.copy()
            signs[axis] *= -1
            vertices.append(signs)
        self._vertices = [basis @ (sign/math.sqrt(3.)) for sign in vertices]
        self._vertex_faces = [tuple(face for face, normal in _NORMALS.items()
                                   if np.dot(normal, sign) > 0) for sign in vertices]

    def _reset_vertex(self, context):
        self._vertex_context = context
        self._vertices, self._vertex_faces = [], []
        self._vertex_index = 0
        self._vertex_views = {i: [] for i in range(4)}
        self._pair_goal_base = None
        self._vertex_block = None
        self._vertex_retry_key = None
        self._bootstrap_active = self._vertex_failure_reason is None
        self._pair_attempts = {i: [] for i in range(4)}
        self._route = []
        self._local_task = self._local_goal = None
        self._local_cooldowns = {}

    def _arrival_support(self, source, index):
        """New accepted pixels allow collection, never known-face handoff."""
        faces = self._vertex_faces[index]
        if any(source['faces'].get(face, 0) < 2 for face in faces):
            return None
        known = source['known_faces']
        if index:
            common = tuple(face for face in faces if face in self._vertex_faces[index-1])
        else:
            if all(known.get(face, 0) >= 2 for face in faces):
                return faces  # No new incoming face: preserve all known support.
            common = tuple(sorted(faces, key=lambda face: known.get(face, 0), reverse=True)[:2])
        if any(known.get(face, 0) < 2 for face in common) or sum(known.get(face, 0) for face in common) < 6:
            return None
        return common

    def _ingest(self, observed, basis):
        if self._vertex_failure_reason is not None:
            return
        source = self._proof()
        index = self._vertex_index
        if source is None or not self._vertices or index >= 4:
            return
        actual = source['base_rotation'] @ basis
        if _pose_angle(actual, observed) > math.radians(4.):
            return  # A fresh older source and a new tracked pose are not arrival.
        common = self._arrival_support(source, index)
        if common is None:
            return
        saved = self._vertex_views[index]
        if saved and (source['frame_id'] == saved[-1]['frame_id']
                      or source['frame_time'] <= saved[-1]['frame_time']):
            return
        if not saved:
            goal = basis.T @ self._vertices[index]
            if math.acos(float(np.clip(np.dot(_view(actual, self.tvec), goal), -1., 1.))) > math.radians(6.):
                return
        else:
            if (self._pair_goal_base is None
                    or _pose_angle(actual, self._pair_goal_base @ basis) > math.radians(4.)
                    or _pose_angle(source['base_rotation'], saved[0]['base_rotation']) < math.radians(8.)):
                return
            if any(source['known_faces'].get(face, 0) < 2 for face in self._vertex_faces[index]):
                return  # Collect a new face, but only real known pixels permit handoff.
        saved.append(dict(frame_id=source['frame_id'], frame_time=source['frame_time'],
                          base_rotation=source['base_rotation'].copy(), common_faces=common))
        if len(saved) >= 2:
            self._vertex_index += 1
        self._pair_goal_base = None
        self._route = []
        self._at_goal_since = None

    def _hard_route(self, observed, path, rows):
        _, valid = self._anchor_support(_samples(observed, path), rows)
        return bool(valid.all())

    def _paths_to_vertex(self, observed, axes, target):
        """Bounded two-axis executable candidates, with free endpoint roll."""
        goal_pose = observed @ _between(target, _view(observed, self.tvec))
        error = _pose_vector(goal_pose, observed)
        result = []
        limit = math.radians(110.)  # Same bounded axes family as the base policy.
        for first, second in ((0, 1), (1, 0)):
            a, b = _unit(axes[first]), _unit(axes[second])
            seed = np.clip(np.linalg.lstsq(np.column_stack((a, b)), error, rcond=None)[0],
                           -limit*.95, limit*.95)

            def final(values):
                return _matrix(b*values[1]) @ _matrix(a*values[0]) @ observed

            for initial in (seed, np.zeros(2)):
                values = _bounded_fit(lambda values: _view(final(values), self.tvec)-target,
                                      initial, limit)
                if values is None:
                    continue
                destination = final(values)
                angle = math.acos(float(np.clip(np.dot(_view(destination, self.tvec), target), -1., 1.)))
                if angle > math.radians(6.):
                    continue
                intermediate = _matrix(a*values[0]) @ observed
                path = ([intermediate] if abs(values[0]) > .001 else []) + [destination]
                result.append((path, float(np.abs(values).sum())))
        return result

    def _hold(self, observed, now, reason):
        self._vertex_block = reason
        self._route = [observed.copy()]
        self._final_rotation = observed.copy()
        self._target_indices = []
        self.target_face = max(_NORMALS, key=lambda f: float(-(observed @ _NORMALS[f])[2]))
        self._started, self._at_goal_since = now, None

    def _fallback_to_cells(self, reason):
        """Abandon scheduling only; the host clock and evidence stay intact."""
        if self._vertex_failure_reason is None:
            self._vertex_failure_reason = reason
            self._vertex_failure_evidence = dict(index=self._vertex_index,
                context=self._vertex_context,
                actual_views={str(i): [dict(frame_id=row['frame_id'], frame_time=row['frame_time'])
                    for row in entries] for i, entries in self._vertex_views.items()})
        self._bootstrap_active = False
        self._vertex_block = None
        self._mixed_state = 'vertex_cells_fallback'
        self._coverage_face = None
        self._local_task = self._local_goal = None
        self._local_cooldowns = {}
        self._route = []
        self._at_goal_since = None

    def _install(self, observed, path, rows, now):
        self._vertex_block = None
        self._route = self._short_waypoints(observed, path)
        self._final_rotation = path[-1]
        # Navigation ledger cannot grant or consume atlas votes.
        self._target_indices = []
        self.target_face = max(_NORMALS, key=lambda f: float(-(self._final_rotation @ _NORMALS[f])[2]))
        self._plan_votes = self._votes(rows)
        self._started, self._at_goal_since = now, None
        self._progress_goal = self._no_descent_since = None
        self._plan_serial += 1
        self.stats['replans'] = max(0, self._plan_serial-1)

    def _pair_paths(self, observed, axes, rows, origin, required_faces):
        options = []
        for axis in (0, 1):
            for sign in (-1, 1):
                for degrees in (12., 16.):
                    goal = _matrix(_unit(axes[axis])*math.radians(sign*degrees)) @ origin
                    if not self._hard_route(observed, [goal], rows):
                        continue
                    readable = self._readability(np.asarray([goal]), rows, anchors=True)[0] > 0
                    if any(sum(readable[i] for i, key in enumerate(self._keys) if key[0] == face) < 2
                           for face in required_faces):
                        continue
                    options.append(([goal], _pose_angle(goal, observed)))
        return options

    def _set_local_route(self, observed, rows, axes, now):
        # Mixed's source-backed task/pair definitions are preserved; its soft
        # planning support is replaced by a hard known-multiface route gate.
        source = self._proof()
        if source is None:
            return self._hold(observed, now, 'vertex_local_current_known_support_missing')
        index = self._local_task['index']
        faces = () if index is None else (self._keys[index][0],)
        options = self._pair_paths(observed, axes, rows, self._local_task['origin'], faces)
        if not options:
            return self._hold(observed, now, 'vertex_local_no_supported_pair_path')
        path, _ = min(options, key=lambda item: item[1])
        self._local_goal = path[-1]
        self._local_attempts += 1
        self._install(observed, path, rows, now)

    def _plan(self, observed, rows, axes, now):
        if self.recognition_goal != 'targets':
            return super()._plan(observed, rows, axes, now)
        if self._vertex_failure_reason is not None:
            return CellScanPolicy._plan(self, observed, rows, axes, now)
        if self._bootstrap_active:
            self._vertex_block = None
            self._mixed_state = 'vertex_cells_bootstrap'
            return CellScanPolicy._plan(self, observed, rows, axes, now)
        started = time.perf_counter()
        if self._local_task is not None:
            if self._local_attempts < 2 and now-self._local_started <= 6.:
                return self._set_local_route(observed, rows, axes, now)
            self._local_cooldowns[self._local_task['signature']] = now+4.
            self._local_task = self._local_goal = None
        source = self._proof()
        basis = self._rotation_basis(self._mixed_feedback.get('geometry_body_basis'))
        if source is None or basis is None:
            return self._hold(observed, now, 'vertex_current_known_multiface_support_missing')
        if not self._vertices:
            self._initialize(observed, basis)
        index = self._vertex_index
        if index >= 4:
            return self._hold(observed, now, 'vertex_itinerary_exhausted_await_public_readiness')
        if index > 0:
            shared = set(self._vertex_faces[index-1]) & set(self._vertex_faces[index])
            if (any(source['known_faces'].get(face, 0) < 2 for face in shared)
                    or sum(source['known_faces'].get(face, 0) for face in shared) < 6):
                return self._hold(observed, now, 'vertex_current_shared_face_handoff_missing')
        saved = self._vertex_views[index]
        if saved:
            attempts = self._pair_attempts[index]
            if len(attempts) >= 2:
                self._fallback_to_cells('vertex_pair_evidence_unresolved')
                return CellScanPolicy._plan(self, observed, rows, axes, now)
            options = self._pair_paths(observed, axes, rows, saved[0]['base_rotation'] @ basis,
                                       saved[0]['common_faces'])
            options = [(path, cost) for path, cost in options if all(
                _pose_angle(path[-1], old @ basis) >= math.radians(8.) for old in attempts)]
            if not options:
                return self._hold(observed, now, 'vertex_endpoint_no_supported_pair_path')
            path, _ = min(options, key=lambda item: item[1])
            self._pair_goal_base = path[-1] @ basis.T
            attempts.append(self._pair_goal_base.copy())
        else:
            target = basis.T @ self._vertices[index]
            options = [(path, cost) for path, cost in self._paths_to_vertex(observed, axes, target)
                       if self._hard_route(observed, path, rows)]
            if not options:
                return self._hold(observed, now, 'vertex_no_known_multiface_axis_route')
            path, _ = min(options, key=lambda item: item[1])
        self._install(observed, path, rows, now)
        self.planning_time_ms = (time.perf_counter()-started)*1000.

    def choose(self, rotation, cells, response_axes, quality=1., observed_rotation=None,
               tvec=None, elapsed=None, semantic_revision=None, anchor_feedback=None):
        if self.recognition_goal != 'targets':
            return super().choose(rotation, cells, response_axes, quality, observed_rotation,
                                  tvec, elapsed, semantic_revision, anchor_feedback)
        feedback = anchor_feedback or {}
        context = (feedback.get('session_id'), feedback.get('map_revision'))
        if context != self._vertex_context:
            self._reset_vertex(context)
        self._mixed_feedback = feedback
        if tvec is not None:
            translation = np.asarray(tvec, float)
            if translation.size == 3 and np.isfinite(translation).all():
                self.tvec = translation.reshape(3, 1)
        observed = self._rotation_basis(rotation if observed_rotation is None else observed_rotation)
        if observed is not None:
            basis, _ = self._support_feedback(feedback, self._rows(cells),
                                              time.monotonic() if elapsed is None else float(elapsed))
            source = self._proof()
            if (self._vertex_failure_reason is None and self._bootstrap_active
                    and basis is not None and source is not None
                    and quality >= .5 and self._anchor_recovery is None
                    and _pose_angle(source['base_rotation'] @ basis, observed) <= math.radians(4.)):
                self._initialize(observed, basis)
                self._route = []
                self._local_task = self._local_goal = None
                self._vertex_block = None
            if (self._vertex_failure_reason is None and basis is not None
                    and quality >= .5 and self._anchor_recovery is None):
                self._ingest(observed, basis)
            key = (context, feedback.get('semantic_revision'), feedback.get('glyph_anchor_frame_id'))
            if self._vertex_block and key != self._vertex_retry_key and self._anchor_recovery is None:
                self._route = []
            self._vertex_retry_key = key
        choose = (CellScanPolicy.choose if self._bootstrap_active or self._vertex_failure_reason is not None
                  else MixedFaceScanPolicy.choose)
        result = choose(self, rotation, cells, response_axes, quality, observed_rotation,
                        tvec, elapsed, semantic_revision, anchor_feedback)
        if self._vertex_block and self._anchor_recovery is None and result.get('reason') != 'mixed_targets_ready':
            result.update(direction=None, phase='observe', reason=self._vertex_block)
        result['vertex_scan'] = self.summary()['vertex']
        return result


__all__ = ['VertexScanPolicy']
