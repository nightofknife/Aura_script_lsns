"""Experimental short camera prefixes; framing is navigation, never a vote."""
import math
import time
from copy import deepcopy

import numpy as np

from ._deep_dive_scan_policy import _matrix, _unit, _pose_angle
from ._deep_dive_target_first_scan_policy import TargetFirstCellScanPolicy


class _JointRouteBlocked(RuntimeError):
    """A bounded navigation failure, not a vision or programming exception."""


class TargetFramedCellScanPolicy(TargetFirstCellScanPolicy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._framed_context = self._framed_source = None
        self._framed_seen = np.zeros(54, dtype=bool)
        self._framed_attempts = dict.fromkeys('URFDLB', 0)
        self._framed_selected = None
        self._framed_owns_route = False
        self._framed_basis_valid = False
        self._joint_source = None
        self._joint_observed = None
        self._joint_route_cache = {}
        self._joint_blocked_reason = None
        self._joint_latest_mask_clear = np.ones(54,dtype=bool)
        self._bridge_attempts = dict.fromkeys('URFDLB',0)
        self._bridge_last_source = {}
        self._bridge_pending = None
        self._bridge_owns_route = False
        self._bridge_selected = None
        self._joint_ever_supported = dict.fromkeys('URFDLB',False)
        self._bridge_last_cancelled_face = None

    def _sync_framed_context(self, feedback):
        context = (feedback.get('session_id'), feedback.get('map_revision'))
        valid = (all(type(v) is int and v >= 0 for v in context)
                 and self._rotation_basis(feedback.get('geometry_body_basis')) is not None)
        identity_valid = all(type(v) is int and v >= 0 for v in context)
        changed = identity_valid and context != self._framed_context
        if changed or not valid:
            self._framed_seen[:] = False
            self._joint_source = None
            self._joint_latest_mask_clear[:] = True
            self._bridge_pending = None
            self._bridge_owns_route = False
            self._framed_source = self._framed_selected = None
            if changed:
                self._framed_attempts = dict.fromkeys('URFDLB', 0)
                self._bridge_attempts = dict.fromkeys('URFDLB',0)
                self._bridge_last_source = {}
                self._joint_ever_supported = dict.fromkeys('URFDLB',False)
            if self._framed_owns_route and self._anchor_recovery is None:
                self._route = []
            self._framed_owns_route = False
        if identity_valid:
            self._framed_context = context
        self._framed_basis_valid = valid

    def _billboard_geometry(self, rotations, translation=None):
        """Camera-facing radius 1 world unit, normal height [0,1] world units.

        Projection extrema of this finite height band occur at its endpoints.
        This deliberately conservative prior is not a detector error bound.
        """
        rotations = np.asarray(rotations, float).reshape(-1,3,3)
        translation = np.asarray(self.tvec if translation is None else translation,float).reshape(3)
        heights = self._points[:,None,:] + self._normals[:,None,:]*np.array([0.,1.])[None,:,None]
        camera = np.einsum('bij,nhj->bnhi',rotations,heights) + translation
        radius = 1.
        corners = camera[:,:,:,None,:] + np.array([
            [-radius,-radius,0.],[-radius,radius,0.],
            [radius,-radius,0.],[radius,radius,0.]])[None,None,None,:,:]
        depth = corners[...,2]
        with np.errstate(divide='ignore',invalid='ignore'):
            pixels = corners[...,:2]/depth[...,None]*self._K[0,0]+self._K[:2,2]
        lo, hi = pixels.min(axis=(2,3)),pixels.max(axis=(2,3))
        valid = ((depth > 1.).all(axis=(2,3)) & np.isfinite(pixels).all(axis=(2,3,4))
                 & (lo[...,0] >= 305) & (hi[...,0] <= 940)
                 & (lo[...,1] >= 95) & (hi[...,1] <= 600))
        reset = ((hi[...,0] > 590) & (lo[...,0] < 698)
                 & (hi[...,1] > 480) & (lo[...,1] < 590))
        return lo,hi,valid & ~reset

    def _framed_readable(self, rotations, rows, translation=None, masks=()):
        previous = self.tvec
        if translation is not None:
            self.tvec = np.asarray(translation,float).reshape(3)
        try:
            original = self._readability(rotations,rows)
            lo,hi,valid = self._billboard_geometry(rotations)
            # Use this source's R/T, rather than the parent bitmap's accumulated
            # visits or its accepted-owner exemption. Every actual mask blocks.
            rotations = np.asarray(rotations,float).reshape(-1,3,3)
            def project(points):
                camera = np.einsum('bij,...j->b...i',rotations,points)+np.asarray(self.tvec,float).reshape(3)
                return camera[...,:2]/camera[...,2,None]*self._K[0,0]+self._K[:2,2]
            quad = project(self._quads)
            corridor = project(self._corridors)
            regions = ((lo,hi),(quad.min(axis=2),quad.max(axis=2)),
                       (corridor.min(axis=2)-14.,corridor.max(axis=2)+14.))
        finally:
            self.tvec = previous
        for mask in masks:
            box = np.asarray(mask['box'],float)
            for region_lo,region_hi in regions:
                overlap = np.minimum(region_hi,box[:2]+box[2:]+8.)-np.maximum(region_lo,box[:2]-8.)
                valid &= ~((overlap[...,0] > 0) & (overlap[...,1] > 0))
        return original*valid

    def _record_opportunities(self, rows, feedback):
        super()._record_opportunities(rows,feedback)
        if (not self._framed_basis_valid or not self._opportunity_current_valid
                or self._framed_context != self._target_source_context):
            return
        metadata = feedback.get('semantic_metadata') or {}
        key = tuple(metadata.get(k) for k in ('session_id','map_revision','frame_id','generation','frame_time'))
        source = feedback.get('accepted_anchor_observation') or {}
        faces = source.get('accepted_faces') or {}
        pose = metadata['pose']
        previous = self.tvec
        self.tvec = np.asarray(pose['tvec'],float).reshape(3)
        try:
            # Latest rectangles filter logical source cells only after original
            # same-source projection. Never paste old screen boxes on a new R.
            self._joint_latest_mask_clear = self._joint_mask_clear(
                np.asarray(pose['rotation'])[None],feedback.get('target_candidate_associations') or ())[0]
        finally:
            self.tvec = previous
        if (key[:2] != self._framed_context or sum(faces.values()) < 6
                or sum(v >= 2 for v in faces.values()) < 2):
            return
        # Counts are not coordinates. Consume only the fitter's actual accepted
        # indices, exported with the same immutable source; never candidate IDs.
        indices = source.get('accepted_cell_indices')
        diagnostic = feedback.get('refine_diagnostic') or {}
        if (isinstance(indices,list) and indices == diagnostic.get('accepted_cell_indices')
                and len(indices) == len(set(i for i in indices if type(i) is int))
                and all(type(i) is int and 0 <= i < 54 for i in indices)):
            counts = {face:sum(self._keys[i][0] == face for i in indices) for face in faces}
            if (counts == faces and sum(counts.values()) == len(indices) and len(indices) >= 6
                    and sum(v >= 2 for v in counts.values()) >= 2):
                pose = metadata['pose']
                previous = self.tvec
                self.tvec = np.asarray(pose['tvec'],float).reshape(3)
                try:
                    clear = self._joint_clear_readability(np.asarray(pose['rotation'])[None],rows,
                        feedback.get('target_candidate_associations') or ())[0] > 0
                finally:
                    self.tvec = previous
                actual = [i for i in indices if clear[i]]
                if self._joint_counts_valid(np.isin(np.arange(54),actual)[None])[0]:
                    self._joint_source = dict(key=key,indices=actual,
                        body_basis=deepcopy(source['body_basis']),
                        base_rotation=(np.asarray(pose['rotation'])@np.asarray(source['body_basis']).T).tolist())
                    for j,face in enumerate('URFDLB'):
                        if sum(i//9 == j for i in actual) >= 2:
                            self._joint_ever_supported[face] = True
        self._bridge_accept_source(feedback)
        if self._framed_source is not None:
            if key == self._framed_source or any(key[i] <= self._framed_source[i] for i in (2,3,4)):
                return
        pose = metadata['pose']
        rotation = np.asarray(pose['rotation'],float)
        visible = self._framed_readable(rotation[None],rows,pose['tvec'],
            feedback.get('target_candidate_associations') or ())[0] > 0
        visible &= np.array([faces.get(face,0) >= 2 for face,_,_ in self._keys])
        self._framed_seen |= visible
        self._framed_source = key

    @staticmethod
    def _joint_counts_valid(visible):
        counts = np.stack([visible[:,i*9:(i+1)*9].sum(axis=1) for i in range(6)],axis=1)
        return (visible.sum(axis=1) >= 6) & ((counts >= 2).sum(axis=1) >= 2)

    def _joint_clear_readability(self, rotations, rows, masks=()):
        """Original glyph gates plus actual-source masks, without owner waiver."""
        rotations = np.asarray(rotations,float).reshape(-1,3,3)
        readable = self._readability(rotations,rows,anchors=True,known_only=False)
        return readable*self._joint_mask_clear(rotations,masks)

    def _joint_mask_clear(self, rotations, masks):
        clear = np.ones((len(rotations),54),dtype=bool)
        if not masks:
            return clear
        def project(points):
            camera = np.einsum('bij,...j->b...i',rotations,points)+np.asarray(self.tvec).reshape(3)
            return camera[...,:2]/camera[...,2,None]*self._K[0,0]+self._K[:2,2]
        quad = project(self._quads); corridor = project(self._corridors)
        for mask in masks:
            box = np.asarray(mask['box'],float)
            for lo,hi in ((quad.min(axis=2),quad.max(axis=2)),
                          (corridor.min(axis=2)-14.,corridor.max(axis=2)+14.)):
                overlap = np.minimum(hi,box[:2]+box[2:]+8.)-np.maximum(lo,box[:2]-8.)
                clear &= ~((overlap[...,0] > 0) & (overlap[...,1] > 0))
        return clear

    def _joint_endpoint_valid(self, rotations, rows):
        if self._joint_source is None or not self._framed_basis_valid:
            return np.zeros(len(rotations),dtype=bool)
        visible = self._joint_clear_readability(rotations,rows) > 0
        actual = np.zeros(54,dtype=bool);actual[self._joint_source['indices']] = True
        return self._joint_counts_valid(visible & actual[None] & self._joint_latest_mask_clear[None])

    def _joint_paths_valid(self, paths, rows):
        # Cache only this decision's object and rows; no cross-frame projection.
        key = (id(paths),id(rows))
        cached = self._joint_route_cache.get(key)
        if cached is not None and cached[0] is paths and cached[1] is rows:
            return cached[2]
        waypoints = [self._short_waypoints(self._joint_observed,path) for path in paths]
        lengths = [len(path) for path in waypoints]
        if not all(lengths):
            return np.zeros(len(paths),dtype=bool)
        poses = np.asarray([pose for path in waypoints for pose in path])
        # Prefix families share many checkpoints. Project each exact rotation
        # once; no rounding, cross-frame cache, or approximate support test.
        unique,inverse = np.unique(poses.reshape(-1,9),axis=0,return_inverse=True)
        valid = self._joint_endpoint_valid(unique.reshape(-1,3,3),rows)[inverse]
        offsets = np.r_[0,np.cumsum(lengths)[:-1]]
        result = np.logical_and.reduceat(valid,offsets)
        self._joint_route_cache[key] = (paths,rows,result)
        return result

    def _planning_anchor_support(self, rotations, rows, *, route=False):
        original = super()._planning_anchor_support(rotations,rows,route=route)
        return original*self._joint_endpoint_valid(rotations,rows)

    def _route_anchor_support(self, paths, rows, *, allow_potential=False):
        original = super()._route_anchor_support(paths,rows,allow_potential=allow_potential)
        return original*self._joint_paths_valid(paths,rows)

    def _planning_endpoint(self, best, score, candidates, paths, costs, readable,
                           adjusted, novelty, observed, rows, now):
        # Cell's score<=0 exploration fallback must not bypass this hard gate.
        safe = self._joint_paths_valid(paths,rows) & novelty & (score > 0) & np.isfinite(score)
        if not safe.any():
            raise _JointRouteBlocked('joint_navigation_no_supported_route')
        if not safe[best]:
            best = int(np.argmax(np.where(safe,score,-np.inf)))
        choice = super()._planning_endpoint(best,score,candidates,paths,costs,readable,
            adjusted,novelty,observed,rows,now)
        return choice if safe[choice] else best

    def _route_targets_complete(self, complete, rows):
        if (self._framed_owns_route and self._local_task is None
                and self._anchor_recovery is None):
            # Atlas none/node completion does not fulfill a camera framing job.
            # This controls only our route, never the public recognition goal.
            return all(self._framed_seen[i] or self._proven_positive(rows[i])
                       for i in self._target_indices)
        return super()._route_targets_complete(complete,rows)

    def _bridge_accept_source(self, feedback):
        pending,source = self._bridge_pending,self._joint_source
        basis = self._rotation_basis(feedback.get('geometry_body_basis'))
        if (not pending or pending.get('arrival_stamp') is None or not source
                or basis is None or not self._opportunity_current_valid):
            return
        key = source['key'];floor = pending['floor']
        pose = np.asarray(source['base_rotation'])@basis
        if (key[:2] == floor[:2] and all(key[i] > floor[i] for i in (2,3,4))
                and key[2] == (feedback.get('semantic_metadata') or {}).get('frame_id')
                and _pose_angle(np.asarray(source['base_rotation']),np.asarray(pending['source_base'])) >= math.radians(8.)
                and _pose_angle(pose,np.asarray(pending['goal_base'])@basis) <= math.radians(4.)):
            pending['ready'] = True

    def _waypoint_semantic_feedback(self, rows, observed, now, semantic_revision):
        fresh,deadline = super()._waypoint_semantic_feedback(rows,observed,now,semantic_revision)
        if self._bridge_owns_route and self._bridge_pending is not None and len(self._route) == 1:
            return bool(self._bridge_pending.get('ready')),deadline
        return fresh,deadline

    def _feedback(self, rows, pose, now, reason, force_failure=False):
        if (reason == 'semantic_dwell_timeout' and force_failure and len(self._route) == 1
                and self._bridge_owns_route and self._bridge_pending is not None
                and self._bridge_pending.get('arrival_stamp') is not None
                and self._local_task is None and self._anchor_recovery is None):
            # Only the actual Cell deadline consumption can expire a bridge.
            # Polling elapsed time or interrupted approach cannot grant fallback.
            self._bridge_pending['timeout_consumed'] = True
        return super()._feedback(rows,pose,now,reason,force_failure)

    def _assigned_interest_interrupts_route(self):
        if (self._bridge_owns_route and self._bridge_pending is not None and self._route
                and self._local_task is None and self._anchor_recovery is None):
            # Parent calls this hook only for a newly admitted strict interest.
            # Cancel navigation, never refund its ledger or credit completion.
            self._bridge_last_cancelled_face = (self._bridge_selected or {}).get('face')
            self.stats['bridge_interest_cancellations'] = self.stats.get('bridge_interest_cancellations',0)+1
            self._bridge_pending = None
            self._bridge_owns_route = False
        return super()._assigned_interest_interrupts_route()

    def _parent_or_bridge_plan(self, observed, rows, axes, now):
        try:
            return super()._plan(observed,rows,axes,now)
        except _JointRouteBlocked:
            if not self._plan_bridge(observed,rows,axes,now):
                raise

    def _plan_bridge(self, observed, rows, axes, now):
        source,feedback = self._joint_source,self._mixed_feedback
        basis = self._rotation_basis(feedback.get('geometry_body_basis'))
        floor = tuple(feedback.get(k) for k in ('session_id','map_revision','frame_id','generation','frame_time'))
        if (source is None or basis is None or floor[:2] != self._framed_context
                or any(type(floor[i]) is not int for i in (0,1,2,3))
                or type(floor[4]) not in (int,float) or not math.isfinite(floor[4])):
            return False
        candidates,paths,costs = self._short_candidates(observed,axes)
        safe = self._joint_paths_valid(paths,rows)
        visible = self._joint_clear_readability(candidates,rows) > 0
        actual = np.zeros(54,dtype=bool);actual[source['indices']] = True
        count = (visible & actual & self._joint_latest_mask_clear).sum(axis=1)
        def normals(poses):
            camera=np.einsum('bij,nj->bni',poses,self._points[4::9])+np.asarray(self.tvec).reshape(3)
            normal=np.einsum('bij,nj->bni',poses,self._normals[4::9])
            return -(camera*normal).sum(axis=2)/np.linalg.norm(camera,axis=2)
        improvement = normals(candidates)-normals(observed[None])[0]
        options=[]
        for j,face in enumerate('URFDLB'):
            if (self._bridge_attempts[face] >= 2 or
                    self._joint_ever_supported[face]):
                continue
            last=self._bridge_last_source.get(face)
            if last and (not all(source['key'][i] > last['key'][i] for i in (2,3,4))
                    or _pose_angle(np.asarray(source['base_rotation']),np.asarray(last['base'])) < math.radians(8.)):
                continue
            for i in np.flatnonzero(safe & (improvement[:,j] > .08)):
                options.append((float(improvement[i,j]*count[i]/(6.*(.65+costs[i]))),j,int(i)))
        if not options:
            return False
        _,j,best=max(options,key=lambda v:(v[0],-v[1],-v[2]));face='URFDLB'[j]
        self._bridge_attempts[face]+=1
        self._bridge_last_source[face]=dict(key=source['key'],base=deepcopy(source['base_rotation']))
        self._bridge_pending=dict(floor=floor,source_base=deepcopy(source['base_rotation']),
            goal_base=(candidates[best]@basis.T).tolist(),basis=basis.copy(),arrival_stamp=None,ready=False)
        self._bridge_owns_route=True
        self._route=self._short_waypoints(observed,paths[best]);self._final_rotation=candidates[best]
        self._target_indices=[];self.target_face=face;self._plan_votes=self._votes(rows)
        self._started=self._last_plan_time=now
        self._at_goal_since=self._progress_goal=self._no_descent_since=None
        self._plan_serial+=1;self.stats['replans']=max(0,self._plan_serial-1)
        self._bridge_selected=dict(face=face,candidate=best,travel_deg=math.degrees(costs[best]),
            normal_gain=float(improvement[best,j]),predicted_cos=float(normals(candidates[best:best+1])[0,j]),navigation_only=True)
        return True

    @staticmethod
    def _short_candidates(observed, axes):
        candidates,paths,costs = [],[],[]
        for axis in (0,1):
            for sign in (-1,1):
                for angle in (12.,22.,30.):
                    final = _matrix(_unit(axes[axis])*math.radians(sign*angle))@observed
                    candidates.append(final);paths.append([final]);costs.append(math.radians(angle))
        for first,second in ((0,1),(1,0)):
            for sign_a in (-1,1):
                for sign_b in (-1,1):
                    for a,b in ((12.,12.),(22.,8.),(8.,22.)):
                        middle = _matrix(_unit(axes[first])*math.radians(sign_a*a))@observed
                        final = _matrix(_unit(axes[second])*math.radians(sign_b*b))@middle
                        candidates.append(final);paths.append([middle,final]);costs.append(math.radians(a+b))
        return np.asarray(candidates),paths,np.asarray(costs)

    def _proven_positive(self, row):
        kind = row.get('occupant')
        if row.get('occupant_status') != 'confirmed' or kind not in ('player','inspiration','singularity'):
            return False
        poses = []
        for evidence in row.get('evidence',()):
            proof = evidence.get('association_evidence') or {}
            frame = evidence.get('frame_id')
            rotation = self._rotation_basis(proof.get('rotation'))
            if (evidence.get('occupant') != kind or rotation is None
                    or type(frame) is not int or type(proof.get('source_frame_id')) is not int
                    or proof.get('source_frame_id') != frame
                    or type(proof.get('source_map_revision')) is not int
                    or proof['source_map_revision'] != self._framed_context[1]):
                continue
            if any(frame != old_frame and _pose_angle(rotation,old_rotation) >= math.radians(8.)
                   for old_frame,old_rotation in poses):
                return True
            poses.append((frame,rotation))
        return False

    def _plan(self, observed, rows, axes, now):
        self._joint_observed = np.asarray(observed,float)
        self._joint_route_cache = {}
        self._framed_owns_route = False
        if self._bridge_pending is not None:
            if self._local_task is None and not self._bridge_pending.get('ready'):
                if not (self._bridge_pending.get('timeout_consumed')
                        and self._framed_basis_valid and self._anchor_recovery is None
                        and self._joint_source is not None
                        and self._joint_endpoint_valid(observed[None],rows)[0]):
                    raise _JointRouteBlocked('joint_bridge_no_new_source' if self._bridge_pending.get('arrival_stamp') is not None
                                             else 'joint_bridge_endpoint_not_reached')
                self.stats['bridge_timeout_fallbacks'] = self.stats.get('bridge_timeout_fallbacks',0)+1
            self._bridge_pending=None
        self._bridge_owns_route=False
        if (self.recognition_goal != 'targets' or self._local_task is not None
                or not self._framed_basis_valid):
            return super()._plan(observed,rows,axes,now)
        started = time.perf_counter()
        # Ordinary confirmed-none votes are not a billboard-framing visit.
        # Readiness may still stop immediately without completing this bitmap.
        pending = ~self._framed_seen & ~np.array([self._proven_positive(row) for row in rows])
        allowed = np.array([self._framed_attempts[face] < 2 for face,_,_ in self._keys])
        pending &= allowed
        if not pending.any():
            return self._parent_or_bridge_plan(observed,rows,axes,now)
        candidates,paths,costs = self._short_candidates(observed,axes)
        readable = self._framed_readable(candidates,rows)
        adjusted = readable.copy()
        for failed,indices,stamp in self._cell_failures:
            angle = np.arccos(np.clip((np.einsum('bij,ij->b',candidates,failed)-1.)*.5,-1.,1.))
            penalty = .92*np.exp(-.5*(angle/math.radians(28.))**2)*math.exp(-max(0.,now-stamp)/45.)
            adjusted[:,indices] *= 1.-penalty[:,None]
        novelty = np.ones(36,dtype=bool)
        for prior in self._reached[-48:]+self._failed_views[-12:]+[observed]:
            novelty &= np.einsum('bij,ij->b',candidates,prior) < 1.+2.*math.cos(math.radians(9.))
        weights = pending.astype(float)
        interests = self._active_interests(rows,now)
        for interest in interests:
            if pending[interest['index']]: weights[interest['index']] = 2.
        per_face = np.stack([(adjusted[:,i*9:(i+1)*9]*weights[i*9:(i+1)*9]).sum(axis=1)/9.
                             for i in range(6)],axis=1)
        gain = per_face.sum(axis=1)
        current_normal = -(observed@self._normals.T)[2,::9]
        normal = -(candidates@self._normals.T)[:,2,::9]
        bonus = .05*(np.clip(normal-current_normal,0.,1.)*(per_face > 0)).sum(axis=1)
        endpoint = self._planning_anchor_support(candidates,rows)
        route = self._route_anchor_support(paths,rows,allow_potential=True)
        rate = self._observed_motion_rate or min(math.radians(24.),max(np.linalg.norm(v) for v in axes.values())*450.)
        score = (gain+bonus)*endpoint*route/(.65+costs/max(math.radians(3.),rate))
        valid = novelty & (gain > 0) & (endpoint > 0) & (route > 0)
        if not valid.any():
            return self._parent_or_bridge_plan(observed,rows,axes,now)
        score[~valid] = -1.
        best = int(np.argmax(score))
        indices = np.flatnonzero((readable[best] > 0)&pending).tolist()
        face = self._keys[int(np.argmax(per_face[best]))*9][0]
        self._framed_attempts[face] += 1
        self._route = self._short_waypoints(observed,paths[best])
        self._final_rotation = candidates[best]
        self._target_indices = indices
        self.target_face = face
        self.visits[face] += 1
        self._plan_votes = self._votes(rows)
        self._started = self._last_plan_time = now
        self._at_goal_since = self._progress_goal = self._no_descent_since = None
        self._plan_serial += 1
        self.stats['replans'] = max(0,self._plan_serial-1)
        self._framed_owns_route = True
        self._mixed_state = 'target_framed_short_prefix'
        for interest in interests:
            if interest['index'] in indices:
                self._assigned_interests[interest['index']]['attempts'] += 1
        self._framed_selected = dict(face=face,indices=indices,travel_deg=math.degrees(costs[best]),
            legs=len(paths[best]),candidate=best,gain=float(gain[best]),navigation_only=True)
        self.planning_time_ms = (time.perf_counter()-started)*1000.

    def choose(self, rotation, cells, response_axes, quality=1.0, observed_rotation=None,
               tvec=None, elapsed=None, semantic_revision=None, anchor_feedback=None):
        self._sync_framed_context(anchor_feedback or {})
        self._joint_observed = np.asarray(rotation if observed_rotation is None else observed_rotation,float)
        self._joint_route_cache = {}
        pending=self._bridge_pending
        basis=self._rotation_basis((anchor_feedback or {}).get('geometry_body_basis'))
        if pending and basis is not None and self._local_task is None and self._anchor_recovery is None:
            change=pending['basis'].T@basis
            if self._bridge_owns_route:
                self._route=[pose@change for pose in self._route]
                if self._final_rotation is not None:self._final_rotation=self._final_rotation@change
            pending['basis']=basis.copy()
            if (pending['arrival_stamp'] is None and len(self._route) == 1 and quality >= .5
                    and (anchor_feedback or {}).get('geometry_tracking_ok') is True
                    and _pose_angle(self._joint_observed,np.asarray(pending['goal_base'])@basis) <= math.radians(4.)):
                stamp=(anchor_feedback or {}).get('frame_time')
                if type(stamp) in (int,float) and math.isfinite(stamp):pending['arrival_stamp']=stamp
        for retry in range(2):
            serial = self._plan_serial
            try:
                result = super().choose(rotation,cells,response_axes,quality,observed_rotation,tvec,
                                        elapsed,semantic_revision,anchor_feedback)
                if result.get('direction') is not None and self._anchor_recovery is None:
                    planning_rows = self._local_planning_rows(self._rows(cells))
                    # Full new route once; later input checks only its active leg.
                    path = self._route if self._plan_serial != serial else self._route[:1]
                    if not path or not self._joint_paths_valid([path],planning_rows)[0]:
                        raise _JointRouteBlocked('joint_navigation_published_route_unsupported')
                break
            except _JointRouteBlocked as error:
                can_replan = (retry == 0 and str(error) == 'joint_navigation_published_route_unsupported'
                    and self._anchor_recovery is None and self._framed_basis_valid
                    and self._joint_source is not None
                    and self._joint_endpoint_valid(self._joint_observed[None],planning_rows)[0])
                self._route = []
                self._framed_owns_route = False
                if can_replan:
                    # Same observed packet and elapsed clock, no input or source
                    # renewal. The existing planner retains every finite ledger.
                    self._joint_route_cache = {}
                    self.stats['joint_route_replans'] = self.stats.get('joint_route_replans',0)+1
                    continue
                self._joint_blocked_reason = str(error)
                result = dict(direction=None,phase='recover',reason=str(error))
                break
        result['framed_owns_route'] = bool(self._framed_owns_route and self._route
            and self._local_task is None and self._anchor_recovery is None)
        result['framed_selected'] = deepcopy(self._framed_selected)
        result['bridge_owns_route']=bool(self._bridge_owns_route and self._route and self._anchor_recovery is None)
        result['bridge_selected']=deepcopy(self._bridge_selected)
        return result

    def summary(self):
        result = super().summary()
        result['target_framed'] = dict(navigation_only=True,covered=int(self._framed_seen.sum()),
            by_face={face:int(self._framed_seen[i*9:(i+1)*9].sum()) for i,face in enumerate('URFDLB')},
            attempts=dict(self._framed_attempts),source=self._framed_source,
            selected=deepcopy(self._framed_selected),height_world=[0.,1.],radius_world=1.)
        result['target_framed']['joint_navigation'] = dict(source=None if self._joint_source is None
            else self._joint_source['key'],indices=[] if self._joint_source is None
            else list(self._joint_source['indices']),blocked_reason=self._joint_blocked_reason,
            ever_supported=dict(self._joint_ever_supported),
            route_replans=self.stats.get('joint_route_replans',0))
        result['target_framed']['bridge']=dict(navigation_only=True,attempts=dict(self._bridge_attempts),
            selected=deepcopy(self._bridge_selected),pending=bool(self._bridge_pending),
            arrival_stamp=None if self._bridge_pending is None else self._bridge_pending['arrival_stamp'],
            ready=False if self._bridge_pending is None else self._bridge_pending['ready'],
            timeout_fallbacks=self.stats.get('bridge_timeout_fallbacks',0),
            interest_cancellations=self.stats.get('bridge_interest_cancellations',0),
            last_cancelled_face=self._bridge_last_cancelled_face)
        return result
