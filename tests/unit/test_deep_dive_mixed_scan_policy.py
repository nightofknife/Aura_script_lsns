"""Source-backed local confirmation never supplies synthetic atlas evidence."""
from copy import deepcopy
import math

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import (
    CellScanPolicy, MixedFaceScanPolicy, _matrix, _pose_angle,
)


AXES = {0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])}


def cells():
    rows = [dict(face=f,row=r,col=c,occupant='none',occupant_status='confirmed',
        node_status='unknown',confidence=.9,occupant_evidence_counts={'none':3},
        evidence=[dict(frame_id=g,group=g,occupant='none',confidence=.9)for g in (1,2,3)])
        for f in 'URFDLB' for r in range(3) for c in range(3)]
    for i,kind in ((4,'player'),(31,'singularity'),(26,'inspiration'),(50,'inspiration')):
        rows[i].update(occupant=kind,occupant_status='confirmed',confidence=.9,
            occupant_evidence_counts={kind:2},
            evidence=[dict(frame_id=g,group=g,occupant=kind,confidence=.9)for g in (1,2)])
    return rows


def snapshot(rows, associations=()):
    return dict(schema='resonance_pc.deep_dive_layout.v1',coordinate_frame='scan_local',
        cells=rows,faces_observed=6,map_valid=True,map_revision=0,semantic_map_revision=0,
        glyph_anchor_age_sec=.2,glyph_anchor_reason='known_multi_face_joint_fit',
        glyph_anchor_at=10.,glyph_anchor_rotation=np.eye(3).tolist(),
        refine_diagnostic=dict(source_frame_id=10,renewed=True),
        accepted_face_observation=dict(frame_id=10,rotation=np.eye(3).tolist(),cell_indices=[4,26]),
        semantic_metadata=dict(frame_id=10),
        player_cell=dict(face='U',row=1,col=1),singularity_cell=dict(face='D',row=1,col=1),
        inspiration_cells=[dict(face='F',row=2,col=2),dict(face='B',row=1,col=2)],
        target_candidate_associations=list(associations))


def lone_positive(rows,index=50,pose=None):
    pose=np.eye(3) if pose is None else pose
    rows[index].update(occupant='unknown',occupant_status='unknown',
        occupant_evidence_counts={'inspiration':1},confidence=0.,
        evidence=[dict(frame_id=9,group=5,occupant='inspiration',confidence=.83,
            association_evidence=dict(source_frame_id=9,source_map_revision=0,rotation=pose.tolist()))])


def test_lone_positive_interrupts_broad_route_at_real_source_pose():
    rows=cells();lone_positive(rows)
    before=deepcopy(rows);policy=MixedFaceScanPolicy(expected_inspirations=2)
    policy._route=[_matrix(np.array([0.,1.,0.]))]
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=snapshot(rows))
    assert result['reason']=='mixed_source_local_confirmation'
    assert result['mixed_scan']['local_cell_index']==50
    assert result['mixed_scan']['local_source_frame_id']==9
    assert math.radians(13.9)<=_pose_angle(policy._final_rotation,np.eye(3))<=math.radians(14.1)
    assert rows==before


def test_new_tracking_pose_is_not_used_as_the_positive_source_pose():
    rows=cells();origin=_matrix(np.array([0.,-.15,0.]))
    lone_positive(rows,pose=origin)
    policy=MixedFaceScanPolicy(expected_inspirations=2)
    observed=_matrix(np.array([.1,0.,0.]))
    task=policy._tasks(rows,snapshot(rows),observed,10.)[0]
    np.testing.assert_allclose(task['origin'],origin)


def test_missing_mismatched_or_other_epoch_pose_does_not_start_local_confirmation():
    for change in ('missing','source','epoch'):
        rows=cells();lone_positive(rows)
        association=rows[50]['evidence'][0]['association_evidence']
        if change=='missing':association.clear()
        elif change=='source':association['source_frame_id']=8
        else:association['source_map_revision']=1
        policy=MixedFaceScanPolicy(expected_inspirations=2)
        assert policy._tasks(rows,snapshot(rows),np.eye(3),10.)==[]


def test_weak_mask_candidate_does_not_start_confirmation_but_relevant_does():
    rows=cells();policy=MixedFaceScanPolicy(expected_inspirations=2)
    weak=dict(kind='singularity',confidence=.1,confirmable=False)
    assert policy._tasks(rows,snapshot(rows,[weak]),np.eye(3),10.)==[]
    weak.update(confidence=.94,confirmable=True)
    tasks=policy._tasks(rows,snapshot(rows,[weak]),np.eye(3),10.)
    assert tasks[0]['index']==31 and tasks[0]['reason']=='current_candidate_requires_association'


def test_four_confirmed_targets_do_not_waive_current_unassociated_candidate():
    rows=cells();policy=MixedFaceScanPolicy(expected_inspirations=2)
    feedback=snapshot(rows,[dict(kind='singularity',confidence=.94,confirmable=True)])
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    assert result['reason']=='mixed_source_local_confirmation'
    assert result['mixed_scan']['local_cell_index']==31


def test_public_readiness_stops_without_extra_full_nodes_or_near_views():
    rows=cells();before=deepcopy(rows);policy=MixedFaceScanPolicy(expected_inspirations=2)
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=snapshot(rows))
    assert result['direction'] is None and result['reason']=='mixed_targets_ready'
    assert rows==before and policy._plan_serial==0


def test_map_invalid_and_stale_anchor_cannot_be_waived_by_correct_inventory():
    rows=cells()
    for reason in ('map','age'):
        policy=MixedFaceScanPolicy(expected_inspirations=2);feedback=snapshot(rows)
        if reason=='map':feedback['map_valid']=False
        else:feedback['glyph_anchor_age_sec']=2.1;feedback['glyph_anchor_rotation']=None
        result=policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
        assert result['reason']!='mixed_targets_ready'


def test_local_retry_retains_cached_goal_and_does_not_resolve_missing_vote():
    rows=cells();lone_positive(rows)
    policy=MixedFaceScanPolicy(expected_inspirations=2)
    policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=snapshot(rows))
    goal=policy._final_rotation.copy()
    policy._plan(goal,policy._rows(rows),AXES,10.7)
    np.testing.assert_allclose(policy._final_rotation,goal)
    assert policy._local_attempts==2 and rows[50]['occupant_status']=='unknown'


def test_unseen_face_is_not_consumed_by_predicting_or_replanning_a_view():
    rows=cells()
    for row in rows[45:54]:row.update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    policy=MixedFaceScanPolicy(expected_inspirations=2)
    policy._last_gain=np.zeros(54);policy._last_attempt=np.full(54,-6.)
    start=_matrix(np.array([0.,.5,0.]))
    policy._plan(start,rows,AXES,10.)
    assert policy._coverage_face=='B' and policy.target_face=='B'
    first=policy._final_rotation.copy()
    policy._plan(first,rows,AXES,12.)
    assert policy._coverage_face=='B'
    assert all(not row['evidence']for row in rows[45:54])


def test_actual_face_evidence_releases_frontier_without_full_occupancy():
    rows=cells();policy=MixedFaceScanPolicy(expected_inspirations=2)
    policy._coverage_face='B'
    policy._last_gain=np.zeros(54);policy._last_attempt=np.full(54,-6.)
    # B remains mostly ordinary unknown; its actual source-backed observation
    # is a scheduling opportunity, not nine synthetic none labels.
    for row in rows[45:54]:row.update(occupant='unknown',occupant_status='unknown',occupant_evidence_counts={})
    rows[50].update(occupant='inspiration',occupant_status='confirmed',
        occupant_evidence_counts={'inspiration':2},
        evidence=[dict(group=g,frame_id=g,occupant='inspiration')for g in (1,2)])
    policy._plan(np.eye(3),rows,AXES,10.)
    assert policy._coverage_face is None
    assert rows[45]['occupant']=='unknown'


def test_full_mode_keeps_original_cell_completion():
    rows=cells()
    for row in rows:row['node_status']='known'
    assert MixedFaceScanPolicy(recognition_goal='full').choose(np.eye(3),rows,AXES,elapsed=0.)['reason']==CellScanPolicy().choose(np.eye(3),rows,AXES,elapsed=0.)['reason']


def test_local_failure_cools_down_but_preserves_real_positive_deficit():
    rows=cells();lone_positive(rows)
    policy=MixedFaceScanPolicy(expected_inspirations=2)
    feedback=snapshot(rows)
    policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    signature=policy._local_task['signature']
    policy._local_attempts=2
    policy._plan(policy._final_rotation,policy._rows(rows),AXES,11.)
    assert policy._local_task is None
    assert policy._local_cooldowns[signature]==15.
    assert policy._tasks(policy._rows(rows),feedback,np.eye(3),12.)==[]
    assert not policy._objective(policy._rows(rows))[0][50]
    assert rows[50]['occupant_status']=='unknown'
    assert policy._tasks(policy._rows(rows),feedback,np.eye(3),15.)[0]['index']==50


def test_anchor_recovery_thresholds_apply_with_a_pending_positive_task():
    rows=cells();lone_positive(rows)
    policy=MixedFaceScanPolicy(expected_inspirations=2)
    feedback=snapshot(rows);feedback['glyph_anchor_age_sec']=1.3
    feedback['refine_diagnostic']=dict(source_frame_id=1,renewed=False)
    policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    feedback['refine_diagnostic']['source_frame_id']=2
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.1,anchor_feedback=feedback)
    assert policy._anchor_recovery_count==1
    assert policy._anchor_recovery is not None
    assert result['direction'] is None and result['reason']=='await_current_anchor_evidence'


def test_candidate_task_does_not_restart_when_source_temporarily_ages():
    rows=cells();policy=MixedFaceScanPolicy(expected_inspirations=2)
    feedback=snapshot(rows,[dict(kind='singularity',confidence=.94,confirmable=True)])
    policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    task=policy._local_task;goal=policy._local_goal.copy()
    feedback['glyph_anchor_age_sec']=1.
    policy.choose(np.eye(3),rows,AXES,elapsed=10.1,anchor_feedback=feedback)
    assert policy._local_task is task
    np.testing.assert_allclose(policy._local_goal,goal)


def test_second_real_positive_clears_local_task_without_changing_atlas():
    rows=cells();lone_positive(rows)
    policy=MixedFaceScanPolicy(expected_inspirations=2)
    policy.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=snapshot(rows))
    rows=cells();before=deepcopy(rows)
    feedback=snapshot(rows);feedback['map_valid']=False
    result=policy.choose(np.eye(3),rows,AXES,elapsed=10.2,anchor_feedback=feedback)
    assert policy._local_task is None
    assert rows==before and result['reason']!='mixed_targets_ready'
