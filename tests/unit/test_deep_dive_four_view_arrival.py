import cv2
import numpy as np
import pytest
from research.deep_dive_four_view_arrival import ReferenceLineArrival,OrderedArrivalGate


def frame_and_anchors():
    rgb=np.zeros((720,1280,3),np.uint8);anchors=[]
    for i in range(8):
        x=320+(i%2)*270;y=110+(i//2)*100
        end=(x+120,y) if i%2==0 else (x,y+70)
        cv2.line(rgb,(x,y),end,(230,230,230),2)
        anchors.append(dict(id=str(i),region=('upper_' if i<4 else 'lower_')+('left' if i%2==0 else 'right'),
            endpoints=[[x,y],list(end)]))
    return rgb,anchors


def test_edge_baselines_use_actual_pixels_and_detect_signed_shift():
    rgb,anchors=frame_and_anchors();probe=ReferenceLineArrival(rgb,anchors)
    reference=probe.measure(rgb)
    assert reference['geometry_match_candidate']
    shifted=cv2.warpAffine(rgb,np.array([[1.,0,0],[0,1.,6]]),(1280,720))
    measured=probe.measure(shifted)
    assert not measured['geometry_match_candidate']
    assert measured['positive_departure_evidence']
    assert np.linalg.norm(measured['inferred_translation_px'])==pytest.approx(6,abs=.2)


def test_blank_image_unknown_is_not_departure_or_arrival():
    rgb,anchors=frame_and_anchors();probe=ReferenceLineArrival(rgb,anchors)
    measured=probe.measure(np.zeros_like(rgb))
    assert measured['status']=='unknown'
    assert not measured['positive_departure_evidence']
    assert not measured['identity_from_geometry']


def test_single_local_region_cannot_establish_complete_arrival():
    rgb,anchors=frame_and_anchors()
    for a in anchors:a['region']='upper_left'
    assert ReferenceLineArrival(rgb,anchors).measure(rgb)['status']=='unknown'


def complete_one(gate,begin=0):
    matching=dict(geometry_match_candidate=True)
    for i,t in enumerate((0,.08,.16,.23,.35)):
        result=gate.observe(begin+i,1.+t,matching,released=i>=3)
    return result


def test_arrival_requires_new_frames_then_released_new_frames():
    gate=OrderedArrivalGate(reset_confirmed=True)
    result=complete_one(gate)
    assert result['status']=='ordered_arrival_confirmed' and result['view_index']==0
    assert gate.target==1 and not gate.left_previous
    result=gate.observe(5,1.40,dict(geometry_match_candidate=True))
    assert result['status']=='waiting_actual_departure'


def test_lost_previous_lines_not_positive_departure():
    gate=OrderedArrivalGate(reset_confirmed=True);complete_one(gate)
    for i in range(5,9):
        result=gate.observe(i,1.4+(i-5)*.1,dict(geometry_match_candidate=True),
            previous_measurement=dict(status='unknown',positive_departure_evidence=False))
    assert result['status']=='waiting_actual_departure' and gate.target==1


def test_two_measured_departures_required_before_next_matching_frame():
    gate=OrderedArrivalGate(reset_confirmed=True);complete_one(gate)
    departing=dict(positive_departure_evidence=True)
    assert gate.observe(5,1.4,{},previous_measurement=departing)['status']=='waiting_actual_departure'
    assert gate.observe(6,1.5,{},previous_measurement=departing)['status']=='approach_or_unknown'
    assert gate.left_previous and gate.target==1


def test_duplicate_or_old_frame_revokes_match_streak():
    gate=OrderedArrivalGate(reset_confirmed=True);match=dict(geometry_match_candidate=True)
    gate.observe(1,1.,match);gate.observe(2,1.1,match)
    assert gate.observe(2,1.1,match)['status']=='source_discontinuity_requires_reacquisition'
    assert not gate.matches and not gate.await_release


def test_reset_required_and_image_contract():
    with pytest.raises(ValueError):OrderedArrivalGate(reset_confirmed=False)
    rgb,anchors=frame_and_anchors()
    with pytest.raises(ValueError):ReferenceLineArrival(rgb[:600],anchors)


def test_gap_cannot_skip_unknown_part_of_symmetric_route():
    gate=OrderedArrivalGate(reset_confirmed=True)
    gate.observe(1,1.,{})
    assert gate.observe(2,3.,{})['status']=='capture_gap_requires_reset'
    assert gate.observe(3,3.1,dict(geometry_match_candidate=True))['status']=='capture_gap_requires_reset'
