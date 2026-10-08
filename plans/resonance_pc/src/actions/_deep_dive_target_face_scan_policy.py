"""Finite adjacent-face camera pages; only the mapper can confirm entities."""
from copy import deepcopy
import math
import time

import numpy as np

from ._deep_dive_scan_policy import CellScanPolicy, _matrix, _pose_angle, _unit, _NORMALS
from ._deep_dive_target_first_scan_policy import TargetFirstCellScanPolicy
from ._deep_dive_target_framing import freeze_framing_interest, select_target_framing_pair


class TargetFacePageScanPolicy(TargetFirstCellScanPolicy):
    def __init__(self, *args, framing=True, page_cosine_range=(.78, .94), **kwargs):
        if type(framing) is not bool:
            raise TypeError('framing must be bool')
        if (not isinstance(page_cosine_range, (tuple, list)) or len(page_cosine_range) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       for v in page_cosine_range)
                or not 0 < page_cosine_range[0] < page_cosine_range[1] <= 1):
            raise ValueError('page_cosine_range must contain finite 0 < min < max <= 1')
        super().__init__(*args, **kwargs)
        self._target_framing_enabled = framing
        self._page_cosine_range = tuple(float(v) for v in page_cosine_range)
        self._page_context = self._page_basis = None
        self._page_order = []
        self._page_face = None
        self._page_results = {}
        self._page_views = []
        self._page_pair = None
        self._page_cursor = self._page_attempts = 0
        self._page_source_floor = None
        self._page_used_sources = set()
        self._page_navigation_route = False
        self._page_new_pair = False
        self._page_resume_required = False
        self._page_framing = {}
        self._page_pair_framing = ()
        self._page_framing_diagnostic = None
        self._page_framing_rejections = 0
        self._page_framing_rejected = []
        self._page_arrival_key = self._page_arrival_started = None

    def _assigned_interest_interrupts_route(self):
        # Finite pages take priority over navigation-only hints. Positive
        # local confirmation and the original readiness still take priority.
        return len(self._page_results) >= 6

    @staticmethod
    def _source_key(value):
        if not isinstance(value, dict):
            return None
        fields = ('session_id', 'map_revision', 'frame_id', 'generation', 'frame_time')
        if (any(type(value.get(k)) is not int or value[k] < 0 for k in fields[:4])
                or type(value.get('frame_time')) not in (int, float)
                or not math.isfinite(value['frame_time'])):
            return None
        return tuple(value[k] for k in fields)

    def _sync_page_basis(self, feedback):
        context = (feedback.get('session_id'), feedback.get('map_revision'))
        basis = self._rotation_basis(feedback.get('geometry_body_basis'))
        valid = (type(context[0]) is int and type(context[1]) is int
                 and context[0] >= 0 and context[1] >= 0 and basis is not None)
        if context != self._page_context or not valid:
            if self._page_navigation_route and self._anchor_recovery is None:
                self._route = []
            self._page_context = context
            self._page_order = []
            self._page_face = self._page_pair = None
            self._page_results = {}
            self._page_views = []
            self._page_cursor = self._page_attempts = 0
            self._page_source_floor = None
            self._page_arrival_key = self._page_arrival_started = None
            self._page_used_sources = set()
            self._page_navigation_route = False
            self._page_resume_required = False
            self._page_framing = {}
            self._page_pair_framing = ()
            self._page_framing_rejections = 0
            self._page_framing_rejected = []
        elif (self._page_basis is not None and self._page_navigation_route
              and self._local_task is None and self._anchor_recovery is None):
            change = self._page_basis.T @ basis
            self._route = [pose @ change for pose in self._route]
            if self._final_rotation is not None:
                self._final_rotation = self._final_rotation @ change
            if self._progress_goal is not None:
                self._progress_goal = self._progress_goal @ change
        self._page_basis = basis.copy() if valid else None

    def _reject_page_framing(self, interest, reason):
        self._page_framing_rejections += 1
        if len(self._page_framing_rejected) < 5:
            self._page_framing_rejected.append(dict(index=interest['index'],
                source_frame_id=interest['source_frame_id'],reason=reason))

    def _remember_assigned_interests(self, rows, feedback, now):
        before = set(self._assigned_interests)
        created = super()._remember_assigned_interests(rows, feedback, now)
        if not self._target_framing_enabled:
            return created
        for index in set(self._assigned_interests)-before:
            interest = self._assigned_interests[index]
            metadata = feedback.get('semantic_metadata') or {}
            pose = metadata.get('pose') or {}
            rotation = self._rotation_basis(pose.get('rotation'))
            try:
                translation = np.asarray(pose.get('tvec'),float).reshape(3)
            except (TypeError, ValueError):
                self._reject_page_framing(interest,'missing_original_translation')
                continue
            if rotation is None or not np.isfinite(translation).all() or translation[2] <= 0:
                self._reject_page_framing(interest,'invalid_original_pose')
                continue
            matches = [candidate for candidate in feedback.get('target_candidate_associations',()) or ()
                if isinstance(candidate,dict) and candidate.get('cell_index') == index
                and candidate.get('kind') == interest['kind']
                and (candidate.get('association_evidence') or {}).get('source_frame_id') == interest['source_frame_id']]
            if len(matches) != 1:
                self._reject_page_framing(interest,'ambiguous_original_candidate')
                continue
            camera = np.einsum('ij,nj->ni',rotation,self._quads[index])+translation
            if not np.isfinite(camera).all() or (camera[:,2] <= 0).any():
                self._reject_page_framing(interest,'invalid_original_projection_depth')
                continue
            quad = camera[:,:2]/camera[:,2,None]*self._K[0,0]+self._K[:2,2]
            center = rotation @ self._points[index]+translation
            cosine = -float((rotation @ self._normals[index]) @ center)/np.linalg.norm(center)
            projection = dict(session_id=metadata.get('session_id'),map_revision=metadata.get('map_revision'),
                frame_id=metadata.get('frame_id'),frame_time=metadata.get('frame_time'),
                cell_index=index,face=self._keys[index][0],rotation=rotation.tolist(),
                tvec=translation.tolist(),quad=quad.tolist(),cosine=float(cosine))
            record = freeze_framing_interest(interest,context=self._target_source_context,now=now,
                source_feedback=feedback,source_projection=projection,candidate=matches[0])
            if record.get('status') == 'valid':
                self._page_framing[index] = record
            else:
                self._reject_page_framing(interest,record.get('reason','invalid_original_framing'))
        return created

    def _active_page_framing(self, rows):
        if not self._target_framing_enabled:
            return []
        active = self._active_interests(rows,getattr(self,'_interest_now',0.))
        return [self._page_framing[i['index']] for i in active
            if i['index'] in self._page_framing and self._keys[i['index']][0] == self._page_face]

    def _page_planning_rows(self, rows, records):
        if not records:
            return rows
        result = deepcopy(rows)
        for record in records:
            interest,candidate,projection = record['interest'],record['candidate'],record['source_projection']
            index,kind = interest['index'],interest['kind']
            result[index]['occupant'] = kind
            result[index]['evidence'] = [dict(occupant=kind,confidence=candidate['confidence'],
                target_box=deepcopy(candidate['box']),target_point=deepcopy(candidate['point']),
                quad=deepcopy(projection['quad']),cosine=projection['cosine'],
                association_evidence=deepcopy(candidate['association_evidence']))]
        return result

    def _objective(self, rows):
        complete, seen = super()._objective(rows)
        if self.recognition_goal == 'targets' and len(self._page_results) < 6:
            # Internal camera work remains pending even when ordinary glyphs
            # are known. This bitmap never enters the atlas or readiness.
            complete = complete.copy()
            for i, (face, _, _) in enumerate(self._keys):
                if face not in self._page_results:
                    complete[i] = False
        return complete, seen

    def _record_opportunities(self, rows, feedback):
        super()._record_opportunities(rows, feedback)
        proof = self._page_source_proof(feedback)
        if proof is None:
            return
        key, base, rotation, cosine, faces = proof
        self._page_used_sources.add(key)
        self._page_views.append(dict(key=key, base_rotation=base.copy(),
            rotation=rotation.copy(), cosine=float(cosine), accepted_faces=dict(faces)))
        if len(self._page_views) >= 2:
            self._leave_page('two_actual_independent_sources')
        elif self._page_cursor + 1 < len(self._page_pair):
            self._page_cursor += 1
            self._page_source_floor = self._source_key(feedback)
            self._page_arrival_key = self._page_arrival_started = None
            if self._page_navigation_route and self._anchor_recovery is None:
                self._route = []

    def _page_source_proof(self, feedback):
        if (self._page_arrival_started is not None
                and self._interest_now > self._page_arrival_started + .9):
            return
        if (self._page_face is None or self._page_pair is None
                or not self._opportunity_current_valid
                or not 0 <= self._page_cursor < len(self._page_pair)):
            return
        metadata = feedback.get('semantic_metadata') or {}
        key = self._source_key(metadata)
        floor = self._page_source_floor
        if (key is None or floor is None or key[:2] != self._page_context
                or key in self._page_used_sources or key[:2] != floor[:2]
                or any(key[i] <= floor[i] for i in (2, 3, 4))):
            return
        arrival = self._page_arrival_key
        if arrival is not None and (key[:2] != arrival[:2]
                or any(key[i] <= arrival[i] for i in (2, 3, 4))):
            return
        source = feedback.get('accepted_anchor_observation') or {}
        faces = source.get('accepted_faces') or {}
        if (sum(faces.values()) < 6 or sum(n >= 2 for n in faces.values()) < 2
                or faces.get(self._page_face, 0) < 2):
            return
        rotation = self._rotation_basis((metadata.get('pose') or {}).get('rotation'))
        source_basis = self._rotation_basis(source.get('body_basis'))
        if rotation is None or source_basis is None or self._page_basis is None:
            return
        base = rotation @ source_basis.T
        goal = self._page_pair[self._page_cursor]
        current = base @ self._page_basis
        if _pose_angle(current, goal @ self._page_basis) > math.radians(6.):
            return
        # Perspective cosine and tvec belong to this exact semantic image.
        tvec = np.asarray(metadata['pose']['tvec'], float).reshape(3)
        index = next(i for i, k in enumerate(self._keys) if k == (self._page_face, 1, 1))
        camera = rotation @ self._points[index] + tvec
        cosine = -float((rotation @ self._normals[index]) @ camera) / np.linalg.norm(camera)
        if cosine < self._page_cosine_range[0]:
            return
        if any(_pose_angle(base, old['base_rotation']) < math.radians(8.)
               or _pose_angle(rotation, old['rotation']) < math.radians(8.)
               for old in self._page_views):
            return
        return key, base, rotation, cosine, faces

    def _waypoint_semantic_feedback(self, rows, observed, now, semantic_revision):
        if (not self._page_navigation_route or self._local_task is not None
                or self._anchor_recovery is not None or self._page_pair is None):
            return super()._waypoint_semantic_feedback(rows, observed, now, semantic_revision)
        if self._page_arrival_started is None:
            self._page_arrival_started = self._at_goal_since
            self._page_arrival_key = self._source_key(self._mixed_feedback)
        ready = (now <= self._page_arrival_started + .9
                 and self._page_arrival_key is not None
                 and self._page_source_proof(self._mixed_feedback) is not None)
        return ready, self._page_arrival_started + .9

    def _leave_page(self, reason):
        if self._page_face is None:
            return
        self._page_results[self._page_face] = dict(
            complete=len(self._page_views) >= 2, reason=reason,
            attempts=self._page_attempts, views=[dict(session_id=v['key'][0],
                map_revision=v['key'][1], frame_id=v['key'][2], generation=v['key'][3],
                frame_time=v['key'][4], cosine=v['cosine'],
                rotation=v['rotation'].tolist(), accepted_faces=v['accepted_faces'])
                for v in self._page_views])
        self._page_face = self._page_pair = None
        self._page_cursor = self._page_attempts = 0
        self._page_views = []
        self._page_source_floor = None
        self._page_arrival_key = self._page_arrival_started = None
        if self._page_navigation_route and self._anchor_recovery is None:
            self._route = []
        self._page_navigation_route = False
        self._page_pair_framing = ()

    def _select_page_pair(self, observed, rows, axes):
        face = self._page_face
        records = self._active_page_framing(rows) if self._target_framing_enabled else []
        rows = self._page_planning_rows(rows,records)
        indices = np.array([i for i, k in enumerate(self._keys) if k[0] == face])
        turns = np.radians((-150., -120., -90., -60., -40., -25., -15.,
                            0., 15., 25., 40., 60., 90., 120., 150.))
        second_angles = np.radians(np.arange(-150., 151., 6.))
        # The exact angle sets repeat across every first-leg family. Reuse
        # their matrices within this call, without changing poses or ordering.
        first_turns = {axis: [_matrix(_unit(axes[axis]) * a) for a in turns]
                       for axis in (0, 1)}
        second_turns = {axis: [_matrix(_unit(axes[axis]) * b) for b in second_angles]
                        for axis in (0, 1)}
        poses, paths, costs, families, values = [], [], [], [], []
        for first, second in ((0, 1), (1, 0)):
            for i, a in enumerate(turns):
                intermediate = first_turns[first][i] @ observed
                for j, b in enumerate(second_angles):
                    final = second_turns[second][j] @ intermediate
                    poses.append(final)
                    paths.append(([intermediate] if abs(a) > .01 else []) + [final])
                    costs.append(abs(a) + abs(b))
                    families.append((first, i, second))
                    values.append(b)
        poses = np.asarray(poses)
        camera = np.einsum('bij,j->bi', poses, self._points[indices[4]]) + self.tvec.reshape(3)
        normals = np.einsum('bij,j->bi', poses, self._normals[indices[4]])
        cosine = -np.sum(camera * normals, axis=1) / np.linalg.norm(camera, axis=1)
        eligible = np.flatnonzero((cosine >= self._page_cosine_range[0])
                                  & (cosine <= self._page_cosine_range[1]))
        if not len(eligible):
            return None
        # These exact predicates already reject the other candidates in the
        # reference. Avoid projecting support paths for discarded endpoints.
        readable = np.zeros((len(poses), 54))
        readable[eligible] = self._readability(poses[eligible], rows)
        glyphs = self._readability(poses[eligible], rows, anchors=True, known_only=False) > 0
        side_counts = np.stack([glyphs[:, j*9:(j+1)*9].sum(axis=1)
                               for j in range(6) if self._keys[j*9][0] != face], axis=1)
        eligible = eligible[(side_counts.max(axis=1) >= 2)
                            & ((readable[eligible][:, indices] > 0).sum(axis=1) >= 6)]
        if not len(eligible):
            return None
        support = np.zeros(len(poses))
        route_support = np.zeros(len(poses))
        support[eligible] = self._planning_anchor_support(poses[eligible], rows)
        route_support[eligible] = self._route_anchor_support(
            [paths[i] for i in eligible], rows, allow_potential=True)
        valid = (support > 0) & (route_support > 0)
        rate = self._observed_motion_rate or math.radians(24.)
        best = None
        pair_options = []
        for family in set(families):
            choices = [i for i, f in enumerate(families) if f == family and valid[i]]
            for left in choices:
                for right in choices:
                    difference = values[right] - values[left]
                    if not math.radians(14.) <= abs(difference) <= math.radians(20.):
                        continue
                    # Third endpoint, if needed, is one further step along the
                    # same second input axis; no return to the first-leg pose.
                    alternatives = [i for i in choices if (values[i]-values[right])*difference > 0
                                    and math.radians(10.) <= abs(values[i]-values[right]) <= math.radians(20.)
                                    and (not self._target_framing_enabled or all(
                                        readable[i,record['interest']['index']] > 0 for record in records))]
                    alternative = min(alternatives, key=lambda i: abs(values[i]-values[right])) if alternatives else None
                    visible = (readable[[left, right]][:, indices] > 0).any(axis=0).sum()
                    quality = readable[[left, right]][:, indices].sum()/18.
                    upright = max(0., -float(normals[[left, right], 1].mean()))
                    near = np.clip(1.-abs(cosine[[left, right]].mean()-.90)/.04, 0., 1.)
                    cost = costs[left] + abs(difference)
                    utility = (visible/9.+quality+.10*upright+.10*near)
                    utility *= math.sqrt(min(support[left], support[right])*route_support[left])
                    utility /= .7+cost/max(rate, math.radians(3.))
                    pair_options.append(dict(left=left,right=right,alternative=alternative,
                        utility=float(utility),eligible=True))
                    if best is None or utility > best[0]:
                        best = (utility, left, right, alternative)
        if best is None:
            return None
        if self._target_framing_enabled:
            selected = select_target_framing_pair(pair_options,readable=readable,endpoint_valid=valid,
                face_indices=indices.tolist(),interests=records,current_budgets=self._assigned_interests,
                context=self._page_context,now=getattr(self,'_interest_now',0.))
            self._page_framing_diagnostic = {k:deepcopy(v) for k,v in selected.items() if k != 'option'}
            if selected.get('status') != 'selected':
                return None
            option = selected['option']
            best = option['utility'],option['left'],option['right'],option.get('alternative')
        self._page_pair_framing = tuple(record['interest']['index'] for record in records)
        _, left, right, alternative = best
        sequence = [left, right] + ([] if alternative is None else [alternative])
        return [poses[i] @ self._page_basis.T for i in sequence], paths[left]

    def _plan(self, observed, rows, axes, now):
        self._page_arrival_key = self._page_arrival_started = None
        if self.recognition_goal != 'targets' or self._page_basis is None or len(self._page_results) >= 6:
            self._page_navigation_route = False
            return super()._plan(observed, rows, axes, now)
        if self._local_task is not None:
            self._page_resume_required = True
            self._page_navigation_route = False
            if (self._local_attempts < 2 and now-self._local_started <= 6.
                    and self._set_local_route(observed, rows, axes, now)):
                self._mixed_state = 'local_confirmation'
                return
            self._local_cooldowns[self._local_task['signature']] = now+4.
            self.insufficient.append(dict(reason='target_first_local_evidence_unresolved',
                kind=self._local_task['kind'], source_frame_id=self._local_task['source_frame_id'],
                cell_index=self._local_task['index']))
            self._local_task = self._local_goal = None
            # A local confirmation changed the actual pose. Re-solve the
            # remaining page from there instead of navigating to an old pose.
            self._page_pair = None
        if self._page_resume_required:
            self._page_pair = None
            self._page_navigation_route = False
            self._page_resume_required = False
        elif (self._target_framing_enabled and self._page_pair is not None and tuple(record['interest']['index']
                for record in self._active_page_framing(rows)) != self._page_pair_framing):
            # New hints are applied only here, after Cell released its route
            # at a finite boundary. The original attempt/TTL budgets survive.
            self._page_pair = None
            self._page_navigation_route = False
        elif (self._page_navigation_route and self._page_pair is not None
              and self._page_cursor == getattr(self, '_page_pending_cursor', None)):
            previous_goal = self._page_pair[self._page_cursor] @ self._page_basis
            if _pose_angle(observed, previous_goal) > math.radians(6.):
                # An aborted first leg does not leave the camera on the
                # shared second-axis orbit. Re-solve from the actual pose;
                # retain the page's attempts and real source proofs.
                self._page_pair = None
            else:
                self._page_cursor += 1
        started = time.perf_counter()
        for _ in range(6):
            if self._page_face is None:
                if not self._page_order:
                    cycle = list('URFDLB')
                    first = min(cycle, key=lambda f: (observed @ _NORMALS[f])[2])
                    position = cycle.index(first)
                    orders = [[cycle[(position+sign*j) % 6] for j in range(6)] for sign in (1, -1)]
                    self._page_order = min(orders, key=lambda order: (observed @ _NORMALS[order[1]])[2])
                pending = [f for f in self._page_order if f not in self._page_results]
                if not pending:
                    return super()._plan(observed, rows, axes, now)
                self._page_face = pending[0]
            if self._page_attempts >= 3:
                self._leave_page('finite_endpoints_exhausted')
                continue
            if self._page_pair is None:
                selected = self._select_page_pair(observed, rows, axes)
                if selected is None:
                    self._leave_page('no_supported_framing_pair')
                    continue
                self._page_pair, initial_path = selected
                self._page_initial_path = initial_path
                self._page_cursor = 0
                self._page_new_pair = True
            if self._page_attempts >= 3 or self._page_cursor >= len(self._page_pair):
                self._leave_page('finite_endpoints_exhausted')
                continue
            floor = self._source_key(self._mixed_feedback)
            if floor is None:
                self._leave_page('missing_atomic_geometry_waterline')
                continue
            goal = self._page_pair[self._page_cursor] @ self._page_basis
            path = self._page_initial_path if self._page_new_pair else [goal]
            self._route = self._short_waypoints(observed, path)
            self._final_rotation = goal
            self._target_indices = [i for i, k in enumerate(self._keys) if k[0] == self._page_face]
            self.target_face = self._page_face
            self._plan_votes = self._votes(rows)
            self._started = self._last_plan_time = now
            self._at_goal_since = self._progress_goal = self._no_descent_since = None
            self._plan_serial += 1
            self.stats['replans'] = max(0, self._plan_serial-1)
            self._page_source_floor = floor
            self._page_attempts += 1
            for index in self._page_pair_framing:
                interest = self._assigned_interests.get(index)
                if interest is not None and interest['attempts'] < 2:
                    interest['attempts'] += 1
            self._page_navigation_route = True
            self._page_new_pair = False
            self._mixed_state = 'face_page'
            self.planning_time_ms = (time.perf_counter()-started)*1000.
            # An endpoint gets one navigation attempt. Actual source receipt
            # can advance early, otherwise the next plan uses the next point.
            self._page_pending_cursor = self._page_cursor
            return
        return super()._plan(observed, rows, axes, now)

    def choose(self, rotation, cells, response_axes, quality=1.0,
               observed_rotation=None, tvec=None, elapsed=None,
               semantic_revision=None, anchor_feedback=None):
        feedback = anchor_feedback or {}
        self._sync_page_basis(feedback)
        if self._local_task is not None or self._anchor_recovery is not None:
            self._page_resume_required = True
            self._page_arrival_key = self._page_arrival_started = None
        result = super().choose(rotation, cells, response_axes, quality,
            observed_rotation, tvec, elapsed, semantic_revision, anchor_feedback)
        if self._local_task is not None or self._anchor_recovery is not None:
            self._page_resume_required = True
            self._page_arrival_key = self._page_arrival_started = None
        return result

    def summary(self):
        result = super().summary()
        result['face_pages'] = dict(navigation_only=True, order=list(self._page_order),
            page_cosine_range=list(self._page_cosine_range),
            active_face=self._page_face, cursor=self._page_cursor, attempts=self._page_attempts,
            accepted_views=len(self._page_views), finished=deepcopy(self._page_results),
            fallback=len(self._page_results) == 6,framing_enabled=self._target_framing_enabled,
            framing=deepcopy(self._page_framing_diagnostic),
            framing_sources=[dict(index=index,source_frame_id=record['interest']['source_frame_id'])
                for index,record in self._page_framing.items()],resume_required=self._page_resume_required,
            framing_rejections=self._page_framing_rejections,
            arrival_source_waterline=self._page_arrival_key,
            arrival_elapsed=self._page_arrival_started,
            framing_rejected=deepcopy(self._page_framing_rejected))
        return result
