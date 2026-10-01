"""Target-first routing uses actual face evidence, never missing page labels."""
from copy import deepcopy
import math

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix, _pose_angle


AXES = {0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])}


def cells():
    rows = [dict(face=face, row=row, col=col, occupant='none',
                 occupant_status='confirmed', node_status='unknown', confidence=.9,
                 occupant_evidence_counts={'none': 3},
                 evidence=[dict(group=g, frame_id=g, occupant='none') for g in (1,2,3)])
            for face in 'URFDLB' for row in range(3) for col in range(3)]
    for index, kind in ((4,'player'), (31,'singularity'), (3,'inspiration'), (35,'inspiration')):
        rows[index].update(occupant=kind, node_status='not_required_target',
            occupant_evidence_counts={kind:2},
            evidence=[dict(group=g,frame_id=g,occupant=kind) for g in (1,2)])
    return rows


def policy(bound=2):
    result=CellScanPolicy(recognition_goal='targets',expected_inspirations=bound)
    result._last_gain=np.zeros(54)
    result._last_attempt=np.full(54,-6.)
    return result


def test_full_alias_and_default_keep_unknown_page_deficits():
    rows=cells()
    assert np.array_equal(CellScanPolicy()._objective(rows)[0],
                          CellScanPolicy(recognition_goal='full')._objective(rows)[0])
    assert sum(CellScanPolicy()._objective(rows)[0])==4
    assert all(policy()._objective(rows)[0])


def test_hud_closure_does_not_assign_unknown_ordinary_cells():
    rows=cells()
    for index,row in enumerate(rows):
        if index not in (3,4,31,35):
            row.update(occupant='unknown',occupant_status='unknown',occupant_evidence_counts={})
    before=deepcopy(rows)
    assert all(policy()._objective(rows)[0])
    assert rows==before and rows[1]['occupant']=='unknown'
    assert not policy(3)._objective(rows)[0][1]


def test_unconfirmed_positive_and_nonindependent_target_remain_real_deficits():
    rows=cells();rows[1].update(occupant='unknown',occupant_status='unknown',
        occupant_evidence_counts={'inspiration':1})
    assert not policy()._objective(rows)[0][1]
    rows=cells();rows[31]['evidence'][1]['group']=1
    assert not policy()._objective(rows)[0][31]


def test_predicted_labels_without_source_frames_are_not_six_face_coverage():
    rows=cells()
    for row in rows[27:36]:row['evidence']=[]
    complete,seen=policy()._objective(rows)
    assert not complete[31] and not any(seen[27:36])
    rows[27]['evidence']=[dict(group=1)]
    assert not policy()._objective(rows)[1][27]


def test_first_actual_missing_face_owns_route_even_with_old_page_deficits():
    rows=cells()
    for row in rows[27:36]:
        row.update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    before=deepcopy(rows);controller=policy()
    start=_matrix(np.array([-.5,.5,0.]))
    controller._plan(start,rows,AXES,30.)
    assert controller.target_face=='D'
    assert controller._target_indices and all(27<=index<36 for index in controller._target_indices)
    assert rows==before
    previous=start
    for waypoint in controller._route:
        angle=math.acos(float(np.clip((np.trace(waypoint@previous.T)-1.)*.5,-1.,1.)))
        assert angle<=math.radians(30.)+1e-8
        previous=waypoint


def test_hud_closed_unknown_node_never_restarts_a_coverage_route():
    controller=policy()
    result=controller.choose(np.eye(3),cells(),AXES,elapsed=30.)
    assert result['phase']=='observe' and result['reason']=='target_objective_covered_await_readiness'
    assert controller._plan_serial==0


def test_current_relevant_candidate_requests_real_slot_but_mask_only_does_not():
    controller=policy();rows=cells()
    weak=dict(kind='inspiration',confidence=.1,confirmable=False,cell_index=1)
    assert controller._candidate_deficits(rows,dict(target_candidate_associations=[weak]))==set()
    weak['confidence']=.25
    assert controller._candidate_deficits(rows,dict(target_candidate_associations=[weak]))=={1}
    controller._current_target_deficits={1}
    assert not controller._objective(rows)[0][1]
    weak['cell_index']=3
    assert controller._candidate_deficits(rows,dict(target_candidate_associations=[weak]))==set()


def test_covered_goal_still_obeys_anchor_recovery_gate():
    controller=policy();rows=cells()
    feedback=dict(glyph_anchor_age_sec=1.3,glyph_anchor_at=1.,glyph_anchor_rotation=np.eye(3),
                  refine_diagnostic=dict(source_frame_id=1,renewed=False))
    controller.choose(np.eye(3),rows,AXES,elapsed=10.,anchor_feedback=feedback)
    feedback['refine_diagnostic']['source_frame_id']=2
    controller.choose(np.eye(3),rows,AXES,elapsed=10.1,anchor_feedback=feedback)
    assert controller._anchor_recovery_count==1 and controller._anchor_recovery is not None


def test_first_face_opportunity_saturates_before_remote_full_grid_reward():
    controller=policy();weights=np.ones(54)
    readable=np.zeros((3,54))
    readable[0,27:30]=1.
    readable[1,27:36]=1.
    readable[2,27:29]=1.
    gain=controller._coverage_gain(readable,weights,{'D'})
    assert gain.tolist()==[1.,1.,0.]
    assert gain[0]/(.65+math.radians(30.)/.3)>gain[1]/(.65+math.radians(110.)/.3)


def test_coverage_route_chooses_short_face_prefix_over_far_nine_cell_endpoint(monkeypatch):
    rows=cells()
    for index in list(range(27,36))+list(range(45,54)):
        rows[index].update(occupant='unknown',occupant_status='unknown',
                           evidence=[],occupant_evidence_counts={})
    before=deepcopy(rows);controller=policy()
    def visibility(poses,rows,**kwargs):
        result=np.zeros((len(poses),54))
        for index,pose in enumerate(poses):
            angle=_pose_angle(pose,np.eye(3))
            if .3<=angle<=.6:result[index,27:30]=1.
            if angle>=1.3:result[index,45:54]=1.
        return result
    monkeypatch.setattr(controller,'_readability',visibility)
    controller._plan(np.eye(3),rows,AXES,30.)
    assert controller.target_face=='D'
    assert controller._target_indices==[27,28,29]
    assert sum(_pose_angle(point,np.eye(3)) for point in controller._route)<math.radians(35.)
    # The original full-map objective still has its original nine-cell reward.
    baseline=CellScanPolicy();baseline._last_gain=np.zeros(54);baseline._last_attempt=np.full(54,-6.)
    monkeypatch.setattr(baseline,'_readability',visibility)
    baseline._plan(np.eye(3),rows,AXES,30.)
    assert baseline.target_face=='B'
    assert rows==before


def test_prefix_candidates_preserve_reachable_paths_and_exact_axis_endpoints():
    controller=policy();start=_matrix(np.array([.2,-.3,.1]))
    intermediate=_matrix(np.array([0.,math.radians(110.),0.]))@start
    final=_matrix(np.array([math.radians(75.),0.,0.]))@intermediate
    other=_matrix(np.array([math.radians(-45.),0.,0.]))@intermediate
    poses,paths,costs=controller._coverage_prefixes(start,[[intermediate,final],[intermediate,other]])
    assert len(poses)==len(paths)==len(costs)
    assert sum(_pose_angle(pose,intermediate)<1e-7 for pose in poses)==1
    assert any(np.array_equal(pose,final) for pose in poses)
    assert any(np.array_equal(pose,other) for pose in poses)
    for pose,path,cost in zip(poses,paths,costs):
        assert np.array_equal(pose,path[-1])
        previous=start;travel=0.
        for point in path:
            angle=_pose_angle(point,previous)
            assert angle<=math.radians(30.)+1e-8
            travel+=angle;previous=point
        assert cost==pytest.approx(travel)


def test_travel_rate_uses_fresh_observed_pose_not_extrapolation():
    controller=policy();rows=cells();moving=_matrix(np.array([.1,0.,0.]))
    controller.choose(np.eye(3),rows,AXES,observed_rotation=np.eye(3),elapsed=20.)
    controller.choose(moving,rows,AXES,observed_rotation=np.eye(3),elapsed=20.1)
    assert controller._observed_motion_rate is None
    controller.choose(moving,rows,AXES,observed_rotation=moving,elapsed=20.2)
    assert controller._observed_motion_rate==pytest.approx(1.)


@pytest.mark.parametrize('goal,bound',[('other',2),('targets',True),('targets',-1)])
def test_invalid_goal_parameters_reject(goal,bound):
    with pytest.raises(ValueError):CellScanPolicy(recognition_goal=goal,expected_inspirations=bound)
