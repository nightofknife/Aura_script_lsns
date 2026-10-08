"""Target scheduling is opt-in and never changes real atlas evidence."""
from copy import deepcopy
import inspect
import json
import math
from pathlib import Path
import textwrap

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix, _pose_angle
from plans.resonance_pc.src.actions._deep_dive_target_first_scan_policy import TargetFirstCellScanPolicy
from test_deep_dive_mixed_scan_policy import AXES, cells, lone_positive, snapshot


def packet(rows, *, pose=None, source_basis=None, current_basis=None, frame=9, stamp=10.):
    pose = np.eye(3) if pose is None else pose
    source_basis = np.eye(3) if source_basis is None else source_basis
    current_basis = source_basis if current_basis is None else current_basis
    feedback = snapshot(rows)
    feedback.update(session_id=7, geometry_body_basis=current_basis.tolist(),
        glyph_anchor_at=stamp, glyph_anchor_frame_id=frame,
        glyph_anchor_rotation=(pose @ source_basis).tolist(), semantic_revision=frame,
        accepted_face_observation=dict(frame_id=frame, rotation=(pose @ source_basis).tolist(), cell_indices=[50]),
        semantic_metadata=dict(frame_id=frame),
        refine_diagnostic=dict(source_frame_id=frame, source_frame_time=stamp,
            source_map_revision=0, renewed=True, accepted_faces={'U':4,'B':3}),
        accepted_anchor_observation=dict(frame_id=frame, frame_time=stamp,
            session_id=7, map_revision=0, rotation=(pose @ source_basis).tolist(),
            body_basis=source_basis.tolist(), accepted_faces={'U':4,'B':3}))
    return feedback


def pending(rows, *, pose=None, basis=None):
    pose = np.eye(3) if pose is None else pose
    basis = np.eye(3) if basis is None else basis
    lone_positive(rows, pose=pose @ basis)
    rows[50]['evidence'][0]['association_evidence']['source_frame_time'] = 10.


def test_real_single_positive_interrupts_ordinary_coverage():
    rows = cells(); pending(rows)
    before = deepcopy(rows)
    policy = TargetFirstCellScanPolicy(expected_inspirations=2)
    policy._route = [_matrix(np.array([0.,1.,0.]))]
    result = policy.choose(np.eye(3), rows, AXES, elapsed=10., anchor_feedback=packet(rows))
    assert result['reason'] == 'target_first_source_local_confirmation'
    assert policy._local_task['index'] == 50 and policy._coverage_face is None
    assert 12.-1e-6 <= math.degrees(_pose_angle(policy._final_rotation, np.eye(3))) <= 16.+1e-6
    assert rows == before


def test_near_angle_two_groups_keep_same_bounded_local_task():
    rows = cells(); pending(rows)
    policy = TargetFirstCellScanPolicy(expected_inspirations=2)
    policy.choose(np.eye(3), rows, AXES, elapsed=10., anchor_feedback=packet(rows))
    task, begun = policy._local_task, policy._local_started
    other = deepcopy(rows[50]['evidence'][0]); other.update(frame_id=11, group=6)
    other['association_evidence'].update(source_frame_id=11, source_frame_time=11.)
    rows[50]['evidence'].append(other); rows[50]['occupant_evidence_counts']['inspiration'] = 2
    policy.choose(np.eye(3), rows, AXES, elapsed=10.1, anchor_feedback=packet(rows, frame=11, stamp=11.))
    assert policy._local_task is task and policy._local_started == begun
    assert rows[50]['occupant_status'] == 'unknown'


@pytest.mark.parametrize('defect', ['basis','frame','time','session','map','rotation','unrenewed'])
def test_unbound_positive_source_does_not_start_local_route(defect):
    rows = cells(); pending(rows)
    feedback = packet(rows)
    if defect == 'basis': feedback.pop('geometry_body_basis')
    elif defect == 'frame': feedback['accepted_anchor_observation']['frame_id'] = 123
    elif defect == 'time': feedback['accepted_anchor_observation']['frame_time'] = 12.
    elif defect == 'session': feedback['accepted_anchor_observation']['session_id'] = 123
    elif defect == 'map': feedback['accepted_anchor_observation']['map_revision'] = 123
    elif defect == 'rotation': rows[50]['evidence'][0]['association_evidence']['rotation'][0][0] = 2.
    elif defect == 'unrenewed': feedback['refine_diagnostic']['renewed'] = False
    policy = TargetFirstCellScanPolicy(expected_inspirations=2)
    policy._remember_target_source(feedback); policy._mixed_feedback = feedback
    assert policy._tasks(rows, feedback, np.eye(3), 10.) == []


def test_local_move_starts_from_actual_pose_not_old_origin():
    rows = cells(); pending(rows)
    observed = _matrix(np.array([0., math.radians(20.), 0.]))
    policy = TargetFirstCellScanPolicy(expected_inspirations=2)
    policy.choose(observed, rows, AXES, elapsed=10., anchor_feedback=packet(rows))
    assert math.degrees(_pose_angle(policy._final_rotation, observed)) <= 16.+1e-6
    assert math.degrees(_pose_angle(policy._final_rotation, np.eye(3))) >= 12.-1e-6


def test_source_basis_and_active_goal_follow_noncommuting_body_correction():
    rows = cells()
    source_basis = _matrix(np.array([.02, -.03, .01]))
    current_basis = source_basis @ _matrix(np.array([0., .01, .02]))
    pending(rows, basis=source_basis)
    policy = TargetFirstCellScanPolicy(expected_inspirations=2)
    policy.choose(current_basis, rows, AXES, elapsed=10.,
                  anchor_feedback=packet(rows, source_basis=source_basis, current_basis=current_basis))
    assert np.allclose(policy._local_task['origin'], current_basis)
    goal = policy._local_goal.copy()
    newest = current_basis @ _matrix(np.array([-.02, .01, 0.]))
    policy.choose(newest, rows, AXES, elapsed=10.1,
                  anchor_feedback=packet(rows, source_basis=source_basis, current_basis=newest))
    assert np.allclose(policy._local_goal, goal @ current_basis.T @ newest)


def test_global_gain_is_exact_cell_gain_without_face_lock():
    policy = TargetFirstCellScanPolicy()
    policy._coverage_face = 'B'
    readable = np.ones((2,54)); weights = np.ones(54)
    assert np.array_equal(policy._coverage_gain(readable,weights,{'B','R'}),
                          CellScanPolicy._coverage_gain(policy,readable,weights,{'B','R'}))


def test_finished_local_task_returns_to_original_global_planner(monkeypatch):
    rows = cells(); pending(rows)
    policy = TargetFirstCellScanPolicy(expected_inspirations=2)
    policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=packet(rows))
    seen=[]
    def plan(self, observed, rows, axes, now): seen.append(observed.copy())
    monkeypatch.setattr(CellScanPolicy,'_plan',plan)
    policy._local_attempts=2
    observed=policy._local_goal.copy()
    policy._plan(observed,rows,AXES,11.)
    assert policy._local_task is None and policy._coverage_face is None
    assert np.allclose(seen[0],observed)
    assert policy._tasks(rows,packet(rows),np.eye(3),12.) == []


def test_full_mode_keeps_cell_planning(monkeypatch):
    policy=TargetFirstCellScanPolicy(recognition_goal='full')
    called=[]
    monkeypatch.setattr(CellScanPolicy,'_plan',lambda *args: called.append(True))
    policy._plan(np.eye(3),cells(),AXES,10.)
    assert called == [True]


def test_local_task_expires_without_waiting_for_a_replan(monkeypatch):
    rows=cells(); pending(rows)
    policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=packet(rows))
    signature=policy._local_task['signature']
    global_calls=[]
    def global_plan(self, observed, rows, axes, now):
        global_calls.append(now)
        self._route=[_matrix(np.array([0.,.3,0.])) @ observed]
        self._final_rotation=self._route[-1]
        self._target_indices=[50]
        self._started=now
        self._at_goal_since=None
    monkeypatch.setattr(CellScanPolicy,'_plan',global_plan)
    policy.choose(np.eye(3),rows,AXES,elapsed=16.01,anchor_feedback=packet(rows))
    assert policy._local_task is None and global_calls == [16.01]
    assert policy._local_cooldowns[signature] == 20.01
    assert rows[50]['occupant_status'] == 'unknown'


@pytest.mark.parametrize('candidate', [
    dict(kind='inspiration', confidence=.95, confirmable=True),
    dict(kind='inspiration', cell_index=26, confidence=.95, confirmable=True),
    dict(kind='player', confidence=.95, confirmable=True),
    dict(kind='singularity', cell_index=31, confidence=.95, confirmable=True),
])
def test_unassociated_or_confirmed_entity_candidate_does_not_steal_global_route(monkeypatch,candidate):
    rows=cells()
    # Real missing coverage remains a reason to run the normal global planner.
    rows[45].update(occupant='unknown', occupant_status='unknown', evidence=[],
                    occupant_evidence_counts={})
    feedback=packet(rows)
    feedback['target_candidate_associations']=[candidate]
    before=deepcopy(feedback)
    policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    global_calls=[]
    def global_plan(self, observed, atlas, axes, now):
        global_calls.append(now)
        self._route=[_matrix(np.array([0.,.3,0.])) @ observed]
        self._final_rotation=self._route[-1]
        self._target_indices=[45]
        self.target_face='B'
        self._started=now
        self._at_goal_since=None
    monkeypatch.setattr(CellScanPolicy,'_plan',global_plan)
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    assert policy._local_task is None
    if candidate.get('cell_index') is None:
        assert global_calls == [10.]
    else:
        # A correctly associated already-confirmed entity may close inventory;
        # that should stop instead of inventing another local route.
        assert global_calls == [] and result['reason'] == 'target_first_targets_ready'
    assert result['reason'] != 'target_first_source_local_confirmation'
    assert feedback == before  # Candidates still constrain public readiness.


def test_generic_candidate_after_six_faces_still_requires_global_localization():
    rows=cells()
    feedback=packet(rows)
    feedback['target_candidate_associations']=[dict(kind='inspiration',confidence=.95,confirmable=True)]
    policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    policy._remember_target_source(feedback)
    policy._mixed_feedback=feedback
    assert feedback['faces_observed'] == 6
    assert policy._tasks(rows,feedback,np.eye(3),10.) == []
    assert feedback['target_candidate_associations'] == [dict(kind='inspiration',confidence=.95,confirmable=True)]


def test_real_positive_still_wins_when_a_generic_candidate_is_present():
    rows=cells(); pending(rows)
    feedback=packet(rows)
    feedback['target_candidate_associations']=[dict(kind='singularity',confidence=.95,confirmable=True)]
    policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    assert result['reason'] == 'target_first_source_local_confirmation'
    assert policy._local_task['index'] == 50 and policy._local_task['signature'][0] == 'positive'


def assigned_packet(rows, *, index=50, frame=9, stamp=10.):
    result=packet(rows,frame=frame,stamp=stamp)
    result['target_candidate_associations']=[dict(kind='inspiration',cell_index=index,
        confidence=.8868,confirmable=True,box=[828,580,62,60],
        association_evidence=dict(source_frame_id=frame,source_frame_time=stamp,source_map_revision=0,
            rotation=np.eye(3).tolist(),error=.1,margin=.3,target_face_glyph_count=3,
            anchor_uncertainty=dict(ready=True,winner_index=index)))]
    return result


def unknown_target(rows,index=50):
    rows[index].update(occupant='unknown',occupant_status='unknown',confidence=0.,
                       evidence=[],occupant_evidence_counts={})


def interest_subject():
    rows=cells(); unknown_target(rows)
    policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    feedback=assigned_packet(rows)
    policy._remember_target_source(feedback);policy._mixed_feedback=feedback
    assert policy._remember_assigned_interests(rows,feedback,10.)
    return policy,rows,feedback


def test_assigned_interest_retains_navigation_only_without_any_positive_vote():
    policy,rows,feedback=interest_subject();before=deepcopy(rows)
    later=packet(rows,frame=10,stamp=11.)
    policy._remember_target_source(later);policy._mixed_feedback=later
    assert not policy._remember_assigned_interests(rows,later,11.)
    pending,weights=policy._planning_weights(rows,11.,np.zeros(54,bool),np.zeros(54))
    assert pending[50] and weights[50] == 1.
    assert rows == before and rows[50]['evidence'] == []


@pytest.mark.parametrize('defect',['none','source','time','map','session','basis','local','quant','winner','margin','rotation','known'])
def test_unsupported_candidate_cannot_create_global_interest(defect):
    rows=cells();unknown_target(rows)
    policy=TargetFirstCellScanPolicy(expected_inspirations=2);feedback=assigned_packet(rows)
    candidate=feedback['target_candidate_associations'][0];proof=candidate['association_evidence']
    if defect=='none':candidate['cell_index']=None
    elif defect=='source':proof['source_frame_id']=99
    elif defect=='time':proof['source_frame_time']=11.
    elif defect=='map':proof['source_map_revision']=1
    elif defect=='session':feedback['accepted_anchor_observation']['session_id']=99
    elif defect=='basis':feedback['geometry_body_basis'][0][0]=2.
    elif defect=='local':proof['target_face_glyph_count']=1
    elif defect=='quant':proof['anchor_uncertainty']['ready']=False
    elif defect=='winner':proof['anchor_uncertainty']['winner_index']=49
    elif defect=='margin':proof['margin']=.12
    elif defect=='rotation':proof['rotation']=_matrix(np.array([0.,.2,0.])).tolist()
    elif defect=='known':rows[50].update(occupant='inspiration',occupant_status='confirmed')
    policy._remember_target_source(feedback)
    assert not policy._remember_assigned_interests(rows,feedback,10.)
    assert not policy._assigned_interests


def test_same_or_new_source_never_resets_interest_expiry_or_attempt_count():
    policy,rows,feedback=interest_subject()
    interest=policy._assigned_interests[50];interest['attempts']=1
    assert not policy._remember_assigned_interests(rows,feedback,11.)
    newer=assigned_packet(rows,frame=10,stamp=11.)
    policy._remember_target_source(newer)
    assert not policy._remember_assigned_interests(rows,newer,12.)
    assert interest['expires'] == 22. and interest['attempts'] == 1
    assert policy._active_interests(rows,22.) == []


@pytest.mark.parametrize('age,ready',[(.906,True),(1.2,True),(2.,False),(-.1,False),
                                     (float('nan'),False)])
def test_delayed_verified_source_can_create_navigation_without_fresh_positive_gate(age,ready):
    rows=cells();unknown_target(rows);before=deepcopy(rows)
    policy=TargetFirstCellScanPolicy();feedback=assigned_packet(rows)
    feedback['glyph_anchor_age_sec']=age
    policy._remember_target_source(feedback)
    assert policy._remember_assigned_interests(rows,feedback,11.2) is ready
    if math.isfinite(age) and age >= .9:
        assert policy._packet_pose(feedback) is None
    assert rows==before
    if ready:
        assert policy._assigned_interests[50]['expires']==23.2
        assert policy._assigned_interests[50]['attempts']==0


@pytest.mark.parametrize('change',['map','session','invalid_map'])
def test_interests_clear_before_missing_basis_context_gate(change):
    policy,rows,feedback=interest_subject()
    changed=deepcopy(feedback);changed['geometry_body_basis']=None
    if change=='map':changed['map_revision']=1
    elif change=='session':changed['session_id']=8
    else:changed['map_revision']=False
    policy._remember_target_source(changed)
    assert not policy._assigned_interests and not policy._active_interests(rows,11.)


def test_historical_hint_survives_latest_failed_source_without_reauthorizing_or_refreshing(monkeypatch):
    policy,rows,feedback=interest_subject();before=deepcopy(rows)
    latest=packet(rows,frame=10,stamp=11.)
    latest['glyph_anchor_age_sec']=1.2
    latest['refine_diagnostic']['renewed']=False
    latest['accepted_anchor_observation']['accepted_faces']={'B':4}
    policy._remember_target_source(latest);policy._mixed_feedback=latest
    assert policy._target_packet_frame is None
    candidates=np.array([np.eye(3),_matrix(np.array([0.,.4,0.]))])
    readable=np.zeros((2,54));readable[1,50]=1.
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda rotations,atlas:np.ones(len(rotations)))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,atlas,**kwargs:np.ones(len(paths)))
    args=(0,np.array([100.,0.]),candidates,[[candidates[0]],[candidates[1]]],
          [0.,.4],readable,readable,np.ones(2,bool),np.eye(3),rows)
    assert policy._planning_endpoint(*args,11.)==1
    assert policy._planning_endpoint(*args,11.1)==1
    assert policy._planning_endpoint(*args,11.2)==0
    assert policy._assigned_interests[50]['expires']==22.
    assert policy._planning_endpoint(*args,22.)==0
    assert rows==before


def test_interest_context_change_discards_navigation_history():
    policy,rows,feedback=interest_subject()
    changed=deepcopy(feedback);changed['session_id']=8
    changed['accepted_anchor_observation']['session_id']=8
    policy._remember_target_source(changed)
    assert policy._assigned_interests == {}


@pytest.mark.parametrize('blocked',['quad','failure','novelty','endpoint','route','paused'])
def test_hint_cannot_force_unreadable_or_unanchored_fallback_endpoint(monkeypatch,blocked):
    policy,rows,feedback=interest_subject()
    candidates=np.array([np.eye(3),_matrix(np.array([0.,.4,0.]))])
    readable=np.zeros((2,54));readable[1,50]=1.
    novelty=np.ones(2,bool)
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda rotations,atlas:
        np.full(len(rotations),0. if blocked=='endpoint' else .85))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,atlas,**kwargs:
        np.full(len(paths),0. if blocked=='route' else .85))
    adjusted=readable.copy()
    if blocked=='quad':readable[1,50]=0.
    if blocked=='novelty':novelty[1]=False
    if blocked=='paused':feedback['fusion_paused']=True
    if blocked=='failure':adjusted[1,50]=0.
    choice=policy._planning_endpoint(0,np.array([1.,-1.]),candidates,[[candidates[0]],[candidates[1]]],
        [0.,.4],readable,adjusted,novelty,np.eye(3),rows,11.)
    assert choice == 0 and policy._assigned_interests[50]['attempts'] == 0


def test_safe_whole_quad_hint_beats_unseen_frontier_but_has_two_attempt_limit(monkeypatch):
    policy,rows,feedback=interest_subject()
    candidates=np.array([np.eye(3),_matrix(np.array([0.,.4,0.]))])
    readable=np.zeros((2,54));readable[1,50]=.8
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda rotations,atlas:
        np.full(len(rotations),.85))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,atlas,**kwargs:
        np.full(len(paths),.85))
    args=(0,np.array([100.,0.]),candidates,[[candidates[0]],[candidates[1]]],
          [0.,.4],readable,readable,np.ones(2,bool),np.eye(3),rows,11.)
    assert policy._planning_endpoint(*args) == 1
    assert policy._planning_endpoint(*args) == 1
    assert policy._planning_endpoint(*args) == 0
    assert policy._assigned_interests[50]['attempts'] == 2
    assert rows[50]['occupant'] == 'unknown' and rows[50]['evidence'] == []


def test_default_cell_endpoint_and_weights_hooks_preserve_old_decision():
    from plans.resonance_pc.src.actions import _deep_dive_scan_policy as module
    source=textwrap.dedent(inspect.getsource(CellScanPolicy._plan))
    old=source.replace('def _plan(', 'def legacy_plan(').replace(
        '    pending, weights = self._planning_weights(rows, now, pending, weights)\n','').replace(
        '    best = self._planning_endpoint(best, score, candidates, paths, costs,\n'
        '        readable, adjusted, novelty, observed, rows, now)\n','')
    assert '_planning_weights(' not in old and '_planning_endpoint(' not in old
    namespace=dict(vars(module));exec(old,namespace)
    rows=cells()
    for row in rows:row['node_status']='known'
    for row in rows[45:54]:row.update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    policies=[CellScanPolicy(recognition_goal='targets',expected_inspirations=2) for _ in range(2)]
    for policy in policies:
        policy._last_gain=np.zeros(54);policy._last_attempt=np.full(54,-6.)
    observed=_matrix(np.array([0.,.5,0.]))
    policies[0]._plan(observed,rows,AXES,10.)
    namespace['legacy_plan'](policies[1],observed,rows,AXES,10.)
    assert policies[0]._target_indices == policies[1]._target_indices
    assert policies[0].target_face == policies[1].target_face
    assert np.array_equal(policies[0]._final_rotation,policies[1]._final_rotation)
    assert all(np.array_equal(a,b) for a,b in zip(policies[0]._route,policies[1]._route))


@pytest.mark.parametrize('frame',[125,157,167,394])
def test_real_clipped_f22_sources_create_only_bound_navigation_interest(frame):
    fixture=json.loads((Path(__file__).parent/'fixtures/deep_dive_assigned_candidates_09.json').read_text())
    source=next(row for row in fixture['sources'] if row['frame_id']==frame)
    feedback=source['feedback']
    rows=cells();unknown_target(rows,26);before=deepcopy(rows)
    policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    policy._remember_target_source(feedback)
    assert policy._target_packet_frame == frame
    assert policy._remember_assigned_interests(rows,feedback,source['elapsed_sec'])
    interest=policy._assigned_interests[26]
    assert interest['source_frame_id'] == frame
    assert interest['expires'] == source['elapsed_sec']+12.
    assert interest['attempts'] == 0 and interest['kind'] == 'inspiration'
    assert feedback['target_candidate_associations'][0]['association_evidence']['target_face_glyph_count'] == 2
    assert rows == before and rows[26]['evidence'] == []


def test_repeated_same_interest_requests_no_replan_or_capture_clock_reset():
    policy,rows,feedback=interest_subject()
    assert not policy._remember_assigned_interests(rows,feedback,10.1)
    assert policy._assigned_interests[50]['expires'] == 22.
    assert policy._assigned_interests[50]['attempts'] == 0


@pytest.mark.parametrize('frame',[125,157,167,394])
def test_real_known_anchor_lower_bound_can_navigate_clipped_assigned_interest(frame):
    fixture=json.loads((Path(__file__).parent/'fixtures/deep_dive_assigned_candidates_09.json').read_text())
    source=next(row for row in fixture['sources'] if row['frame_id']==frame)
    feedback=source['feedback'];policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    rows=[dict(occupant='unknown',occupant_status='unknown',node_status='unknown',
               confidence=0.,evidence=[]) for _ in range(54)]
    # Actual same-source known glyph indices, not the final board atlas.
    for index in feedback['refine_diagnostic']['confirmed_cell_indices']:
        rows[index].update(occupant='none',node_status='known',confidence=.7)
    before=deepcopy(rows)
    proof=feedback['target_candidate_associations'][0]['association_evidence']
    policy.tvec=np.asarray(proof['tvec']);observed=np.asarray(proof['rotation'])
    policy._remember_target_source(feedback);policy._mixed_feedback=feedback
    assert policy._remember_assigned_interests(rows,feedback,source['elapsed_sec'])
    # Saved actual 150px calibration responses from this same scan.
    axes={0:np.array([.0036915791273675704,-.27246852216273193,-.1509528650463846])/150.,
          1:np.array([.32192062071949107,.006805733995435682,-.0037349483549360685])/150.}
    angles=np.radians((-110.,-75.,-45.,-22.,0.,22.,45.,75.,110.));paths=[]
    turns={axis:[_matrix(vector/np.linalg.norm(vector)*angle) for angle in angles]
           for axis,vector in axes.items()}
    for first,second in ((0,1),(1,0)):
        for i,a in enumerate(angles):
            mid=turns[first][i]@observed
            for j,b in enumerate(angles):
                if abs(a)+abs(b)<math.radians(10.):continue
                end=turns[second][j]@mid
                paths.append(([mid] if abs(a)>.01 else [])+([end] if abs(b)>.01 else []))
    candidates,paths,costs=policy._coverage_prefixes(observed,paths)
    readable=policy._readability(candidates,rows)
    novelty=np.array([_pose_angle(pose,observed)>=math.radians(9.) for pose in candidates])
    best=next(i for i in range(len(candidates)) if readable[i,26]==0.)
    chosen=policy._planning_endpoint(best,np.zeros(len(candidates)),candidates,paths,costs,
        readable,readable,novelty,observed,rows,source['elapsed_sec'])
    assert chosen != best and readable[chosen,26]>0 and novelty[chosen]
    assert policy._planning_anchor_support(candidates,rows)[chosen]>0
    assert policy._route_anchor_support(paths,rows,allow_potential=True)[chosen]>0
    assert policy._assigned_interests[26]['attempts']==1
    assert rows==before and rows[26]['evidence']==[]
