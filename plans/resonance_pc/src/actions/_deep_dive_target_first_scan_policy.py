"""Opt-in cell coverage with bounded, real-source target confirmation.

Scheduling only: this class never modifies atlas labels or supplies votes.
"""
import math
import time
from copy import deepcopy

import numpy as np

from ._deep_dive_scan_policy import CellScanPolicy, MixedFaceScanPolicy, _matrix, _pose_angle, _unit


class TargetFirstCellScanPolicy(MixedFaceScanPolicy):
    def __init__(self, base_step_px=300, max_step_px=450, *,
                 recognition_goal='targets', expected_inspirations=None,
                 opportunity_navigation=False):
        if type(opportunity_navigation) is not bool:
            raise TypeError('opportunity_navigation must be bool')
        super().__init__(base_step_px, max_step_px, recognition_goal=recognition_goal,
                         expected_inspirations=expected_inspirations)
        self.opportunity_navigation = opportunity_navigation
        self._target_source_context = None
        self._target_sources = {}
        self._local_basis = None
        self._target_packet_frame = None
        self._assigned_interests = {}
        self._opportunities = np.zeros(54, dtype=bool)
        self._opportunity_context = None
        self._opportunity_source = None
        self._opportunity_current_valid = False
        self._local_source_budgets = {}

    def _remember_target_source(self, feedback):
        self._target_packet_frame = None
        context = (feedback.get('session_id'), feedback.get('map_revision'))
        if (context != self._opportunity_context or type(context[0]) is not int
                or type(context[1]) is not int or context[1] < 0):
            self._opportunities[:] = False
            self._opportunity_context = context
            self._opportunity_source = None
            # Historical navigation cannot survive a context switch even when
            # the new packet has no usable body basis yet.
            self._assigned_interests = {}
            self._local_source_budgets = {}
        basis = self._rotation_basis(feedback.get('geometry_body_basis'))
        if None in context or basis is None:
            return None
        if context != self._target_source_context:
            self._target_source_context = context
            self._target_sources = {}
            self._local_task = self._local_goal = self._local_basis = None
            self._local_cooldowns = {}
            self._local_source_budgets = {}
            self._assigned_interests = {}
            self._opportunities[:] = False
            self._opportunity_context = context
            self._opportunity_source = None
        source = feedback.get('accepted_anchor_observation') or {}
        diagnostic = feedback.get('refine_diagnostic') or {}
        rotation = self._rotation_basis(source.get('rotation'))
        source_basis = self._rotation_basis(source.get('body_basis'))
        stamp = source.get('frame_time')
        faces = source.get('accepted_faces') or {}
        counts = list(faces.values())
        stamps = (stamp, diagnostic.get('source_frame_time'), feedback.get('glyph_anchor_at'))
        valid = (rotation is not None and source_basis is not None
            and (source.get('session_id'), source.get('map_revision')) == context
            and type(source.get('frame_id')) is int
            and source['frame_id'] == diagnostic.get('source_frame_id') == feedback.get('glyph_anchor_frame_id')
            and diagnostic.get('source_map_revision') == context[1]
            and all(type(value) in (int, float) and math.isfinite(value) for value in stamps)
            and max(stamps)-min(stamps) <= 1e-6
            and diagnostic.get('renewed') is True and not feedback.get('fusion_paused')
            and faces == diagnostic.get('accepted_faces') and counts
            and all(type(count) is int and count >= 0 for count in counts)
            and sum(counts) >= 6 and (sum(count >= 2 for count in counts) >= 2 or max(counts) >= 7))
        if valid:
            self._target_sources[source['frame_id']] = dict(
                base_rotation=rotation @ source_basis.T, rotation=rotation.copy(), stamp=stamp, context=context)
            self._target_packet_frame = source['frame_id']
            if len(self._target_sources) > 16:
                oldest = min(self._target_sources, key=lambda key: self._target_sources[key]['stamp'])
                del self._target_sources[oldest]
        return basis

    def _evidence_pose(self, entry, revision):
        proof = entry.get('association_evidence') or {}
        source = self._target_sources.get(entry.get('frame_id'))
        basis = self._rotation_basis(self._mixed_feedback.get('geometry_body_basis'))
        rotation = self._rotation_basis(proof.get('rotation'))
        stamp = proof.get('source_frame_time')
        if (source is None or basis is None or rotation is None
                or proof.get('source_frame_id') != entry.get('frame_id')
                or proof.get('source_map_revision') != revision or source['context'] != self._target_source_context
                or type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not math.isclose(stamp, source['stamp'], rel_tol=0., abs_tol=1e-6)):
            return None
        if _pose_angle(rotation, source['rotation']) > 1e-6:
            return None
        return source['base_rotation'] @ basis

    def _packet_pose(self, feedback):
        source = feedback.get('accepted_anchor_observation') or {}
        packet = feedback.get('accepted_face_observation') or {}
        diagnostic = feedback.get('refine_diagnostic') or {}
        retained = self._target_sources.get(source.get('frame_id'))
        basis = self._rotation_basis(feedback.get('geometry_body_basis'))
        if (retained is None or basis is None or not diagnostic.get('renewed')
                or self._target_packet_frame != source.get('frame_id')
                or packet.get('frame_id') != source.get('frame_id')
                or (feedback.get('glyph_anchor_age_sec', 0.) or 0.) >= .9
                or feedback.get('fusion_paused')):
            return None
        metadata = feedback.get('semantic_metadata') or {}
        if metadata and metadata.get('frame_id') != source.get('frame_id'):
            return None
        return retained['base_rotation'] @ basis

    def _tasks(self, rows, feedback, observed, now):
        if self._rotation_basis(feedback.get('geometry_body_basis')) is None:
            return []
        tasks = super()._tasks(rows, feedback, observed, now)
        revision = feedback.get('semantic_map_revision', feedback.get('map_revision'))
        # Unassociated candidates can need a different face, not a small local
        # dither. Leave their masks/readiness obligations intact, but let the
        # ordinary global cell planner reach them instead of interrupting it.
        tasks = [task for task in tasks if task['signature'][0] == 'positive'
                 and any(entry.get('frame_id') == task['source_frame_id']
                        and self._evidence_pose(entry, revision) is not None
                        for entry in rows[task['index']].get('evidence', ()))]
        # A second near-angle group remains the same unresolved entity task;
        # it must not repeatedly restart the bounded local attempt clock.
        for task in tasks:
            if task['signature'][0] == 'positive':
                task['signature'] = task['signature'][:3]
        return [task for task in tasks
                if self._local_cooldowns.get(task['signature'], -1e9) <= now
                and (self._local_budget_available(task)
                     or self._local_task is not None
                     and task['signature'] == self._local_task['signature']
                     and self._local_budget_key(self._local_task) in self._local_source_budgets)]

    def _local_budget_key(self, task):
        """Bind navigation attempts to a real source, not an activation clock."""
        if (not isinstance(task, dict) or not task.get('signature')
                or task['signature'][0] != 'positive'
                or task.get('kind') not in ('player', 'singularity', 'inspiration')
                or type(task.get('index')) is not int or not 0 <= task['index'] < 54):
            return None
        source = self._target_sources.get(task.get('source_frame_id'))
        basis = self._rotation_basis(self._mixed_feedback.get('geometry_body_basis'))
        origin = self._rotation_basis(task.get('origin'))
        context = (self._mixed_feedback.get('session_id'), self._mixed_feedback.get('map_revision'))
        if (source is None or basis is None or origin is None
                or source['context'] != context or context != self._target_source_context
                or _pose_angle(source['base_rotation'] @ basis, origin) > 1e-6):
            return None
        return (*context, task['kind'], task['index'], task['source_frame_id'], source['stamp'])

    def _local_budget_available(self, task):
        key = self._local_budget_key(task)
        if key is None:
            return False
        existing = self._local_source_budgets.get(key)
        if existing is not None:
            return existing['attempts'] < 2
        rotation = self._target_sources[task['source_frame_id']]['base_rotation']
        # A new frame or renewed glyph packet at the same orientation does not
        # reopen an exhausted local search. Body corrections are not motion.
        return all(_pose_angle(rotation, value['rotation']) >= math.radians(8.)
                   for prior, value in self._local_source_budgets.items()
                   if prior[:4] == key[:4])

    def _coverage_gain(self, readable, weights, unseen_faces):
        return CellScanPolicy._coverage_gain(self, readable, weights, unseen_faces)

    def _record_opportunities(self, rows, feedback):
        """Record actual image opportunities, never occupancy or glyph votes."""
        self._opportunity_current_valid = False
        if self.recognition_goal != 'targets' or self._target_packet_frame is None:
            return
        source = feedback.get('accepted_anchor_observation') or {}
        metadata = feedback.get('semantic_metadata') or {}
        coverage = feedback.get('target_coverage') or {}
        pose = metadata.get('pose') or {}
        rotation = self._rotation_basis(pose.get('rotation'))
        age = feedback.get('glyph_anchor_age_sec')
        stamps = (source.get('frame_time'), metadata.get('frame_time'), coverage.get('source_frame_time'))
        try:
            translation = np.asarray(pose.get('tvec'), float).reshape(3)
        except (ValueError, TypeError):
            return
        if (coverage.get('model_executed') is not True or coverage.get('coverage_valid') is not True
                or metadata.get('session_id') != self._target_source_context[0]
                or metadata.get('map_revision') != pose.get('map_revision')
                or metadata.get('map_revision') != self._target_source_context[1]
                or coverage.get('source_map_revision') != metadata.get('map_revision')
                or source.get('frame_id') != metadata.get('frame_id')
                or coverage.get('source_frame_id') != metadata.get('frame_id')
                or type(metadata.get('frame_id')) is not int
                or type(metadata.get('map_revision')) is not int
                or type(feedback.get('map_revision')) is not int
                or type(source.get('map_revision')) is not int
                or type(pose.get('map_revision')) is not int
                or type((feedback.get('refine_diagnostic') or {}).get('source_map_revision')) is not int
                or metadata['map_revision'] < 0 or metadata['frame_id'] < 0
                or type(coverage.get('source_frame_id')) is not int
                or type(coverage.get('source_map_revision')) is not int
                or type(metadata.get('generation')) is not int or metadata['generation'] < 0
                or any(type(value) not in (int,float) or not math.isfinite(value) for value in stamps)
                or max(stamps)-min(stamps) > 1e-6
                or type(age) not in (int,float) or not math.isfinite(age) or not 0 <= age < .8
                or feedback.get('fusion_paused') or rotation is None
                or _pose_angle(rotation, np.asarray(source['rotation'])) > 1e-6
                or not np.isfinite(translation).all() or translation[2] <= 0):
            return
        key = {name: metadata[name] for name in ('frame_id','generation','session_id','map_revision','frame_time')}
        if self._opportunity_source is not None:
            previous = self._opportunity_source
            if key['frame_id'] < previous['frame_id'] or (
                    key['frame_id'] == previous['frame_id'] and key != previous):
                return
            if key == previous:
                self._opportunity_current_valid = True
                return
            if (key['generation'] <= previous['generation']
                    or key['frame_time'] <= previous['frame_time']):
                return
        masks = feedback.get('target_candidate_associations') or []
        rectangles = []
        for mask in masks:
            if not isinstance(mask,dict):
                return
            try:
                box = np.asarray(mask.get('box'),float)
            except (TypeError,ValueError):
                return
            if (box.shape != (4,) or not np.isfinite(box).all() or (box[:2] < 0).any()
                    or (box[2:] <= 0).any() or box[0]+box[2] > 1280 or box[1]+box[3] > 720):
                return
            rectangles.append((box,mask))
        previous_tvec = self.tvec
        self.tvec = translation
        try:
            visible = self._readability(rotation[None], rows)[0] > 0
        finally:
            self.tvec = previous_tvec
        faces = source['accepted_faces']
        visible &= np.array([faces.get(face,0) >= 2 for face,_,_ in self._keys])
        if rectangles:
            def project(points):
                camera = np.einsum('ij,...j->...i',rotation,points)+translation
                return camera[...,:2]/camera[...,2,None]*self._K[0,0]+self._K[:2,2]
            quad = project(self._quads)
            corridor = project(self._corridors)
            regions = ((quad.min(axis=1),quad.max(axis=1)),
                       (corridor.min(axis=1)-14.,corridor.max(axis=1)+14.))
            packet = feedback.get('accepted_face_observation') or {}
            accepted = set(packet.get('cell_indices',())) if packet.get('frame_id') == key['frame_id'] else set()
            for box,mask in rectangles:
                blocked = np.zeros(54,dtype=bool)
                for lo,hi in regions:
                    overlap = np.maximum(0.,np.minimum(hi,box[:2]+box[2:]+8.)-np.maximum(lo,box[:2]-8.))
                    blocked |= (overlap[:,0] > 0) & (overlap[:,1] > 0)
                proof = mask.get('association_evidence') or {}
                owner = mask.get('cell_index')
                # Only an already accepted same-source cell can exempt its own
                # rectangle. All neighbouring/weak/unassociated masks remain.
                if (type(owner) is int and owner in accepted and 0 <= owner < 54
                        and proof.get('source_frame_id') == key['frame_id']
                        and proof.get('source_map_revision') == key['map_revision']
                        and type(proof.get('source_frame_time')) in (int,float)
                        and abs(proof['source_frame_time']-key['frame_time']) <= 1e-6):
                    blocked[owner] = False
                visible &= ~blocked
        self._opportunities |= visible
        self._opportunity_source = key
        self._opportunity_current_valid = True

    def _opportunity_pending(self):
        return (self.opportunity_navigation and self.recognition_goal == 'targets'
                and self._opportunity_source is not None
                and type(self._opportunity_context[1]) is int and self._opportunity_context[1] >= 0
                and self._opportunity_context == self._target_source_context)

    def _remember_assigned_interests(self, rows, feedback, now):
        from ._deep_dive_target_readiness import _relevant
        source = feedback.get('accepted_anchor_observation') or {}
        age = feedback.get('glyph_anchor_age_sec')
        if (self.recognition_goal != 'targets' or self._target_packet_frame is None
                or type(age) not in (int,float) or not math.isfinite(age)
                or not 0 <= age < 2. or feedback.get('fusion_paused')):
            return False
        faces = source.get('accepted_faces') or {}
        if sum(count >= 2 for count in faces.values()) < 2:
            return False
        created = False
        for candidate in feedback.get('target_candidate_associations', ()) or ():
            if not isinstance(candidate, dict) or not _relevant(candidate):
                continue
            index, kind = candidate.get('cell_index'), candidate.get('kind')
            if (type(index) is not int or not 0 <= index < 54
                    or kind not in ('player','singularity','inspiration')):
                continue
            row = rows[index]
            if (row.get('occupant_status') == 'confirmed'
                    and row.get('occupant') in ('player','singularity','inspiration')):
                continue
            proof = candidate.get('association_evidence') or {}
            rotation = self._rotation_basis(proof.get('rotation'))
            stable = proof.get('anchor_uncertainty') or {}
            stamp = proof.get('source_frame_time')
            face = self._keys[index][0]
            if (proof.get('source_frame_id') != source.get('frame_id')
                    or proof.get('source_map_revision') != self._target_source_context[1]
                    or type(stamp) not in (int,float) or not math.isfinite(stamp)
                    or not math.isclose(stamp, source['frame_time'], rel_tol=0., abs_tol=1e-6)
                    or rotation is None
                    or _pose_angle(rotation, self._target_sources[source['frame_id']]['rotation']) > 1e-6
                    or type(proof.get('target_face_glyph_count')) is not int
                    or proof['target_face_glyph_count'] < 2 or faces.get(face,0) < 2
                    or stable.get('ready') is not True or stable.get('winner_index') != index
                    or type(proof.get('error')) not in (int,float)
                    or not math.isfinite(proof['error']) or not 0 <= proof['error'] < .30
                    or type(proof.get('margin')) not in (int,float)
                    or not math.isfinite(proof['margin']) or proof['margin'] <= .12):
                continue
            old = self._assigned_interests.get(index)
            if old is None:
                self._assigned_interests[index] = dict(index=index, kind=kind,
                    source_frame_id=source['frame_id'], source_frame_time=stamp,
                    context=self._target_source_context, expires=now+12., attempts=0,
                    requested=True)
                created = True
            # A repeated or later source cannot restart TTL/attempt budgets.
        return created

    def _active_interests(self, rows, now):
        if self.recognition_goal != 'targets' or self._local_task is not None:
            return []
        return [interest for index,interest in self._assigned_interests.items()
            if interest['context'] == self._target_source_context
            and now < interest['expires'] and interest['attempts'] < 2
            and not (rows[index].get('occupant_status') == 'confirmed'
                     and rows[index].get('occupant') in ('player','singularity','inspiration'))]

    def _planning_weights(self, rows, now, pending, weights):
        interests = self._active_interests(rows, now)
        navigation = self._opportunity_pending()
        if not interests and not navigation:
            return pending, weights
        pending, weights = pending.copy(), weights.copy()
        if navigation:
            missing = ~self._opportunities
            pending |= missing
            weights[missing] = np.maximum(weights[missing],1.)
        for interest in interests:
            index = interest['index']
            pending[index] = True
            weights[index] = max(1., weights[index])
        return pending, weights

    def _objective(self, rows):
        complete, seen = super()._objective(rows)
        if self._opportunity_pending():
            seen = seen & self._opportunities
        now = getattr(self,'_interest_now',0.)
        for interest in self._active_interests(rows,now):
            complete[interest['index']] = False
        return complete, seen

    def _planning_endpoint(self, best, score, candidates, paths, costs, readable,
                           adjusted, novelty, observed, rows, now):
        interests = self._active_interests(rows, now)
        navigation = self._opportunity_pending() and not self._opportunities.all()
        feedback = self._mixed_feedback
        # These are validated historical navigation inputs. Current-image
        # freshness is enforced by the controller and recognition gates, not
        # by consuming the same source again merely to score future cameras.
        if not (interests or navigation) or feedback.get('fusion_paused'):
            return best
        # Use Cell's existing navigation prior for incoming unknown glyphs.
        # Requiring every projected checkpoint to have already learned glyphs
        # blocks the very approach that would expose a clipped target. These
        # predictions never supply votes or renew the actual source anchor.
        endpoint_support = self._planning_anchor_support(candidates, rows)
        route_support = self._route_anchor_support(paths, rows, allow_potential=True)
        eligible = (novelty & np.isfinite(endpoint_support) & (endpoint_support > 0)
                    & np.isfinite(route_support) & (route_support > 0))
        usable = eligible & np.any(np.stack([
            (readable[:,value['index']] > 0) & (adjusted[:,value['index']] > 0)
            for value in interests]),axis=0) if interests else np.zeros(len(candidates),dtype=bool)
        choices = list(np.flatnonzero(usable))
        options = []
        for interest in interests:
            index = interest['index']
            for choice in choices:
                if readable[choice,index] <= 0 or adjusted[choice,index] <= 0:
                    continue
                utility = adjusted[choice,index]/(1.+costs[choice]/math.radians(100.))
                options.append((float(utility),int(choice),interest))
        if options:
            _, choice, interest = max(options,key=lambda value:(value[0],-value[1]))
            interest['attempts'] += 1
            self.stats['assigned_hint_attempts'] = self.stats.get('assigned_hint_attempts',0)+1
            return choice
        if not navigation:
            return best
        # Seen upper cells must not hide a face's never-exposed lower edge.
        # Score the number of remaining cells, with a small whole-face,
        # near-normal bonus; these predictions cannot complete any observation.
        gain = np.zeros(len(candidates))
        camera = np.einsum('bij,nj->bni',candidates,self._points[4::9])+np.asarray(self.tvec).reshape(3)
        normals = np.einsum('bij,nj->bni',candidates,self._normals[4::9])
        cosine = -np.sum(camera*normals,axis=2)/np.linalg.norm(camera,axis=2)
        for face in range(6):
            indices = np.arange(face*9,(face+1)*9)
            missing = indices[~self._opportunities[indices]]
            if not len(missing):
                continue
            exposed = np.where(readable[:,missing]>0,adjusted[:,missing],0.).sum(axis=1)/9.
            full = (readable[:,indices]>0).all(axis=1) & (cosine[:,face]>=.90)
            gain += exposed*(1.+.10*full)
        utility = gain*endpoint_support*route_support/(1.+np.asarray(costs)/math.radians(100.))
        utility[~eligible] = 0.
        if not np.isfinite(utility).all() or not (utility>0).any():
            return best
        choice = int(np.argmax(utility))
        self.stats['opportunity_navigation_plans'] = self.stats.get('opportunity_navigation_plans',0)+1
        return choice

    def summary(self):
        result = super().summary()
        result['assigned_interests'] = [dict(index=value['index'],kind=value['kind'],
            source_frame_id=value['source_frame_id'],expires=value['expires'],attempts=value['attempts'])
            for value in self._assigned_interests.values()]
        result['local_source_budgets'] = [dict(kind=key[2], index=key[3],
            source_frame_id=key[4], source_frame_time=key[5], attempts=value['attempts'])
            for key, value in self._local_source_budgets.items()]
        result['navigation_opportunities'] = dict(covered=int(self._opportunities.sum()),
            enabled=self.opportunity_navigation,
            by_face={self._keys[i*9][0]:int(self._opportunities[i*9:(i+1)*9].sum()) for i in range(6)},
            uncovered_indices=np.flatnonzero(~self._opportunities).tolist(),
            source=dict(self._opportunity_source) if self._opportunity_source else None,
            plans=self.stats.get('opportunity_navigation_plans',0))
        return result

    def _local_planning_rows(self, rows):
        """Apply one real positive's sprite only to a private navigation view."""
        task = self._local_task
        if (not isinstance(task,dict) or not task.get('signature')
                or task['signature'][0] != 'positive'
                or type(task.get('index')) is not int or not 0 <= task['index'] < 54
                or task.get('kind') not in ('player','inspiration','singularity')):
            return rows
        revision = self._mixed_feedback.get('semantic_map_revision', self._mixed_feedback.get('map_revision'))
        if self._target_source_context != (self._mixed_feedback.get('session_id'),
                                           self._mixed_feedback.get('map_revision')):
            return rows
        origin = self._rotation_basis(task.get('origin'))
        if origin is None:
            return rows
        evidence = []
        for entry in rows[task['index']].get('evidence', ()):
            if (not isinstance(entry,dict) or entry.get('frame_id') != task.get('source_frame_id')
                    or entry.get('occupant') != task['kind']
                    or type(entry.get('confidence')) not in (int,float)
                    or not math.isfinite(entry['confidence']) or entry['confidence'] < .70):
                continue
            pose = self._evidence_pose(entry, revision)
            if pose is None or _pose_angle(pose, origin) > 1e-6:
                continue
            try:
                box = np.asarray(entry.get('target_box'),float)
            except (ValueError,TypeError):
                continue
            if (box.shape != (4,) or not np.isfinite(box).all() or (box[:2] < 0).any()
                    or (box[2:] <= 0).any() or box[0]+box[2] > 1280 or box[1]+box[3] > 720):
                continue
            evidence.append(deepcopy(entry))
        if not evidence:
            return rows
        target_row = dict(rows[task['index']], occupant=task['kind'], evidence=evidence)
        if self._sprite_footprint(target_row) is None:
            return rows
        planning = [dict(row) for row in rows]
        planning[task['index']] = target_row
        return planning

    def _set_local_route(self, observed, rows, axes, now):
        task = self._local_task
        if not self._local_budget_available(task):
            return False
        if self._local_goal is None:
            # Steer a small reachable change from the actual current pose,
            # never a round trip to an old source followed by its old endpoint.
            candidates = np.array([_matrix(_unit(axes[axis])*math.radians(sign*angle)) @ observed
                for axis in (0, 1) for sign in (-1, 1) for angle in (12., 16.)])
            planning_rows = self._local_planning_rows(rows)
            support = self._planning_anchor_support(candidates, planning_rows)
            routes = self._route_anchor_support(
                [self._short_waypoints(observed, [pose]) for pose in candidates], planning_rows, allow_potential=True)
            readable = self._readability(candidates, planning_rows)
            index = task['index']
            gain = readable[:, index] if index is not None else readable.sum(axis=1)/9.
            cost = np.array([_pose_angle(pose, observed) for pose in candidates])
            novelty = np.array([_pose_angle(pose, task['origin']) >= math.radians(12.)
                                for pose in candidates])
            score = (1.+gain)*np.sqrt(support*routes)/(1.+cost/math.radians(20.))
            score[~novelty] = -1.
            if not np.any(novelty):
                return False
            self._local_goal = candidates[int(np.argmax(score))]
        super()._set_local_route(observed, rows, axes, now)
        key = self._local_budget_key(task)
        budget = self._local_source_budgets.setdefault(key, dict(attempts=0,
            rotation=self._target_sources[task['source_frame_id']]['base_rotation'].copy()))
        budget['attempts'] += 1
        return True

    def _plan(self, observed, rows, axes, now):
        if self.recognition_goal == 'targets' and self._local_task is not None:
            if self._local_attempts < 2 and now-self._local_started <= 6.:
                self._mixed_state = 'local_confirmation'
                if self._set_local_route(observed, rows, axes, now):
                    return
            self._local_cooldowns[self._local_task['signature']] = now+4.
            self.insufficient.append(dict(reason='target_first_local_evidence_unresolved',
                kind=self._local_task['kind'], source_frame_id=self._local_task['source_frame_id'],
                cell_index=self._local_task['index']))
            self._local_task = self._local_goal = None
        self._mixed_state = 'coverage'
        # Preserve the original cell planner's multi-face coverage and costs;
        # there is deliberately no Mixed coverage-face lock or target rewrite.
        return CellScanPolicy._plan(self, observed, rows, axes, now)

    def _assigned_interest_interrupts_route(self):
        return True

    def choose(self, rotation, cells, response_axes, quality=1.0,
               observed_rotation=None, tvec=None, elapsed=None,
               semantic_revision=None, anchor_feedback=None):
        feedback = anchor_feedback or {}
        basis = self._remember_target_source(feedback)
        now = time.monotonic() if elapsed is None else float(elapsed)
        self._interest_now = now
        self._record_opportunities(self._rows(cells),feedback)
        new_interest = self._remember_assigned_interests(self._rows(cells),feedback,now)
        if (new_interest and self._assigned_interest_interrupts_route()
                and self._local_task is None and self._anchor_recovery is None):
            self._route = []
            self._at_goal_since = None
        if self._local_task is not None and now-self._local_started > 6.:
            task = self._local_task
            self._local_cooldowns[task['signature']] = now+4.
            self.insufficient.append(dict(reason='target_first_local_evidence_unresolved',
                kind=task['kind'], source_frame_id=task['source_frame_id'], cell_index=task['index']))
            self._local_task = self._local_goal = None
            if self._anchor_recovery is None:
                self._route = []
        if basis is not None and self._local_task is not None and self._local_basis is not None:
            change = self._local_basis.T @ basis
            self._local_task['origin'] = self._local_task['origin'] @ change
            if self._local_goal is not None:
                self._local_goal = self._local_goal @ change
            if self._anchor_recovery is None:
                self._route = [pose @ change for pose in self._route]
                if self._final_rotation is not None:
                    self._final_rotation = self._final_rotation @ change
        elif basis is None and self._local_task is not None:
            self._local_task = self._local_goal = None
            if self._anchor_recovery is None:
                self._route = []
        self._local_basis = basis.copy() if basis is not None else None
        result = super().choose(rotation, cells, response_axes, quality,
            observed_rotation, tvec, elapsed, semantic_revision, feedback)
        result['target_first_scan'] = result.pop('mixed_scan', self.summary())
        if result.get('reason') == 'mixed_source_local_confirmation':
            result['reason'] = 'target_first_source_local_confirmation'
        elif result.get('reason') == 'mixed_overlap_face_frontier':
            result['reason'] = 'missing_cells_projected_readable'
        elif result.get('reason') == 'mixed_targets_ready':
            result['reason'] = 'target_first_targets_ready'
        return result
