"""Actual multi-face recovery navigation; no capture, inference or input."""
from copy import deepcopy
import math

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix, _pose_angle


AXES = {0:np.array([0., .003, 0.]), 1:np.array([.003, 0., 0.])}


def rows():
    return [dict(face=face,row=row,col=col,occupant='unknown',occupant_status='unknown',
                 node_status='unknown',confidence=0.,occupant_evidence_counts={},evidence=[])
            for face in 'URFDLB' for row in range(3) for col in range(3)]


def feedback(fid=1,stamp=100.,*,faces=None,renewed=True,basis=None,source_basis=None,rotation=None,age=.2):
    basis = np.eye(3) if basis is None else basis
    source_basis = basis if source_basis is None else source_basis
    rotation = source_basis if rotation is None else rotation
    faces = {'D':4,'L':9} if faces is None else faces
    result = dict(session_id=999,map_revision=0,correction_epoch=0,semantic_revision=fid,
        geometry_body_basis=basis.tolist(),glyph_anchor_at=stamp,glyph_anchor_age_sec=age,
        glyph_anchor_frame_id=fid,glyph_anchor_map_revision=0,glyph_anchor_rotation=rotation.tolist(),
        refine_diagnostic=dict(source_frame_id=fid,source_frame_time=stamp,source_map_revision=0,
                               renewed=renewed,accepted_faces=faces))
    if renewed:
        result['accepted_anchor_observation'] = dict(frame_id=fid,frame_time=stamp,session_id=999,
            map_revision=0,rotation=rotation.tolist(),body_basis=source_basis.tolist(),accepted_faces=faces)
    return result


def subject(monkeypatch):
    controller = CellScanPolicy(recognition_goal='targets',expected_inspirations=2)
    calls = []
    def plan(observed,atlas,axes,now):
        calls.append(now)
        controller._route = [_matrix(np.array([0.,math.radians(20.),0.])) @ observed]
        controller._final_rotation = controller._route[-1].copy()
        controller._target_indices = [26]
        controller.target_face = 'F'
        controller._started = now
        controller._at_goal_since = None
        controller._plan_serial += 1
    monkeypatch.setattr(controller,'_plan',plan)
    return controller,calls


def enter_recovery(monkeypatch):
    controller,calls = subject(monkeypatch);atlas = rows()
    controller.choose(np.eye(3),atlas,AXES,elapsed=0.,anchor_feedback=feedback())
    actual = _matrix(np.array([0.,math.radians(6.),0.]))
    failed = feedback(2,101.,renewed=False,age=1.4)
    failed.update(glyph_anchor_at=100.,glyph_anchor_frame_id=1,glyph_anchor_rotation=np.eye(3).tolist())
    controller.choose(actual,atlas,AXES,elapsed=1.7,anchor_feedback=failed)
    failed['semantic_revision']=3;failed['refine_diagnostic']['source_frame_id']=3
    choice = controller.choose(actual,atlas,AXES,elapsed=1.8,anchor_feedback=failed)
    assert choice['phase'] == 'anchor_recovery'
    assert controller._anchor_recovery['multi_face']
    return controller,atlas,calls


def test_single_face_renewal_cannot_resume_failed_coverage(monkeypatch):
    controller,atlas,calls = enter_recovery(monkeypatch)
    before = deepcopy(atlas)
    choice = controller.choose(np.eye(3),atlas,AXES,elapsed=2.,anchor_feedback=feedback(4,102.,faces={'D':4}))
    assert choice['reason'] == 'await_current_anchor_evidence'
    assert controller._anchor_recovery is not None and len(calls) == 1
    assert atlas == before


def test_fresh_multiface_source_finishes_once_and_marks_failed_pose(monkeypatch):
    controller,atlas,calls = enter_recovery(monkeypatch)
    source = feedback(4,102.,faces={'D':4,'L':2})
    controller.choose(np.eye(3),atlas,AXES,elapsed=2.,anchor_feedback=source)
    assert controller._anchor_recovery is None and len(calls) == 2
    assert controller.stats['multi_face_recoveries'] == 1
    assert controller._cell_failures[-1][1] == [26]
    assert controller.insufficient[-1]['reason'] == 'coverage_failed_multi_face_recovery'
    assert atlas[26]['occupant'] == 'unknown'
    controller.choose(np.eye(3),atlas,AXES,elapsed=2.1,anchor_feedback=source)
    assert controller.stats['multi_face_recoveries'] == 1


def test_prediction_or_history_cannot_acknowledge_actual_arrival(monkeypatch):
    controller,atlas,_ = enter_recovery(monkeypatch)
    actual = _matrix(np.array([0.,math.radians(6.),0.]))
    source = feedback(4,102.,faces={'D':4,'L':2})
    controller.choose(np.eye(3),atlas,AXES,observed_rotation=actual,elapsed=2.,anchor_feedback=source)
    assert controller._anchor_recovery is not None
    # Freshness of an old support frame also cannot manufacture a new proof.
    old = feedback();old['glyph_anchor_at']=105.
    controller.choose(np.eye(3),atlas,AXES,elapsed=2.1,anchor_feedback=old)
    assert controller._anchor_recovery is not None


@pytest.mark.parametrize('faces', [{'D':4},{'D':5,'L':1},{'D':2,'L':2}])
def test_recovery_departure_requires_six_real_glyphs_across_two_supported_faces(monkeypatch,faces):
    controller,atlas,_ = enter_recovery(monkeypatch)
    controller.choose(np.eye(3),atlas,AXES,elapsed=2.,anchor_feedback=feedback(4,102.,faces=faces))
    assert controller._anchor_recovery is not None


@pytest.mark.parametrize('defect', ['frame','time','map','session','rotation','basis','fit'])
def test_unbound_or_invalid_source_does_not_renew_navigation_support(monkeypatch,defect):
    controller,atlas,_ = enter_recovery(monkeypatch)
    source = feedback(4,102.,faces={'D':4,'L':2});proof=source['accepted_anchor_observation']
    if defect == 'frame':proof['frame_id']=999
    if defect == 'time':proof['frame_time'] += .01
    if defect == 'map':proof['map_revision']=1
    if defect == 'session':proof['session_id']='other'
    if defect == 'rotation':proof['rotation'][2][2]=-1.
    if defect == 'basis':proof['body_basis'][0][0]=2.
    if defect == 'fit':source['refine_diagnostic']['renewed']=False
    controller.choose(np.eye(3),atlas,AXES,elapsed=2.,anchor_feedback=source)
    assert controller._anchor_recovery is not None


def test_noncommuting_applied_body_changes_transform_goal_on_the_right(monkeypatch):
    controller,calls=subject(monkeypatch);atlas=rows()
    physical=_matrix(np.array([.3,-.4,.2]))
    first_basis=_matrix(np.array([.02,0.,0.]))
    semantic_body=_matrix(np.array([0.,.015,0.]))
    source_basis=first_basis @ semantic_body
    initial=feedback(basis=first_basis,source_basis=source_basis,rotation=physical @ source_basis)
    controller.choose(physical @ first_basis,atlas,AXES,elapsed=0.,anchor_feedback=initial)
    updated=first_basis @ _matrix(np.array([0.,0.,.04]))
    actual=_matrix(np.array([0.,math.radians(6.),0.])) @ physical @ updated
    failed=feedback(2,101.,renewed=False,basis=updated,age=1.4)
    failed.update(glyph_anchor_at=100.,glyph_anchor_frame_id=1,
                  glyph_anchor_rotation=(physical @ source_basis).tolist())
    controller.choose(actual,atlas,AXES,elapsed=1.7,anchor_feedback=failed)
    failed['semantic_revision']=3;failed['refine_diagnostic']['source_frame_id']=3
    controller.choose(actual,atlas,AXES,elapsed=1.8,anchor_feedback=failed)
    assert np.allclose(controller._anchor_recovery['goal'],physical @ updated)
    # A second body correction changes neither physical error nor arrival.
    newest=updated @ _matrix(np.array([-.03,.01,0.]))
    failed['geometry_body_basis']=newest.tolist();failed['correction_epoch']=2
    actual_new=actual @ updated.T @ newest
    controller.choose(physical @ newest,atlas,AXES,observed_rotation=actual_new,elapsed=1.9,anchor_feedback=failed)
    assert controller._anchor_recovery is not None
    assert np.allclose(controller._anchor_recovery['goal'],physical @ newest)
    assert _pose_angle(controller._anchor_recovery['goal'],actual_new) == pytest.approx(math.radians(6.))


def test_map_or_session_change_discards_cached_navigation(monkeypatch):
    controller,atlas,_=enter_recovery(monkeypatch)
    changed=feedback(4,102.,faces={'D':4});changed['session_id']='new'
    controller.choose(np.eye(3),atlas,AXES,elapsed=2.,anchor_feedback=changed)
    assert controller._multi_face_anchor is None
    assert controller._anchor_recovery is None


def test_recent_actual_target_gain_preserves_legacy_recovery(monkeypatch):
    controller,calls=subject(monkeypatch);atlas=rows()
    controller.choose(np.eye(3),atlas,AXES,elapsed=0.,anchor_feedback=feedback())
    atlas[26]['evidence']=[dict(frame_id=2,group=2,occupant='inspiration',confidence=.9,
        association_evidence=dict(source_frame_id=2,source_map_revision=0,rotation=np.eye(3).tolist()))]
    failed=feedback(2,101.,renewed=False,age=1.4)
    failed.update(glyph_anchor_at=100.,glyph_anchor_frame_id=1)
    actual=_matrix(np.array([0.,math.radians(6.),0.]))
    controller.choose(actual,atlas,AXES,elapsed=1.7,anchor_feedback=failed)
    failed['semantic_revision']=3;failed['refine_diagnostic']['source_frame_id']=3
    controller.choose(actual,atlas,AXES,elapsed=1.8,anchor_feedback=failed)
    assert not controller._anchor_recovery['multi_face']


def test_strengthened_recovery_retains_original_timeout(monkeypatch):
    controller,atlas,_=enter_recovery(monkeypatch)
    result=controller.choose(np.eye(3),atlas,AXES,elapsed=6.,anchor_feedback=feedback(4,102.,faces={'D':4}))
    assert result['reason'] == 'anchor_recovery_failed'
