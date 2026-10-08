"""Finite target framing is a navigation experiment, not absence evidence."""
from copy import deepcopy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_target_framed_scan_policy import TargetFramedCellScanPolicy
from plans.resonance_pc.src.actions._deep_dive_target_first_scan_policy import TargetFirstCellScanPolicy
from plans.resonance_pc.src.actions._deep_dive_scan_policy import _pose_angle
from test_deep_dive_target_navigation_opportunities import actual_packet
from test_deep_dive_mixed_scan_policy import AXES,cells


def prepared():
    policy=TargetFramedCellScanPolicy()
    rows=cells(); value=actual_packet(rows)
    indices=[0,2,3,6,45,47,50]
    value['accepted_anchor_observation']['accepted_cell_indices']=indices
    value['refine_diagnostic']['accepted_cell_indices']=indices.copy()
    policy._sync_framed_context(value)
    return policy,rows,value


def test_real_framed_projection_accepts_column_translation_and_restores_it():
    policy,rows,value=prepared()
    rotation=np.asarray(value['semantic_metadata']['pose']['rotation'])[None]
    column=np.array([[0.],[.42],[42.]])
    policy.tvec=column
    default=policy._framed_readable(rotation,rows)
    assert default.shape==(1,54) and np.isfinite(default).all()
    assert policy.tvec is column
    explicit=policy._framed_readable(rotation,rows,column.reshape(3))
    assert np.array_equal(default,explicit) and policy.tvec is column
    # Also exercise the actual planner's full finite candidate batch.
    candidates,_,_=policy._short_candidates(rotation[0],AXES)
    assert policy._framed_readable(candidates,rows).shape==(36,54)
    assert policy.tvec is column


def test_exactly_36_executable_candidates_have_at_most_30_degree_total_travel():
    observed=np.eye(3)
    candidates,paths,costs=TargetFramedCellScanPolicy._short_candidates(observed,AXES)
    assert len(candidates)==len(paths)==len(costs)==36
    assert sum(len(path)==1 for path in paths)==12
    assert sum(len(path)==2 for path in paths)==24
    for final,path,cost in zip(candidates,paths,costs):
        previous=observed; actual=0.
        for point in path:
            actual+=_pose_angle(point,previous); previous=point
        assert np.allclose(final,path[-1])
        assert actual<=math.radians(30.)+1e-12
        assert actual==pytest.approx(cost,abs=1e-12)


def test_world_prior_contains_saved_strong_box_padding_and_rejects_weak_hud(tmp_path):
    values=json.loads((Path(__file__).parent/'fixtures/deep_dive_framed_geometry25.json').read_text(encoding='utf8'))
    policy=TargetFramedCellScanPolicy(); report=[]
    for source in values['sources']:
        R=np.array(source['pose']['rotation']); T=np.array(source['pose']['tvec'])
        lo,hi,valid=policy._billboard_geometry(R[None],T)
        report.append(dict(round=source['round'],frame_id=source['frame_id'],
                           lo=lo[0,31].tolist(),hi=hi[0,31].tolist(),safe=bool(valid[0,31])))
        if source['round']==3:
            assert not valid[0,31],source
        else:
            for mask in source['boss_masks']:
                box=np.asarray(mask['box'],float)
                assert (lo[0,31] <= box[:2]-8.).all(),(source['frame_id'],lo[0,31],box)
                assert (hi[0,31] >= box[:2]+box[2:]+8.).all(),(source['frame_id'],hi[0,31],box)
    # Containment is not safe framing: Reset/HUD may reject these strong views.
    summary=policy.summary()['target_framed']
    assert summary['height_world']==[0.,1.] and summary['radius_world']==1.
    (tmp_path/'geometry_proxy.json').write_text(json.dumps(dict(
        navigation_only=True,height_world=[0.,1.],radius_world=1.,sources=report)),encoding='utf8')


def test_actual_source_bitmap_has_exact_pose_but_never_writes_votes(monkeypatch):
    policy,rows,value=prepared(); before=deepcopy(rows); calls=[]
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    def read(poses,rows,translation=None,masks=()):
        calls.append((poses.copy(),np.array(translation),deepcopy(masks)))
        return np.ones((len(poses),54))
    monkeypatch.setattr(policy,'_framed_readable',read)
    policy._remember_target_source(value);policy._record_opportunities(rows,value)
    policy._record_opportunities(rows,value)
    assert len(calls)==1
    assert np.allclose(calls[0][0][0],value['semantic_metadata']['pose']['rotation'])
    assert np.allclose(calls[0][1],value['semantic_metadata']['pose']['tvec'])
    assert policy._framed_seen.sum()==18 and rows==before
    assert policy._opportunities.sum()==18  # Separate old diagnostic, not copied.


@pytest.mark.parametrize('defect',['model','coverage','stale','map','basis','thin','pose'])
def test_invalid_actual_source_never_marks_framed_navigation(defect,monkeypatch):
    policy,rows,value=prepared()
    if defect=='model':value['target_coverage']['model_executed']=False
    elif defect=='coverage':value['target_coverage']['coverage_valid']=False
    elif defect=='stale':value['glyph_anchor_age_sec']=.8
    elif defect=='map':value['semantic_metadata']['map_revision']=1
    elif defect=='basis':value['geometry_body_basis']=None;policy._sync_framed_context(value)
    elif defect=='thin':
        value['accepted_anchor_observation']['accepted_faces']={'U':7}
        value['refine_diagnostic']['accepted_faces']={'U':7}
    elif defect=='pose':value['semantic_metadata']['pose']['rotation'][0][0]=2.
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    policy._remember_target_source(value);policy._record_opportunities(rows,value)
    assert not policy._framed_seen.any()


def test_actual_mask_expansion_blocks_billboard_opportunity(monkeypatch):
    policy,rows,value=prepared(); R=np.array(value['semantic_metadata']['pose']['rotation'])
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    lo,hi,valid=policy._billboard_geometry(R[None],value['semantic_metadata']['pose']['tvec'])
    indices=np.flatnonzero(valid[0]);assert len(indices)
    i=int(indices[0]); point=(lo[0,i]+hi[0,i])/2
    mask={'box':[float(point[0]),float(point[1]),1.,1.]}
    visible=policy._framed_readable(R[None],rows,value['semantic_metadata']['pose']['tvec'],[mask])
    assert not visible[0,i]


@pytest.mark.parametrize('region',['quad','corridor'])
@pytest.mark.parametrize('owner_proof',[False,True])
def test_rolled_corner_and_corridor_mask_block_even_an_accepted_owner(monkeypatch,region,owner_proof):
    from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
    policy,rows,value=prepared()
    R=_matrix(np.array([0.,0.,math.pi/4.]))[None]
    T=np.array([0.,0.,43.])
    def project(points):
        camera=np.einsum('bij,...j->b...i',R,points)+T
        return camera[...,:2]/camera[...,2,None]*policy._K[0,0]+policy._K[:2,2]
    quad=project(policy._quads);corridor=project(policy._corridors)
    i=0
    if region=='quad':
        point=quad[0,i,0]
    else:
        point=corridor[0,i].max(axis=0)+np.array([12.,12.])
        assert (point>quad[0,i].max(axis=0)).any()
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    # Isolate the other two authoritative regions from the billboard itself.
    lo=np.full((1,54,2),100.);hi=lo+1.
    monkeypatch.setattr(policy,'_billboard_geometry',lambda *args:(lo,hi,np.ones((1,54),bool)))
    mask={'box':[float(point[0]),float(point[1]),1.,1.], 'confidence':.1}
    if owner_proof:
        mask.update(cell_index=i,association_evidence={'source_frame_id':100,'accepted':True})
    original_tvec=policy.tvec.copy()
    assert policy._framed_readable(R,rows,T)[0,i]>0
    assert not policy._framed_readable(R,rows,T,[mask])[0,i]
    assert np.array_equal(policy.tvec,original_tvec)


def test_missing_basis_clears_navigation_without_replenishing_face_budget():
    policy,rows,value=prepared();policy._framed_seen[0]=True;policy._framed_attempts['U']=2
    broken=deepcopy(value);broken['geometry_body_basis']=None
    policy._sync_framed_context(broken);policy._sync_framed_context(value)
    assert not policy._framed_seen.any() and policy._framed_attempts['U']==2
    broken['map_revision']=1;policy._sync_framed_context(broken)
    assert not policy._framed_seen.any() and all(v==0 for v in policy._framed_attempts.values())


def plan_subject(monkeypatch):
    policy,rows,value=prepared();rows[31].update(occupant='unknown',occupant_status='unknown',evidence=[])
    policy._mixed_feedback=value
    monkeypatch.setattr(policy,'_joint_endpoint_valid',lambda poses,rows:np.ones(len(poses),dtype=bool))
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda poses,rows:np.ones(len(poses)))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,rows,**kw:np.ones(len(paths)))
    return policy,rows,value


def test_positive_gain_short_route_consumes_one_face_attempt_and_keeps_rows(monkeypatch):
    policy,rows,value=plan_subject(monkeypatch);before=deepcopy(rows)
    readable=np.zeros((36,54));readable[0,31]=1.
    monkeypatch.setattr(policy,'_framed_readable',lambda *args:readable.copy())
    policy._plan(np.eye(3),rows,AXES,10.)
    assert policy._framed_attempts['D']==1 and sum(policy._framed_attempts.values())==1
    assert policy._framed_selected['travel_deg']==pytest.approx(12.)
    assert policy._framed_selected['indices']==[31] and rows==before


@pytest.mark.parametrize('reason',['zero_gain','budget','support','novelty'])
def test_framing_retreats_to_parent_without_spending_attempts(monkeypatch,reason):
    policy,rows,value=plan_subject(monkeypatch);readable=np.zeros((36,54));readable[:,31]=1.
    if reason=='zero_gain':readable[:]=0.
    elif reason=='budget':policy._framed_attempts['D']=2
    elif reason=='support':monkeypatch.setattr(policy,'_planning_anchor_support',lambda poses,rows:np.zeros(len(poses)))
    else:
        candidates,_,_=policy._short_candidates(np.eye(3),AXES)
        policy._reached=list(candidates)
    monkeypatch.setattr(policy,'_framed_readable',lambda *args:readable.copy())
    calls=[];monkeypatch.setattr(TargetFirstCellScanPolicy,'_plan',lambda *args:calls.append(True))
    before=dict(policy._framed_attempts);policy._plan(np.eye(3),rows,AXES,10.)
    assert calls==[True] and policy._framed_attempts==before


def test_actual_positive_local_goes_to_parent_before_framing(monkeypatch):
    policy,rows,value=prepared();policy._local_task={'kind':'singularity','index':31}
    calls=[];monkeypatch.setattr(TargetFirstCellScanPolicy,'_plan',lambda *args:calls.append(True))
    policy._plan(np.eye(3),rows,AXES,10.)
    assert calls==[True] and all(v==0 for v in policy._framed_attempts.values())


def test_confirmed_none_does_not_substitute_for_actual_safe_height_band(monkeypatch):
    policy,rows,value=plan_subject(monkeypatch)
    rows[31].update(occupant='none',occupant_status='confirmed')
    readable=np.zeros((36,54));readable[0,31]=1.
    monkeypatch.setattr(policy,'_framed_readable',lambda *args:readable.copy())
    policy._plan(np.eye(3),rows,AXES,10.)
    assert policy._framed_selected['indices']==[31]
    assert policy._framed_attempts['D']==1


def test_only_confirmed_positive_with_actual_eight_degree_pair_is_excluded():
    policy,rows,value=prepared(); row=rows[31]
    row.update(occupant='singularity',occupant_status='confirmed',evidence=[])
    from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
    for frame,angle in ((1,0.),(2,8.1)):
        row['evidence'].append(dict(occupant='singularity',frame_id=frame,
            association_evidence=dict(source_frame_id=frame,source_map_revision=0,
                rotation=_matrix(np.array([0.,math.radians(angle),0.])).tolist())))
    assert policy._proven_positive(row)
    row['evidence'][1]['association_evidence']['rotation']=_matrix(np.array([0.,math.radians(7.9),0.])).tolist()
    assert not policy._proven_positive(row)


def test_public_decision_records_short_route_ownership_without_copying_packets(monkeypatch):
    policy,rows,value=plan_subject(monkeypatch)
    def readable(poses,rows,translation=None,masks=()):
        result=np.zeros((len(poses),54));result[:,31]=1.
        return result
    monkeypatch.setattr(policy,'_framed_readable',readable)
    observed=np.array(value['semantic_metadata']['pose']['rotation'])
    result=policy.choose(observed,rows,AXES,elapsed=10.,anchor_feedback=value)
    assert result['framed_owns_route']
    assert result['framed_selected']['travel_deg']<=30.+1e-9
    assert 'source_feedback' not in result['framed_selected']
    result['framed_selected']['indices'].append(99)
    assert 99 not in policy._framed_selected['indices']


def joint_subject(monkeypatch):
    policy,rows,value=prepared()
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    policy._remember_target_source(value);policy._record_opportunities(rows,value)
    policy._joint_observed=np.eye(3)
    assert policy._joint_source['indices']==[0,2,3,6,45,47,50]
    return policy,rows,value


@pytest.mark.parametrize('defect',['missing','candidate_only','duplicate','mismatch','foreign','single_face'])
def test_joint_certificate_needs_explicit_same_source_accepted_indices(defect,monkeypatch):
    policy,rows,value=prepared()
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    if defect in ('missing','candidate_only'):
        value['accepted_anchor_observation'].pop('accepted_cell_indices')
        if defect=='candidate_only':value['refine_diagnostic']['candidate_cell_indices']=[0,2,3,6,45,47,50]
    elif defect=='duplicate':value['accepted_anchor_observation']['accepted_cell_indices']=[0]*7
    elif defect=='mismatch':value['refine_diagnostic']['accepted_cell_indices']=[0]*7
    elif defect=='foreign':
        value['accepted_anchor_observation']['accepted_cell_indices'].append(53)
        value['refine_diagnostic']['accepted_cell_indices'].append(53)
    else:
        for key in ('accepted_anchor_observation','refine_diagnostic'):
            value[key]['accepted_faces']={'U':7};value[key]['accepted_cell_indices']=list(range(7))
    policy._remember_target_source(value);policy._record_opportunities(rows,value)
    assert policy._joint_source is None


def test_history_certificate_does_not_extend_current_freshness_or_cross_context(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch);source=deepcopy(policy._joint_source)
    later=deepcopy(value);later['glyph_anchor_age_sec']=1.2
    policy._remember_target_source(later);policy._record_opportunities(rows,later)
    assert policy._joint_source==source and not policy._opportunity_current_valid
    assert policy._joint_source['key'][-1]==10.  # No renewed source clock.
    later['map_revision']=1;later['geometry_body_basis']=None
    policy._sync_framed_context(later)
    assert policy._joint_source is None


def test_incoming_face_requires_new_actual_joint_source_without_label_changes(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch)
    before=deepcopy(rows);next_source=deepcopy(value)
    for key in ('accepted_anchor_observation','semantic_metadata'):
        next_source[key].update(frame_id=10,frame_time=10.1)
    next_source['semantic_metadata']['generation']=13
    next_source['target_coverage'].update(source_frame_id=10,source_frame_time=10.1)
    next_source['glyph_anchor_at']=10.1;next_source['glyph_anchor_frame_id']=10
    next_source['refine_diagnostic'].update(source_frame_id=10,source_frame_time=10.1)
    for key in ('accepted_anchor_observation','refine_diagnostic'):
        next_source[key].update(accepted_faces={'F':3,'D':3},accepted_cell_indices=[18,19,20,27,28,29])
    policy._remember_target_source(next_source);policy._record_opportunities(rows,next_source)
    assert policy._joint_source['indices']==[18,19,20,27,28,29]
    assert policy._joint_source['key'][2]==10 and rows==before


def test_latest_atomic_mask_filters_history_cells_without_owner_waiver(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch);old=deepcopy(policy._joint_source)
    R=np.array(value['semantic_metadata']['pose']['rotation'])
    T=np.array(value['semantic_metadata']['pose']['tvec']).reshape(3)
    camera=policy._quads[0]@R.T+T
    quad=camera[:,:2]/camera[:,2,None]*policy._K[0,0]+policy._K[:2,2]
    point=quad.mean(axis=0)
    masked=deepcopy(value)
    masked['target_candidate_associations']=[dict(box=[float(point[0]),float(point[1]),1.,1.],
        cell_index=0,association_evidence={'source_frame_id':9,'owner':True})]
    policy._remember_target_source(masked);policy._record_opportunities(rows,masked)
    assert not policy._joint_latest_mask_clear[0]
    # Even a retained certificate cannot make that blocked cell support input.
    policy._joint_source=old
    def exact_six(poses,rows,**kwargs):
        visible=np.zeros((len(poses),54));visible[:,[0,2,3,45,47,50]]=1.;return visible
    monkeypatch.setattr(policy,'_readability',exact_six)
    assert not policy._joint_endpoint_valid(R[None],rows)[0]


def test_potential_other_face_cannot_replace_real_source_neighbour(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch)
    def reader(poses,rows,**kwargs):
        # All incoming D glyphs are plausible, but real B support is gone.
        result=np.ones((len(poses),54));result[:,45:54]=0.;return result
    monkeypatch.setattr(policy,'_readability',reader)
    assert not policy._joint_endpoint_valid(np.eye(3)[None],rows)[0]
    assert policy._planning_anchor_support(np.eye(3)[None],rows)[0]==0


def test_every_short_waypoint_checked_and_shared_exact_points_projected_once(monkeypatch):
    from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
    policy,rows,value=joint_subject(monkeypatch);calls=[]
    end=_matrix(np.array([0.,math.radians(60.),0.]))
    def check(poses,rows):
        calls.append(len(poses))
        # Final endpoint is safe, the 30-degree checkpoint is not.
        angles=np.array([math.degrees(_pose_angle(pose,np.eye(3))) for pose in poses])
        return angles>50.
    monkeypatch.setattr(policy,'_joint_endpoint_valid',check)
    paths=[[end],[end]]
    assert not policy._joint_paths_valid(paths,rows).any()
    assert calls==[2]  # Two unique exact checkpoints, not four projections.
    policy._joint_paths_valid(paths,rows)
    assert calls==[2]


def test_original_global_zero_score_fallback_is_finitely_blocked_via_real_choose(monkeypatch):
    policy,rows,value=prepared()
    rows[31].update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    before=deepcopy(rows)
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    monkeypatch.setattr(policy,'_framed_readable',lambda poses,*args:np.zeros((len(poses),54)))
    monkeypatch.setattr(policy,'_joint_endpoint_valid',lambda poses,rows:np.zeros(len(poses),dtype=bool))
    observed=np.asarray(value['semantic_metadata']['pose']['rotation'])
    result=policy.choose(observed,rows,AXES,elapsed=10.,anchor_feedback=value)
    assert result['phase']=='recover' and result['direction'] is None
    assert result['reason']=='joint_navigation_no_supported_route'
    assert not policy._route and rows==before


def test_published_parent_route_cannot_bypass_postcheck(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch)
    def parent_choose(*args,**kwargs):
        policy._route=[np.eye(3)]
        return dict(direction=(1.,0.),phase='coverage')
    monkeypatch.setattr(TargetFirstCellScanPolicy,'choose',parent_choose)
    monkeypatch.setattr(policy,'_joint_endpoint_valid',lambda poses,rows:np.zeros(len(poses),dtype=bool))
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=value)
    assert result['reason']=='joint_navigation_published_route_unsupported'
    assert result['direction'] is None and not policy._route


def test_route_guard_does_not_suppress_unrelated_programming_errors(monkeypatch):
    policy,rows,value=prepared()
    def broken(*args,**kwargs):raise ValueError('unrelated')
    monkeypatch.setattr(TargetFirstCellScanPolicy,'choose',broken)
    with pytest.raises(ValueError,match='unrelated'):
        policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=value)


def owned_camera_route(monkeypatch):
    from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
    policy,rows,value=joint_subject(monkeypatch)
    rows[31].update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    observed=np.array(value['semantic_metadata']['pose']['rotation'])
    goal=_matrix(np.array([0.,math.radians(12.),0.]))@observed
    policy._route=[goal];policy._final_rotation=goal;policy._target_indices=[5,7,8]
    policy.target_face='U'
    policy._started=policy._last_plan_time=10.;policy._plan_serial=1
    policy._framed_seen[:]=False;policy._framed_owns_route=True;policy._framed_attempts['U']=1
    monkeypatch.setattr(policy,'_joint_endpoint_valid',lambda poses,rows:np.ones(len(poses),dtype=bool))
    calls=[]
    def plan(actual,atlas,axes,now):
        calls.append(now);policy._route=[goal];policy._final_rotation=goal
        policy._framed_owns_route=False;policy._target_indices=[]
    monkeypatch.setattr(policy,'_plan',plan)
    return policy,rows,value,observed,calls


def test_real_choose_keeps_unarrived_framed_route_despite_confirmed_none(monkeypatch):
    policy,rows,value,observed,calls=owned_camera_route(monkeypatch)
    before=deepcopy(rows);attempts=dict(policy._framed_attempts)
    result=policy.choose(observed,rows,AXES,elapsed=10.1,anchor_feedback=value)
    assert result['direction'] is not None and result['framed_owns_route']
    assert calls==[] and policy._plan_serial==1
    assert policy._framed_attempts==attempts and rows==before


@pytest.mark.parametrize('proof',['framed_source','positive_pair'])
def test_real_choose_ends_owned_route_after_real_navigation_proof(proof,monkeypatch):
    from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
    policy,rows,value,observed,calls=owned_camera_route(monkeypatch)
    if proof=='framed_source':
        policy._framed_source=None
        monkeypatch.setattr(policy,'_framed_readable',lambda poses,*args:np.ones((len(poses),54)))
    else:
        for i in policy._target_indices:
            rows[i].update(occupant='player',occupant_status='confirmed',evidence=[
                dict(occupant='player',frame_id=f,association_evidence=dict(source_frame_id=f,
                    source_map_revision=0,rotation=_matrix(np.array([0.,math.radians(a),0.])).tolist()))
                for f,a in ((1,0.),(2,8.1))])
    result=policy.choose(observed,rows,AXES,elapsed=10.1,anchor_feedback=value)
    assert len(calls)==1 and result['direction'] is not None


def test_real_choose_parent_route_still_ends_on_original_complete_nodes(monkeypatch):
    policy,rows,value,observed,calls=owned_camera_route(monkeypatch)
    policy._framed_owns_route=False
    policy.choose(observed,rows,AXES,elapsed=10.1,anchor_feedback=value)
    assert len(calls)==1


@pytest.mark.parametrize('phase',['local','recovery'])
def test_local_and_recovery_do_not_use_framed_bitmap_completion(phase):
    policy,rows,value=prepared();policy._target_indices=[5,7,8]
    policy._framed_seen[:]=False;policy._framed_owns_route=True
    if phase=='local':policy._local_task={'kind':'inspiration','index':26}
    else:policy._anchor_recovery={'started':10.}
    assert policy._route_targets_complete(np.ones(54,dtype=bool),rows)


def bridge_subject(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch)
    value.update(frame_id=10,generation=13,frame_time=10.1,geometry_tracking_ok=True)
    policy._mixed_feedback=value
    rows[31].update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    policy._bridge_attempts.update(R=2,F=2,L=2)
    return policy,rows,value


def test_bridge_preserves_real_support_and_never_makes_votes(monkeypatch):
    policy,rows,value=bridge_subject(monkeypatch);before=deepcopy(rows)
    framed=dict(policy._framed_attempts)
    assert policy._plan_bridge(np.eye(3),rows,AXES,10.1)
    assert policy._bridge_selected['face']=='D'
    assert policy._bridge_selected['normal_gain']>.08
    assert policy._bridge_selected['travel_deg']<=30.000001
    assert policy._target_indices==[] and policy._bridge_attempts['D']==1
    assert policy._framed_attempts==framed and rows==before
    assert policy._joint_paths_valid([policy._route],rows)[0]


@pytest.mark.parametrize('invalid',['route','budget','clock','no_source'])
def test_bridge_cannot_use_unsafe_potential_or_refresh_a_budget(invalid,monkeypatch):
    policy,rows,value=bridge_subject(monkeypatch)
    if invalid=='route':
        monkeypatch.setattr(policy,'_joint_paths_valid',lambda paths,rows:np.zeros(len(paths),dtype=bool))
    elif invalid=='budget':policy._bridge_attempts=dict.fromkeys('URFDLB',2)
    elif invalid=='clock':value['frame_time']=float('nan')
    else:policy._joint_source=None
    attempts=dict(policy._bridge_attempts)
    assert not policy._plan_bridge(np.eye(3),rows,AXES,10.1)
    assert policy._bridge_attempts==attempts and policy._bridge_pending is None


def test_bridge_same_source_and_near_pose_cannot_restart_sealed_face_budget(monkeypatch):
    from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
    policy,rows,value=bridge_subject(monkeypatch)
    assert policy._plan_bridge(np.eye(3),rows,AXES,10.1)
    policy._bridge_pending=None
    assert not policy._plan_bridge(np.eye(3),rows,AXES,10.2)
    source=policy._joint_source
    source['key']=tuple(source['key'][:2])+(11,14,10.2)
    source['base_rotation']=_matrix(np.array([0.,math.radians(7.9),0.])).tolist()
    assert not policy._plan_bridge(np.eye(3),rows,AXES,10.3)
    source['base_rotation']=_matrix(np.array([0.,math.radians(8.1),0.])).tolist()
    assert policy._plan_bridge(np.eye(3),rows,AXES,10.3)
    policy._bridge_pending=None
    source['key']=tuple(source['key'][:2])+(12,15,10.3)
    source['base_rotation']=_matrix(np.array([0.,math.radians(20.),0.])).tolist()
    assert not policy._plan_bridge(np.eye(3),rows,AXES,10.4)
    assert policy._bridge_attempts['D']==2


@pytest.mark.parametrize('invalid',['none','old','remote','pose','age','before_arrival'])
def test_bridge_handoff_needs_new_actual_joint_and_tracked_arrival(invalid,monkeypatch):
    policy,rows,value=bridge_subject(monkeypatch)
    assert policy._plan_bridge(np.eye(3),rows,AXES,10.1)
    pending=policy._bridge_pending;pending['arrival_stamp']=10.5
    source=policy._joint_source
    source['key']=(7,0,20,30,10.4)  # Before geometry arrival, genuinely after dispatch.
    source['base_rotation']=deepcopy(pending['goal_base'])
    value['semantic_metadata']['frame_id']=20
    policy._opportunity_current_valid=True
    if invalid=='old':source['key']=(7,0,10,13,10.1)
    elif invalid=='remote':source['key']=(8,0,20,30,10.4)
    elif invalid=='pose':source['base_rotation']=np.eye(3).tolist()
    elif invalid=='age':policy._opportunity_current_valid=False
    elif invalid=='before_arrival':pending['arrival_stamp']=None
    policy._bridge_accept_source(value)
    assert pending['ready'] is (invalid=='none')
    assert policy._bridge_attempts['D']==1


def bridge_choose_subject(monkeypatch,two_legs=False):
    from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
    policy,rows,value=bridge_subject(monkeypatch)
    middle=_matrix(np.array([0.,math.radians(12.),0.]))
    goal=_matrix(np.array([math.radians(12.),0.,0.]))@middle
    policy._route=[middle,goal] if two_legs else [goal]
    policy._final_rotation=goal;policy._target_indices=[];policy.target_face='D'
    policy._started=policy._last_plan_time=10.;policy._plan_serial=1
    policy._bridge_owns_route=True;policy._bridge_attempts['D']=1
    policy._bridge_pending=dict(floor=(7,0,10,13,10.1),source_base=np.eye(3).tolist(),
        goal_base=goal.tolist(),basis=np.eye(3),arrival_stamp=None,ready=False)
    monkeypatch.setattr(policy,'_joint_endpoint_valid',lambda poses,rows:np.ones(len(poses),dtype=bool))
    return policy,rows,value,middle,goal


def test_real_choose_bridge_first_leg_advances_without_final_source_or_arrival_stamp(monkeypatch):
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch,True)
    result=policy.choose(middle,rows,AXES,elapsed=10.2,anchor_feedback=value)
    assert result['direction'] is not None and result['bridge_owns_route']
    assert len(policy._route)==1 and np.array_equal(policy._route[0],goal)
    assert policy._bridge_pending['arrival_stamp'] is None
    assert policy._bridge_attempts['D']==1


def test_real_choose_bridge_ordinary_none_does_not_clear_route_or_budget(monkeypatch):
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch)
    before=deepcopy(rows)
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.2,anchor_feedback=value)
    assert result['direction'] is not None and result['bridge_owns_route']
    assert np.array_equal(policy._route[0],goal)
    assert policy._bridge_attempts['D']==1 and rows==before


def test_real_choose_bridge_missing_new_source_stops_at_original_semantic_deadline(monkeypatch):
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch)
    first=policy.choose(goal,rows,AXES,elapsed=10.2,semantic_revision=1,anchor_feedback=value)
    assert first['reason']=='await_semantic_frame'
    assert policy._bridge_pending['arrival_stamp']==10.1
    waiting=policy.choose(goal,rows,AXES,elapsed=10.5,semantic_revision=2,anchor_feedback=value)
    assert waiting['reason']=='await_semantic_frame'
    # A timed-out bridge remains a finite stop if no safe current joint exists.
    monkeypatch.setattr(policy,'_joint_endpoint_valid',lambda poses,rows:np.zeros(len(poses),dtype=bool))
    stopped=policy.choose(goal,rows,AXES,elapsed=10.81,semantic_revision=3,anchor_feedback=value)
    assert stopped['reason']=='joint_bridge_no_new_source'
    assert stopped['direction'] is None and policy._bridge_attempts['D']==1
    assert any(v['reason']=='semantic_dwell_timeout' for v in policy.insufficient)


def next_joint_packet(value,indices):
    result=deepcopy(value);frame=result['semantic_metadata']['frame_id']+1
    stamp=result['semantic_metadata']['frame_time']+.1
    result['semantic_metadata'].update(frame_id=frame,frame_time=stamp,
        generation=result['semantic_metadata']['generation']+1)
    result['accepted_anchor_observation'].update(frame_id=frame,frame_time=stamp)
    result['target_coverage'].update(source_frame_id=frame,source_frame_time=stamp)
    result.update(glyph_anchor_frame_id=frame,glyph_anchor_at=stamp)
    result['refine_diagnostic'].update(source_frame_id=frame,source_frame_time=stamp)
    faces={face:sum(i//9==j for i in indices) for j,face in enumerate('URFDLB')
           if any(i//9==j for i in indices)}
    for key in ('accepted_anchor_observation','refine_diagnostic'):
        result[key].update(accepted_faces=faces,accepted_cell_indices=list(indices))
    return result


def test_bridge_never_returns_to_historically_supported_U_when_current_source_is_RF(monkeypatch):
    policy,rows,value=bridge_subject(monkeypatch);before=deepcopy(rows)
    assert policy._joint_ever_supported['U']
    newer=next_joint_packet(value,[9,10,11,18,19,20])
    policy._remember_target_source(newer);policy._record_opportunities(rows,newer)
    assert policy._joint_source['indices']==[9,10,11,18,19,20]
    assert policy._joint_ever_supported['U'] and policy._joint_ever_supported['R']
    policy._bridge_attempts.update(D=2,L=2)
    # U is currently absent but already learned; no unseen face has budget left.
    assert not policy._plan_bridge(np.eye(3),rows,AXES,10.3)
    assert rows==before and policy._bridge_attempts['U']==0


def test_historical_face_bit_uses_filtered_actual_indices_not_accepted_face_count(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch)
    newer=next_joint_packet(value,[0,2,3,6,9,10,45,47,50])
    def filtered(poses,rows,**kwargs):
        result=np.ones((len(poses),54));result[:,10]=0.;return result
    monkeypatch.setattr(policy,'_readability',filtered)
    policy._remember_target_source(newer);policy._record_opportunities(rows,newer)
    assert newer['accepted_anchor_observation']['accepted_faces']['R']==2
    assert 9 in policy._joint_source['indices'] and 10 not in policy._joint_source['indices']
    assert not policy._joint_ever_supported['R']
    upgraded=next_joint_packet(newer,[0,2,3,6,9,10,45,47,50])
    monkeypatch.setattr(policy,'_readability',lambda poses,rows,**kwargs:np.ones((len(poses),54)))
    policy._remember_target_source(upgraded);policy._record_opportunities(rows,upgraded)
    assert policy._joint_ever_supported['R'] and 10 in policy._joint_source['indices']


def test_history_survives_temporary_basis_failure_but_isolates_real_context_changes(monkeypatch):
    policy,rows,value=joint_subject(monkeypatch)
    policy._bridge_attempts['D']=1;history=dict(policy._joint_ever_supported)
    invalid=deepcopy(value);invalid['geometry_body_basis']=None
    policy._sync_framed_context(invalid)
    assert policy._joint_source is None and policy._joint_ever_supported==history
    assert policy._bridge_attempts['D']==1
    policy._mixed_feedback=invalid
    assert not policy._plan_bridge(np.eye(3),rows,AXES,10.3)
    changed=deepcopy(value);changed['map_revision']=1
    policy._sync_framed_context(changed)
    assert not any(policy._joint_ever_supported.values())
    assert not any(policy._bridge_attempts.values())


def actual30_geometry_route(monkeypatch):
    """Saved30 source31/goal2/stop51 projections; admission is tested separately."""
    policy,rows,value=prepared()
    old_goal=np.array([[.810645022774817,.06425762958895523,.5820013780825822],
        [.4818768909420209,.4914498261186946,-.7254458838424523],
        [-.3326399089503063,.8685321096237684,.36742708872103935]])
    actual=np.array([[.8134511136203644,.05872348615532676,.5786612462603262],
        [.4439908510747979,.5799891078772679,-.6829968952386296],
        [-.3757251787033514,.8125048842422401,.44572009509426247]])
    old_t=np.array([-.0079284087309118,.4280104015800679,41.35757816531768])
    actual_t=np.array([.03508703246403982,.4915786073274836,41.30757999330059])
    indices=[2,6,7,8,9,10,13,18,19,21]
    policy._joint_source=dict(indices=indices,key=(7,0,9,12,10.),
        body_basis=np.eye(3).tolist(),base_rotation=np.eye(3).tolist())
    policy._joint_latest_mask_clear[:]=True
    policy.tvec=old_t
    assert policy._joint_endpoint_valid(old_goal[None],rows)[0]
    policy.tvec=actual_t
    assert not policy._joint_endpoint_valid(old_goal[None],rows)[0]
    assert policy._joint_endpoint_valid(actual[None],rows)[0]
    rows[31].update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    policy._route=[old_goal];policy._final_rotation=old_goal;policy._target_indices=[5,6,7]
    policy.target_face='U';policy._framed_owns_route=True;policy._framed_attempts['U']=2
    policy._started=policy._last_plan_time=10.;policy._plan_serial=2
    monkeypatch.setattr(policy,'_record_opportunities',lambda *args:None)
    return policy,rows,value,actual,old_goal


@pytest.mark.parametrize('replacement',['safe','unsafe','none'])
def test_actual30_changed_translation_replans_once_via_real_choose(replacement,monkeypatch):
    from plans.resonance_pc.src.actions._deep_dive_target_framed_scan_policy import _JointRouteBlocked
    policy,rows,value,actual,old_goal=actual30_geometry_route(monkeypatch)
    before=deepcopy(rows);attempts=dict(policy._framed_attempts);calls=[]
    candidates,paths,_=policy._short_candidates(actual,AXES)
    policy._joint_observed=actual
    safe=policy._joint_paths_valid(paths,rows)
    safe_goal=candidates[np.flatnonzero(safe)[0]].copy()
    assert _pose_angle(actual,safe_goal)>math.radians(4.)
    def plan(observed,atlas,axes,now):
        calls.append((now,observed.copy()))
        if replacement=='none':raise _JointRouteBlocked('joint_navigation_no_supported_route')
        policy._route=[safe_goal if replacement=='safe' else old_goal.copy()]
        policy._final_rotation=policy._route[-1];policy._target_indices=[]
        policy._plan_serial+=1
    monkeypatch.setattr(policy,'_plan',plan)
    result=policy.choose(actual,rows,AXES,tvec=policy.tvec,elapsed=10.2,anchor_feedback=value)
    assert len(calls)==1 and calls[0][0]==10.2
    assert np.array_equal(calls[0][1],actual)
    assert policy.stats['joint_route_replans']==1
    assert policy.summary()['target_framed']['joint_navigation']['route_replans']==1
    assert policy._framed_attempts==attempts and rows==before
    if replacement=='safe':
        assert result['direction'] is not None
        assert policy._joint_endpoint_valid(policy._route[0][None],rows)[0]
    else:
        assert result['direction'] is None and result['phase']=='recover' and not policy._route
        assert result['reason']==('joint_navigation_no_supported_route' if replacement=='none'
                                 else 'joint_navigation_published_route_unsupported')


def test_unsupported_current_pose_cannot_retry_an_unsafe_published_route(monkeypatch):
    policy,rows,value,actual,old_goal=actual30_geometry_route(monkeypatch)
    policy._joint_latest_mask_clear[:]=False
    def never(*args):raise AssertionError('cannot replan without current actual joint support')
    monkeypatch.setattr(policy,'_plan',never)
    result=policy.choose(actual,rows,AXES,tvec=policy.tvec,elapsed=10.2,anchor_feedback=value)
    assert result['reason']=='joint_navigation_published_route_unsupported'
    assert policy.stats.get('joint_route_replans',0)==0


def test_real_bridge_deadline_can_move_to_other_face_without_reusing_source_D_budget(monkeypatch):
    from plans.resonance_pc.src.actions._deep_dive_target_framed_scan_policy import _JointRouteBlocked
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch)
    source=policy._joint_source
    policy._bridge_last_source['D']=dict(key=source['key'],base=deepcopy(source['base_rotation']))
    policy._bridge_attempts['L']=0
    monkeypatch.setattr(policy,'_framed_readable',lambda poses,*args:np.zeros((len(poses),54)))
    calls=[]
    def bounded_bridge(observed,atlas,axes,now):
        calls.append(now)
        if not policy._plan_bridge(observed,atlas,axes,now):raise _JointRouteBlocked('no_safe_replacement')
    monkeypatch.setattr(policy,'_parent_or_bridge_plan',bounded_bridge)
    before=deepcopy(rows);authority=deepcopy(policy._bridge_last_source['D'])
    first=policy.choose(goal,rows,AXES,elapsed=10.2,anchor_feedback=value)
    assert first['reason']=='await_semantic_frame'
    assert calls==[]
    result=policy.choose(goal,rows,AXES,elapsed=10.81,anchor_feedback=value)
    assert calls==[10.81] and result['direction'] is not None
    assert policy._bridge_selected['face']=='L' and policy._bridge_attempts['L']==1
    assert policy._bridge_attempts['D']==1 and policy._bridge_last_source['D']==authority
    assert policy.stats['bridge_timeout_fallbacks']==1
    assert policy.summary()['target_framed']['bridge']['timeout_fallbacks']==1
    assert any(v['reason']=='semantic_dwell_timeout' for v in policy.insufficient)
    assert policy._failed_views and rows==before
    assert not policy._bridge_pending['ready']  # Expired D was never credited.


@pytest.mark.parametrize('cause',['elapsed_only','stagnant','approach','invalid_current','no_safe'])
def test_bridge_fallback_requires_actual_deadline_event_and_safe_current(cause,monkeypatch):
    from plans.resonance_pc.src.actions._deep_dive_target_framed_scan_policy import _JointRouteBlocked
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch)
    policy._bridge_pending['arrival_stamp']=10.1
    if cause in ('stagnant','approach'):
        policy._feedback(rows,goal,10.9,'observed_pose_stagnant' if cause=='stagnant' else 'pose_approach_timeout',True)
    elif cause in ('invalid_current','no_safe'):
        policy._feedback(rows,goal,10.9,'semantic_dwell_timeout',True)
    if cause=='invalid_current':policy._framed_basis_valid=False
    if cause=='no_safe':
        monkeypatch.setattr(policy,'_joint_endpoint_valid',lambda poses,rows:np.zeros(len(poses),dtype=bool))
    authority=policy._bridge_pending;attempts=dict(policy._bridge_attempts)
    with pytest.raises(_JointRouteBlocked,match='joint_bridge_no_new_source'):
        policy._plan(goal,rows,AXES,11.)
    assert policy._bridge_pending is authority and policy._bridge_attempts==attempts
    assert policy.stats.get('bridge_timeout_fallbacks',0)==0


@pytest.mark.parametrize('safe',[True,False])
def test_real_choose_strict_new_interest_cancels_bridge_without_refunding_authority(safe,monkeypatch):
    from test_deep_dive_target_first_scan_policy import assigned_packet,unknown_target
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch)
    unknown_target(rows,50)
    # A real admitted association is a navigation clue, not a positive vote.
    value['target_candidate_associations']=assigned_packet(rows)['target_candidate_associations']
    policy._bridge_selected={'face':'D'}
    policy._bridge_last_source['D']=dict(key=policy._joint_source['key'],
        base=deepcopy(policy._joint_source['base_rotation']))
    before=deepcopy(rows);authority=deepcopy(policy._bridge_last_source)
    attempts=dict(policy._bridge_attempts);history=dict(policy._joint_ever_supported)
    failed=deepcopy(policy._failed_views);calls=[]
    monkeypatch.setattr(policy,'_record_opportunities',lambda *args:None)
    def plan(observed,atlas,axes,now):
        assert policy._bridge_pending is None and not policy._bridge_owns_route
        calls.append(now)
        policy._route=[middle.copy()];policy._final_rotation=middle.copy()
        policy._target_indices=[];policy._plan_serial+=1
    monkeypatch.setattr(policy,'_plan',plan)
    monkeypatch.setattr(policy,'_joint_paths_valid',
        lambda paths,atlas:np.full(len(paths),safe,dtype=bool))
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.2,anchor_feedback=value)
    assert 50 in policy._assigned_interests and policy._local_task is None
    assert calls==([10.2] if safe else [10.2,10.2])
    assert (result['direction'] is not None) is safe
    if not safe:assert result['reason']=='joint_navigation_published_route_unsupported'
    assert policy._bridge_pending is None and not policy._bridge_owns_route
    assert policy._bridge_attempts==attempts and policy._bridge_last_source==authority
    assert policy._joint_ever_supported==history and policy._failed_views==failed
    assert rows==before and rows[50]['evidence']==[]
    summary=policy.summary()['target_framed']['bridge']
    assert summary['interest_cancellations']==1 and summary['last_cancelled_face']=='D'
    assert summary['timeout_fallbacks']==0 and not summary['ready']


def test_real_choose_without_new_interest_preserves_unfinished_bridge(monkeypatch):
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch)
    pending=policy._bridge_pending;before=deepcopy(rows)
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.2,anchor_feedback=value)
    assert result['direction'] is not None and policy._bridge_pending is pending
    assert policy._bridge_owns_route and policy._bridge_attempts['D']==1
    assert policy.summary()['target_framed']['bridge']['interest_cancellations']==0
    assert rows==before


@pytest.mark.parametrize('phase',['local','recovery','no_route','not_owned'])
def test_interest_hook_does_not_cancel_non_interruptible_bridge(phase,monkeypatch):
    policy,rows,value,middle,goal=bridge_choose_subject(monkeypatch)
    pending=policy._bridge_pending;attempts=dict(policy._bridge_attempts)
    if phase=='local':policy._local_task={'index':31,'kind':'singularity','source_frame_id':9}
    elif phase=='recovery':policy._anchor_recovery={'started':10.}
    elif phase=='no_route':policy._route=[]
    else:policy._bridge_owns_route=False
    assert policy._assigned_interest_interrupts_route() is True
    assert policy._bridge_pending is pending and policy._bridge_attempts==attempts
    assert policy.summary()['target_framed']['bridge']['interest_cancellations']==0
