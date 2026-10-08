"""Actual image opportunity affects navigation only, never recognition proof."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_target_first_scan_policy import TargetFirstCellScanPolicy
from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix
from test_deep_dive_target_first_scan_policy import packet
from test_deep_dive_mixed_scan_policy import AXES,cells


def actual_packet(rows):
    feedback=packet(rows)
    source=feedback['accepted_anchor_observation']
    feedback['semantic_metadata']=dict(frame_id=9,generation=12,session_id=7,map_revision=0,
        frame_time=10.,pose=dict(rotation=source['rotation'],tvec=[[0.],[.42],[42.]],map_revision=0))
    feedback['target_coverage']=dict(model_executed=True,coverage_valid=True,
        source_frame_id=9,source_frame_time=10.,source_map_revision=0)
    feedback['glyph_anchor_age_sec']=.2
    feedback['target_candidate_associations']=[]
    return feedback


def record(policy,rows,feedback):
    policy._remember_target_source(feedback)
    policy._record_opportunities(rows,feedback)


def selective_reader(monkeypatch,policy,indices):
    values=np.zeros((1,54));values[0,indices]=1.
    monkeypatch.setattr(policy,'_readability',lambda rotations,rows:values.copy())


def test_valid_actual_pose_records_only_supported_faces_once_and_restores_tvec(monkeypatch):
    rows=cells();before=deepcopy(rows);policy=TargetFirstCellScanPolicy(opportunity_navigation=True)
    feedback=actual_packet(rows);calls=[]
    previous=policy.tvec.copy()
    def read(rotations,atlas):
        calls.append((rotations.copy(),policy.tvec.copy()))
        return np.ones((1,54))
    monkeypatch.setattr(policy,'_readability',read)
    record(policy,rows,feedback);record(policy,rows,feedback)
    assert len(calls)==1 and policy._opportunities.sum()==18
    assert np.array_equal(calls[0][0][0],feedback['semantic_metadata']['pose']['rotation'])
    assert np.array_equal(calls[0][1],np.asarray(feedback['semantic_metadata']['pose']['tvec']).reshape(3))
    assert np.array_equal(policy.tvec,previous) and rows==before
    summary=policy.summary()['navigation_opportunities']
    assert summary['by_face']['U']==9 and summary['by_face']['B']==9 and summary['covered']==18
    assert summary['source']['generation']==12


@pytest.mark.parametrize('defect',['model','coverage','frame','stamp','session','map','bool_map',
                                 'pose_map','pose_rotation','pose_translation','stale','paused','not_renewed','thin'])
def test_invalid_source_cannot_supply_opportunity(monkeypatch,defect):
    rows=cells();policy=TargetFirstCellScanPolicy(opportunity_navigation=True);feedback=actual_packet(rows)
    if defect=='model':feedback['target_coverage']['model_executed']=False
    if defect=='coverage':feedback['target_coverage']['coverage_valid']=False
    if defect=='frame':feedback['target_coverage']['source_frame_id']=8
    if defect=='stamp':feedback['target_coverage']['source_frame_time']=9.9
    if defect=='session':feedback['semantic_metadata']['session_id']=8
    if defect=='map':feedback['semantic_metadata']['map_revision']=1
    if defect=='bool_map':
        feedback['map_revision']=False
        feedback['semantic_metadata']['map_revision']=False
        feedback['semantic_metadata']['pose']['map_revision']=False
        feedback['accepted_anchor_observation']['map_revision']=False
        feedback['target_coverage']['source_map_revision']=False
    if defect=='pose_map':feedback['semantic_metadata']['pose']['map_revision']=1
    if defect=='pose_rotation':feedback['semantic_metadata']['pose']['rotation']=_matrix(np.array([0.,.1,0.])).tolist()
    if defect=='pose_translation':feedback['semantic_metadata']['pose']['tvec']=[[0.],[0.],[float('nan')]]
    if defect=='stale':feedback['glyph_anchor_age_sec']=.8
    if defect=='paused':feedback['fusion_paused']=True
    if defect=='not_renewed':feedback['refine_diagnostic']['renewed']=False
    if defect=='thin':
        feedback['refine_diagnostic']['accepted_faces']={'U':6}
        feedback['accepted_anchor_observation']['accepted_faces']={'U':6}
    monkeypatch.setattr(policy,'_readability',lambda *args:pytest.fail('invalid source projected'))
    record(policy,rows,feedback)
    assert not policy._opportunities.any() and not policy._opportunity_current_valid


@pytest.mark.parametrize('defect',['frame','generation','time','same_frame_generation'])
def test_capture_identity_must_advance_without_reusing_a_source(monkeypatch,defect):
    rows=cells();policy=TargetFirstCellScanPolicy(opportunity_navigation=True);feedback=actual_packet(rows)
    selective_reader(monkeypatch,policy,[0]);record(policy,rows,feedback)
    old=policy.summary()['navigation_opportunities']['source']
    next_packet=actual_packet(rows)
    for parent,name in ((next_packet,'glyph_anchor_frame_id'),
                        (next_packet['accepted_anchor_observation'],'frame_id'),
                        (next_packet['refine_diagnostic'],'source_frame_id'),
                        (next_packet['semantic_metadata'],'frame_id'),
                        (next_packet['target_coverage'],'source_frame_id')):parent[name]=10
    next_packet['semantic_metadata']['generation']=13
    for parent,name in ((next_packet,'glyph_anchor_at'),(next_packet['accepted_anchor_observation'],'frame_time'),
                        (next_packet['refine_diagnostic'],'source_frame_time'),(next_packet['semantic_metadata'],'frame_time'),
                        (next_packet['target_coverage'],'source_frame_time')):parent[name]=11.
    if defect=='frame':
        for parent,name in ((next_packet,'glyph_anchor_frame_id'),(next_packet['accepted_anchor_observation'],'frame_id'),
                            (next_packet['refine_diagnostic'],'source_frame_id'),(next_packet['semantic_metadata'],'frame_id'),
                            (next_packet['target_coverage'],'source_frame_id')):parent[name]=8
    if defect=='generation':next_packet['semantic_metadata']['generation']=11
    if defect=='time':
        for parent,name in ((next_packet,'glyph_anchor_at'),(next_packet['accepted_anchor_observation'],'frame_time'),
                            (next_packet['refine_diagnostic'],'source_frame_time'),(next_packet['semantic_metadata'],'frame_time'),
                            (next_packet['target_coverage'],'source_frame_time')):parent[name]=9.
    if defect=='same_frame_generation':
        next_packet=deepcopy(feedback);next_packet['semantic_metadata']['generation']=13
    selective_reader(monkeypatch,policy,[1]);record(policy,rows,next_packet)
    assert policy._opportunities[0] and not policy._opportunities[1]
    assert policy.summary()['navigation_opportunities']['source']==old
    assert not policy._opportunity_current_valid


@pytest.mark.parametrize('change',['map','session','invalid_map_type'])
def test_context_change_clears_navigation_even_with_missing_body_basis(monkeypatch,change):
    rows=cells();policy=TargetFirstCellScanPolicy(opportunity_navigation=True);feedback=actual_packet(rows)
    selective_reader(monkeypatch,policy,[0]);record(policy,rows,feedback)
    new=deepcopy(feedback);new['geometry_body_basis']=None
    if change=='map':new['map_revision']=1
    if change=='session':new['session_id']=8
    if change=='invalid_map_type':new['map_revision']=False
    record(policy,rows,new)
    assert not policy._opportunities.any() and not policy._opportunity_pending()
    assert policy._opportunity_source is None


def test_actual_weak_box_edge_blocks_opportunity_without_modifying_masks(monkeypatch):
    rows=cells();policy=TargetFirstCellScanPolicy(opportunity_navigation=True);feedback=actual_packet(rows)
    index=4;R=np.asarray(feedback['semantic_metadata']['pose']['rotation']);t=np.asarray(feedback['semantic_metadata']['pose']['tvec']).reshape(3)
    camera=(R@policy._quads[index].T).T+t;q=camera[:,:2]/camera[:,2,None]*policy._K[0,0]+policy._K[:2,2]
    lo,hi=q.min(0),q.max(0)
    # A tiny weak box sits just outside the quad; the original8px margin
    # intersects it, although unexpanded16%-area masking would miss it.
    feedback['target_candidate_associations']=[dict(kind='inspiration',confidence=.08,
        box=[float(hi[0]+2),float((lo[1]+hi[1])/2),3.,3.])]
    before=deepcopy(feedback);selective_reader(monkeypatch,policy,[index])
    record(policy,rows,feedback)
    assert not policy._opportunities[index] and feedback==before


def test_unexposed_partial_face_is_not_filtered_out_by_unseen_face_priority(monkeypatch):
    rows=cells();policy=TargetFirstCellScanPolicy(opportunity_navigation=True);feedback=actual_packet(rows)
    rows[26].update(occupant='unknown',occupant_status='unknown',node_status='unknown',confidence=0.,evidence=[])
    before=deepcopy(rows)
    selective_reader(monkeypatch,policy,[0]);record(policy,rows,feedback)
    policy._opportunities[:]=True;policy._opportunities[18:27]=True;policy._opportunities[26]=False
    policy._opportunities[27:36]=False
    policy._mixed_feedback=feedback
    pending=np.zeros(54,bool);pending[27:36]=True;weights=pending.astype(float)*3
    pending,weights=policy._planning_weights(rows,10.,pending,weights)
    assert pending[26] and weights[26]>=1.
    candidates=np.array([np.eye(3),_matrix(np.array([0.,.3,0.]))])
    readable=np.zeros((2,54));readable[0,27:30]=.1;readable[1,26]=1.
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda rotations,atlas:np.ones(len(rotations)))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,atlas,**kwargs:np.ones(len(paths)))
    chosen=policy._planning_endpoint(0,np.array([100.,0.]),candidates,[[candidates[0]],[candidates[1]]],
        [0.,.3],readable,readable,np.ones(2,bool),np.eye(3),rows,10.)
    assert chosen==1 and rows==before
    failed=readable.copy();failed[1,26]=0.
    assert policy._planning_endpoint(0,np.zeros(2),candidates,[[candidates[0]],[candidates[1]]],
        [0.,.3],readable,failed,np.ones(2,bool),np.eye(3),rows,10.)==0


def test_single_masked_hole_does_not_receive_same_face_reward_as_nine_unexposed_cells(monkeypatch):
    rows=cells();before=deepcopy(rows);policy=TargetFirstCellScanPolicy(opportunity_navigation=True)
    feedback=actual_packet(rows);selective_reader(monkeypatch,policy,[0]);record(policy,rows,feedback)
    policy._opportunities[:]=True;policy._opportunities[1]=False;policy._opportunities[9:18]=False
    bitmap=policy._opportunities.copy();policy._mixed_feedback=feedback
    candidates=np.array([np.eye(3),_matrix(np.array([0.,.3,0.]))])
    readable=np.zeros((2,54));readable[0,1]=1.;readable[1,9:18]=1.
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda rotations,atlas:np.ones(len(rotations)))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,atlas,**kwargs:np.ones(len(paths)))
    chosen=policy._planning_endpoint(0,np.array([100.,0.]),candidates,
        [[candidates[0]],[candidates[1]]],[0.,.3],readable,readable,np.ones(2,bool),
        np.eye(3),rows,10.)
    assert chosen==1  # A modestly longer whole new face beats one old-face hole.
    assert rows==before and np.array_equal(policy._opportunities,bitmap)


def test_targets_ready_exits_even_when_all_opportunities_are_missing(monkeypatch):
    rows=cells();policy=TargetFirstCellScanPolicy(opportunity_navigation=True);feedback=actual_packet(rows)
    selective_reader(monkeypatch,policy,[])
    from plans.resonance_pc.src.actions import _deep_dive_target_readiness as readiness
    monkeypatch.setattr(readiness,'targets_readiness',lambda *args,**kwargs:dict(ready=True))
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    assert result['direction'] is None and result['reason']=='target_first_targets_ready'
    assert not policy._opportunities.any()


def test_historical_opportunity_missing_scoring_survives_stale_latest_source_without_new_bits(monkeypatch):
    rows=cells();before=deepcopy(rows);policy=TargetFirstCellScanPolicy(opportunity_navigation=True)
    feedback=actual_packet(rows);selective_reader(monkeypatch,policy,[0])
    record(policy,rows,feedback)
    policy._opportunities[:]=True;policy._opportunities[26]=False
    bitmap=policy._opportunities.copy();source=deepcopy(policy._opportunity_source)
    latest=deepcopy(feedback);latest['glyph_anchor_age_sec']=1.2
    latest['refine_diagnostic']['renewed']=False
    record(policy,rows,latest);policy._mixed_feedback=latest
    assert not policy._opportunity_current_valid and policy._target_packet_frame is None
    candidates=np.array([np.eye(3),_matrix(np.array([0.,.3,0.]))])
    readable=np.zeros((2,54));readable[1,26]=1.
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda rotations,atlas:np.ones(len(rotations)))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,atlas,**kwargs:np.ones(len(paths)))
    assert policy._planning_endpoint(0,np.array([100.,0.]),candidates,
        [[candidates[0]],[candidates[1]]],[0.,.3],readable,readable,np.ones(2,bool),
        np.eye(3),rows,11.)==1
    assert np.array_equal(policy._opportunities,bitmap) and policy._opportunity_source==source
    assert rows==before


def test_real_source_front_upper_glyphs_do_not_cover_hud_hidden_lower_cell():
    fixture=json.loads((Path(__file__).parent/'fixtures/deep_dive_navigation_sources_13.json').read_text())
    source=next(row for row in fixture['sources'] if row['frame_id']==10)
    rows=[dict(occupant='unknown',node_status='unknown',confidence=0.,evidence=[]) for _ in range(54)]
    before=deepcopy(rows);policy=TargetFirstCellScanPolicy(opportunity_navigation=True)
    record(policy,rows,source['feedback'])
    assert policy._opportunities[18:27].any()
    assert not policy._opportunities[26] and rows==before
    assert policy.summary()['navigation_opportunities']['source']['frame_id']==10


@pytest.mark.parametrize('invalid',[1,0,None,'False',np.bool_(True)])
def test_opportunity_navigation_requires_actual_bool(invalid):
    with pytest.raises(TypeError,match='opportunity_navigation must be bool'):
        TargetFirstCellScanPolicy(opportunity_navigation=invalid)


def test_default_ablation_records_bitmap_but_matches_cell_normal_plan_without_hint(monkeypatch):
    rows=cells()
    for row in rows:row['node_status']='known'
    for row in rows[45:54]:
        row.update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    before=deepcopy(rows);policy=TargetFirstCellScanPolicy(expected_inspirations=2)
    baseline=CellScanPolicy(recognition_goal='targets',expected_inspirations=2)
    feedback=actual_packet(rows);reader=policy._readability
    selective_reader(monkeypatch,policy,[0]);record(policy,rows,feedback)
    monkeypatch.setattr(policy,'_readability',reader)
    assert policy._opportunities[0] and not policy._opportunity_pending()
    assert policy.summary()['navigation_opportunities']['enabled'] is False
    pending=np.zeros(54,bool);weights=np.zeros(54)
    result=policy._planning_weights(rows,10.,pending,weights)
    assert result[0] is pending and result[1] is weights
    complete,seen=policy._objective(rows);basecomplete,baseseen=baseline._objective(rows)
    assert np.array_equal(complete,basecomplete) and np.array_equal(seen,baseseen)
    policy._mixed_feedback=feedback
    for instance in (policy,baseline):
        instance._last_gain=np.zeros(54);instance._last_attempt=np.full(54,-6.)
    observed=_matrix(np.array([0.,.5,0.]))
    policy._plan(observed,rows,AXES,10.);baseline._plan(observed,rows,AXES,10.)
    assert policy.target_face==baseline.target_face
    assert policy._target_indices==baseline._target_indices
    assert np.array_equal(policy._final_rotation,baseline._final_rotation)
    assert len(policy._route)==len(baseline._route)
    assert all(np.array_equal(a,b) for a,b in zip(policy._route,baseline._route))
    assert policy.stats.get('opportunity_navigation_plans',0)==0 and rows==before
