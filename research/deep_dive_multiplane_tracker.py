"""Bounded research bank of independently tracked, already bound cube faces.

No unseen-face discovery, entity votes, or full-cube physical reconstruction.
The caller must bind each RGB to its actual capture source and mask sprites.
"""
from copy import deepcopy
from itertools import combinations
import math

import cv2
import numpy as np

from plans.resonance_pc.src.actions._deep_dive_layout_vision import (
    BASES, DISTANCE, FACES, K, _angle, _point, _quad, _ui_mask)
from research.deep_dive_face_plane_tracker import fit_face_plane


class MultiPlaneTracker:
    """Track each face with its own image/cloud/R/T, never another face's T.

    ``source`` needs session_id, map_revision, generation, frame_time. The
    metadata is preserved verbatim; no source id/time is inferred. Source age
    and actual RGB/source binding remain the capture caller's responsibility.
    Default mode replaces each frame's feature bindings. The research variant
    explicitly uses retain_bindings=True, refill_below=200 and
    direct_check_interval=5; direct checks never select by desired orientation
    or smaller residual. Their pixels are current; their object/source binding
    remains the accepted historical keyframe.
    """
    def __init__(self, *, preferred_face='U', max_keyframes_per_face=2,
                 max_features_per_face=400, orientation_conflict_deg=3.,
                 max_pose_jump_deg=30., reseed=True, retain_bindings=False,
                 refill_below=200, direct_check_interval=0):
        if (preferred_face not in BASES or not 1 <= max_keyframes_per_face <= 3
                or not 25 <= max_features_per_face <= 600
                or not 0 < orientation_conflict_deg <= 15
                or not 0 < max_pose_jump_deg <= 30
                or type(refill_below) is not int or not 25 <= refill_below <= 600
                or type(direct_check_interval) is not int or not 0 <= direct_check_interval <= 100):
            raise ValueError('invalid_bank_configuration')
        self.preferred_face = preferred_face
        self.max_keyframes = int(max_keyframes_per_face)
        self.max_features = int(max_features_per_face)
        self.orientation_conflict_deg = float(orientation_conflict_deg)
        self.max_pose_jump_deg = float(max_pose_jump_deg)
        self.reseed = bool(reseed)
        self.retain_bindings = bool(retain_bindings)
        self.refill_below = int(refill_below)
        self.direct_check_interval = int(direct_check_interval)
        self._accepted_counts = {}
        self._direct_checks = {}
        self._banks = {}
        self._source = None
        self._current = {}
        self._snapshot = dict(status='not_bootstrapped', geometry_only=True,
                              shared_rotation=None, faces={})

    @staticmethod
    def _gray(rgb):
        if not isinstance(rgb, np.ndarray) or rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
            raise ValueError('invalid_rgb')
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        gray.setflags(write=False)
        return gray

    @staticmethod
    def _source_key(source):
        if not isinstance(source, dict):
            raise ValueError('source_not_mapping')
        for key in ('session_id', 'map_revision', 'generation'):
            if type(source.get(key)) is not int or source[key] < 0:
                raise ValueError('invalid_source_'+key)
        stamp = source.get('frame_time')
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError('invalid_source_frame_time')
        return source['session_id'], source['map_revision'], source['generation'], float(stamp)

    def _admit_source(self, source, bootstrap=False):
        key = self._source_key(source)
        if self._source is not None:
            prior = self._source_key(self._source)
            if key[:2] != prior[:2]:
                self._banks.clear()
                self._current.clear()
                self._accepted_counts.clear()
                self._direct_checks.clear()
                self._source = None
                if not bootstrap:
                    raise ValueError('source_context_changed_requires_bootstrap')
            elif key[2] <= prior[2] or key[3] <= prior[3]:
                self._current.clear()
                raise ValueError('source_not_advanced')
        self._source = deepcopy(source)
        self._current = {}

    @staticmethod
    def _projection(face, rotation, translation):
        rows = []
        normal = np.asarray(BASES[face][0], float)
        rv = cv2.Rodrigues(rotation)[0]
        camera = -(rotation.T @ translation.reshape(3))
        for row in range(3):
            for col in range(3):
                cell = dict(face=face, row=row, col=col)
                xyz = np.vstack((_quad(cell), _point(cell)))
                camera_xyz = xyz @ rotation.T + translation.reshape(1, 3)
                projected = cv2.projectPoints(xyz, rv, translation, K, None)[0].reshape(5, 2)
                to_camera = camera-_point(cell)
                cosine = float(normal @ to_camera/np.linalg.norm(to_camera))
                if np.any(camera_xyz[:, 2] <= 0) or cosine <= .05 or not np.isfinite(projected).all():
                    continue
                rows.append(dict(**cell, index=FACES.index(face)*9+row*3+col,
                    quad=projected[:4], centre=projected[4], cosine=cosine,
                    area=abs(float(cv2.contourArea(np.float32(projected[:4]))))))
        return rows

    def _fresh_seed(self, gray, face, fit, objects, pixels, mask_boxes):
        if not self.reseed:
            return objects[:self.max_features].copy(), pixels[:self.max_features].copy()
        rotation, translation = np.asarray(fit['rotation']), np.asarray(fit['translation']).reshape(3, 1)
        mask = np.zeros(gray.shape, np.uint8)
        for item in self._projection(face, rotation, translation):
            quad, center = item['quad'], item['centre']
            cv2.fillConvexPoly(mask, np.int32(center+(quad-center)*.78), 255)
        mask &= _ui_mask()
        for box in mask_boxes:
            x, y, width, height = map(int, box)
            cv2.rectangle(mask, (max(0, x), max(0, y)), (x+width, y+height), 0, -1)
        points = cv2.goodFeaturesToTrack(gray, maxCorners=self.max_features, qualityLevel=.01,
                                       minDistance=7, mask=mask)
        if points is None or len(points) < 25:
            return objects[:self.max_features].copy(), pixels[:self.max_features].copy()
        points = points.reshape(-1, 2).astype(float)
        rays = np.column_stack(((points[:, 0]-K[0, 2])/K[0, 0],
                                (points[:, 1]-K[1, 2])/K[1, 1], np.ones(len(points)))) @ rotation
        camera = -(rotation.T @ translation).reshape(3)
        normal = np.asarray(BASES[face][0], float)
        denominator = rays @ normal
        valid = np.abs(denominator) > 1e-8
        lengths = np.zeros(len(points))
        lengths[valid] = (DISTANCE-normal @ camera)/denominator[valid]
        valid &= lengths > 0
        generated = camera+lengths[:, None]*rays
        # These are observed features on an already bound local plane. They do
        # not bind a new face or prove glyph/cell identity.
        if np.count_nonzero(valid) < 25:
            return objects[:self.max_features].copy(), pixels[:self.max_features].copy()
        return generated[valid].copy(), points[valid].astype(np.float32)

    def _seed(self, gray, face, fit, objects, pixels, mask_boxes):
        if not self.retain_bindings:
            return self._fresh_seed(gray, face, fit, objects, pixels, mask_boxes)
        keep_objects = objects[:self.max_features].copy()
        keep_pixels = pixels[:self.max_features].copy()
        if not self.reseed or len(keep_objects) >= min(self.refill_below, self.max_features):
            return keep_objects, keep_pixels
        generated, candidates = self._fresh_seed(gray, face, fit, objects, pixels, mask_boxes)
        extra_objects, extra_pixels = [], []
        for obj, pixel in zip(generated, candidates):
            existing = (np.vstack((keep_pixels, np.asarray(extra_pixels).reshape(-1, 2)))
                        if extra_pixels else keep_pixels)
            if len(existing) and np.min(np.linalg.norm(existing-pixel, axis=1)) < 7.:
                continue
            extra_objects.append(obj)
            extra_pixels.append(pixel)
            if len(keep_objects)+len(extra_objects) >= self.max_features:
                break
        if not extra_objects:
            return keep_objects, keep_pixels
        return np.vstack((keep_objects, extra_objects)), np.vstack((keep_pixels, extra_pixels)).astype(np.float32)

    def _accept(self, face, gray, source, fit, all_objects, all_pixels, mask_boxes):
        indices = fit['inlier_indices']
        actual_objects = np.asarray(all_objects, float)[indices].copy()
        actual_pixels = np.asarray(all_pixels, np.float32)[indices].copy()
        objects, pixels = self._seed(gray, face, fit, actual_objects, actual_pixels, mask_boxes)
        prior = self._banks.get(face)
        keyframes = [] if prior is None else list(prior['keyframes'])
        if prior is not None and (not keyframes or _angle(
                np.asarray(fit['rotation']), keyframes[-1]['rotation']) >= 10.):
            keyframes.append({key: prior[key] for key in ('gray', 'objects', 'pixels', 'rotation', 'translation', 'source')})
            keyframes = keyframes[-self.max_keyframes:]
        self._banks[face] = dict(gray=gray, objects=objects, pixels=pixels,
            rotation=np.asarray(fit['rotation']), translation=np.asarray(fit['translation']).reshape(3, 1),
            source=deepcopy(source), fit=deepcopy(fit), keyframes=keyframes)
        self._current[face] = dict(source=deepcopy(source), fit=deepcopy(fit),
            rotation=deepcopy(fit['rotation']), translation=deepcopy(fit['translation']),
            indexed_features=[dict(object=point.tolist(), pixel=pixel.tolist())
                              for point, pixel in zip(actual_objects, actual_pixels)],
            next_feature_count=len(objects), current=True, geometry_only=True,
            object_binding=('retain_accepted_objects_and_same_face_refill' if self.retain_bindings
                            else 'reset_identity_then_same_face_plane_reprojection'))

    def bootstrap(self, rgb, source, objects, pixels, rotation, translation, *, mask_boxes=()):
        self._current = {}
        self._direct_checks = {}
        try:
            gray = self._gray(rgb)
            self._admit_source(source, bootstrap=True)
        except ValueError:
            self._publish({})
            raise
        self._banks.clear()
        self._accepted_counts.clear()
        reports = {}
        for face in ('U', 'R', 'F'):
            fit = fit_face_plane(objects, pixels, face, rotation, translation, limit=self.max_pose_jump_deg)
            reports[face] = fit
            if fit['success']:
                self._accept(face, gray, source, fit, objects, pixels, mask_boxes)
                self._accepted_counts[face] = 1
        return self._publish(reports)

    @staticmethod
    def _flow(candidate, gray):
        points = np.asarray(candidate['pixels'], np.float32)
        forward, status, _ = cv2.calcOpticalFlowPyrLK(candidate['gray'], gray, points, None,
            winSize=(25, 25), maxLevel=3, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 25, .02))
        if forward is None:
            return None
        backward, reverse, _ = cv2.calcOpticalFlowPyrLK(gray, candidate['gray'], forward, None,
            winSize=(25, 25), maxLevel=3)
        if backward is None:
            return None
        valid = ((status.ravel() > 0) & (reverse.ravel() > 0)
                 & (np.linalg.norm(backward-points, axis=1) < 1.5)
                 & np.isfinite(forward).all(axis=1))
        return candidate['objects'][valid], forward[valid]

    def update(self, rgb, source, *, mask_boxes=()):
        self._current = {}
        self._direct_checks = {}
        try:
            gray = self._gray(rgb)
            self._admit_source(source)
        except ValueError:
            self._publish({})
            raise
        reports = {}
        for face, bank in list(self._banks.items()):
            reports[face] = dict(success=False, reason='no_current_tracked_plane')
            for candidate in [bank]+list(reversed(bank['keyframes'])):
                tracked = self._flow(candidate, gray)
                if tracked is None:
                    continue
                objects, pixels = tracked
                fit = fit_face_plane(objects, pixels, face, bank['rotation'], bank['translation'],
                                     limit=self.max_pose_jump_deg)
                reports[face] = fit
                if fit['success']:
                    self._accepted_counts[face] = self._accepted_counts.get(face, 1)+1
                    fit, objects, pixels = self._periodic_direct(
                        face, bank, gray, source, fit, objects, pixels)
                    reports[face] = fit
                    self._accept(face, gray, source, fit, objects, pixels, mask_boxes)
                    break
        return self._publish(reports)

    def _periodic_direct(self, face, bank, gray, source, fit, objects, pixels):
        if (not self.retain_bindings or not self.direct_check_interval
                or self._accepted_counts[face] % self.direct_check_interval):
            return fit, objects, pixels
        report = dict(current_source=deepcopy(source), keyframe_source=None,
                      selected=False, reason='no_bound_keyframe', chained_fit=deepcopy(fit))
        self._direct_checks[face] = report
        if not bank['keyframes']:
            return fit, objects, pixels
        anchor = bank['keyframes'][-1]
        report['keyframe_source'] = deepcopy(anchor['source'])
        tracked = self._flow(anchor, gray)
        if tracked is None:
            report['reason'] = 'direct_flow_failed'
            return fit, objects, pixels
        direct_objects, direct_pixels = tracked
        direct = fit_face_plane(direct_objects, direct_pixels, face,
            bank['rotation'], bank['translation'], limit=self.max_pose_jump_deg)
        report['direct_fit'] = deepcopy(direct)
        if not direct['success']:
            report['reason'] = direct['reason']
            return fit, objects, pixels
        report.update(selected=True, reason='direct_all_original_gates_passed',
            pose_difference_deg=_angle(np.asarray(fit['rotation']), np.asarray(direct['rotation'])))
        return direct, direct_objects, direct_pixels

    def _publish(self, reports):
        pairwise = []
        for left, right in combinations(self._current, 2):
            pairwise.append(dict(faces=[left, right], angle_deg=_angle(
                np.asarray(self._current[left]['rotation']), np.asarray(self._current[right]['rotation']))))
        conflict = any(pair['angle_deg'] > self.orientation_conflict_deg for pair in pairwise)
        selected = None
        if self._current and not conflict:
            selected = (self.preferred_face if self.preferred_face in self._current else
                        max(self._current, key=lambda face: self._current[face]['fit']['inliers']))
        faces = {}
        for face, bank in self._banks.items():
            faces[face] = (deepcopy(self._current[face]) if face in self._current else
                dict(current=False, history_source=deepcopy(bank['source']),
                     history_rotation=bank['rotation'].tolist(), history_translation=bank['translation'].reshape(3).tolist(),
                     history_feature_count=len(bank['pixels']), reason=reports.get(face, {}).get('reason', 'not_current')))
            faces[face]['keyframe_count'] = len(bank['keyframes'])
        self._snapshot = dict(status='orientation_conflict' if conflict else 'geometry_only' if selected else 'no_current_support',
            geometry_only=True, source=deepcopy(self._source), selected_face=selected,
            shared_rotation=None if selected is None else deepcopy(self._current[selected]['rotation']),
            shared_rotation_method='one_actual_face_no_averaging', shared_support_faces=list(self._current),
            orientation_conflict_deg=self.orientation_conflict_deg, orientation_comparisons=pairwise,
            faces=faces, fit_reports=deepcopy(reports), unseen_face_discovery=False,
            binding_policy='retain_and_refill' if self.retain_bindings else 'replace_each_frame',
            refill_below=self.refill_below, direct_check_interval=self.direct_check_interval,
            current_measurement_counts=deepcopy(self._accepted_counts), direct_checks=deepcopy(self._direct_checks),
            full_cube_coordinates_valid=False, target_evidence=False)
        return self.snapshot()

    def snapshot(self):
        return deepcopy(self._snapshot)

    def visible(self, face=None):
        """Current local model projections only; never use historical sources."""
        face = self.preferred_face if face is None else face
        if face not in self._current or self._snapshot.get('status') == 'orientation_conflict':
            return []
        pose = self._current[face]
        rows = self._projection(face, np.asarray(pose['rotation']), np.asarray(pose['translation']).reshape(3, 1))
        return [dict(item, source=deepcopy(pose['source']), projection_kind='local_face_plane_geometry_only') for item in rows]

    def local_visible(self, face):
        """Explicit local camera-navigation quads despite shared-R conflict.

        This face must independently pass every fit gate in the current source.
        No historical source or another face's T is used. These quads cannot
        authorize cross-face competition, full-cube reconstruction or entities.
        The original ``visible`` still revokes projections on global conflict.
        """
        if face not in self._current:
            return []
        pose = self._current[face]
        if not pose.get('current') or not pose.get('fit', {}).get('success'):
            return []
        shared_valid = (self._snapshot.get('status') == 'geometry_only'
                        and self._snapshot.get('shared_rotation') is not None)
        rows = self._projection(face, np.asarray(pose['rotation']),
                                np.asarray(pose['translation']).reshape(3, 1))
        return [dict(item, source=deepcopy(pose['source']),
            shared_orientation_valid=shared_valid,
            projection_kind=('local_face_plane_geometry_only' if shared_valid else
                             'local_face_geometry_shared_orientation_unknown'),
            full_cube_coordinates_valid=False, target_evidence=False) for item in rows]
