"""Reading-only orientation is never a substitute for strict action registration."""
from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_anchor_reader as reader
from plans.resonance_pc.src.actions import _deep_dive_operation_frame as operation
from plans.resonance_pc.src.actions._deep_dive_planner_rules import coord_dict


FIXTURES=Path(__file__).parents[1]/'fixtures/deep_dive_anchor'


def test_real_two_known_faces_only_propose_reading_orientation():
    saved=json.loads((FIXTURES/'reading_only_binding04.json').read_text(encoding='utf-8'))
    image=cv2.cvtColor(cv2.imread(str(FIXTURES/'reading_only_binding04.png')),cv2.COLOR_BGR2RGB)
    before=deepcopy(saved['layout'])
    detector=lambda rgb:deepcopy(saved['model_packet'])
    proposal=reader.propose_read_only_wide_reference(image,saved['layout'],target_detector=detector)
    assert proposal['status']=='reading_only'
    assert proposal['input_authorized'] is False and proposal['operation_ready'] is False
    assert proposal['known_face_counts']==[7,0,6] and proposal['deficient_faces']==[1]
    assert proposal['eligible_slots']==list(range(9,18))
    assert len(proposal['Q_candidates'])==1
    assert proposal['geometry_quality']['anchors']>=7
    assert min(proposal['geometry_quality']['face_anchors'])>=2
    assert proposal['geometry_quality']['rmse_px']<=6
    assert saved['layout']==before
    # Independent validation uses the current image, rather than trusting the
    # weakened known-face counts as an action-grade quality declaration.
    valid=reader._validate_current_proposal(image,saved['layout'],proposal,saved['model_packet']['targets'])
    assert valid[1]['face_anchors']==[7,0,6]
    cells,actor=operation._layout(saved['layout'])
    with pytest.raises(ValueError,match='reference_not_ready'):
        operation._reference_frame(proposal,cells,actor,0,0,1)
    assert operation.bind_move(proposal,1)['status']=='blocked'
    assert operation.bind_rotation(proposal,0)['status']=='blocked'


def test_cached_reading_direction_requires_new_pixel_fit(monkeypatch):
    saved=json.loads((FIXTURES/'reading_only_binding04.json').read_text(encoding='utf-8'))
    image=cv2.cvtColor(cv2.imread(str(FIXTURES/'reading_only_binding04.png')),cv2.COLOR_BGR2RGB)
    image[0,0,0]^=1  # Synthetic HUD-only change, never a claimed WGC capture.
    calls=[]
    monkeypatch.setattr(operation,'_refine',lambda *a,**k:calls.append(1) or None)
    with pytest.raises(ValueError,match='current_geometry_insufficient'):
        reader._validate_current_proposal(image,saved['layout'],saved['initial_proposal'],saved['model_packet']['targets'])
    assert calls==[1]


@pytest.fixture
def setup_reader(monkeypatch):
    image=np.zeros((720,1280,3),np.uint8)
    cells={s:dict(coord_dict(s),occupant='unknown',occupant_status='unknown',
                  node_status='unknown',icon_id=None,confidence=0.,evidence=[],occupant_evidence_counts={})
           for s in range(54)}
    layout=dict(cells=list(cells.values()))
    proposal=dict(status='reading_only',source_session=7,layout_digest='proven_layout',
                  pose=dict(K=operation._NORMAL_K.tolist(),rvec=[0.,0.,0.],tvec=[0.,0.,40.]))
    mapping=dict(op_to_logical=list(range(54)),face_anchors=[7,0,6])
    surface=[]
    for slot,x in ((9,400.),(10,500.)):
        surface.append(dict(operation_slot=slot,centre=np.array([x+35,235.]),
                            quad=np.array([[x,200.],[x+70,200.],[x+70,270.],[x,270.]])))
    monkeypatch.setattr(reader,'targets_readiness',lambda value:dict(ready=True))
    monkeypatch.setattr(reader,'_validate_current_proposal',lambda *a:(cells,mapping,surface,np.eye(3),np.array([[0.],[0.],[40.]])))
    monkeypatch.setattr(reader,'classify_icon',lambda crop:dict(icon_id='blue_scales',confidence=.85))
    packet=dict(targets=[],model_executed=True,coverage_valid=True)
    return image,layout,proposal,{},packet,cells,mapping,surface


def read(case,generation,timestamp,**changes):
    image,layout,proposal,votes,packet,*_=case
    arguments=dict(source_id=f'7:{generation}',source_time=timestamp,source_session=7,
        source_generation=generation,source_backend='wgc',target_packet=packet,now=timestamp+.1)
    arguments.update(changes)
    return reader.read_missing_registration_anchors(image,layout,proposal,votes,**arguments)


def test_three_new_captures_only_fill_minimum_missing_face_nodes(setup_reader):
    case=setup_reader;before=deepcopy(case[1])
    assert not read(case,1,10.)['ready']
    assert not read(case,2,10.2)['ready']
    result=read(case,3,10.5)
    assert result['ready'] and result['status']=='reading_only'
    assert result['input_authorized'] is False and result['requires_strict_public_rebuild']
    assert [row['face'] for row in result['cells']]==['R','R']
    assert [row['col'] for row in result['cells']]==[0,1]
    for row in result['cells']:
        proof=row['node_read_evidence']
        assert row['icon_id']=='blue_scales' and row['occupant']=='none'
        assert proof['source_ids']==['7:1','7:2','7:3']
        assert proof['target_inventory_closed'] and proof['no_target_box_overlap']
        assert all(s['image_digest']==reader._digest(case[0]) for s in proof['sources'])
        assert all(s['pose']==result['current_pose'] for s in proof['sources'])
    assert case[1]==before


def test_duplicates_or_short_span_cannot_supply_three_sources(setup_reader):
    case=setup_reader
    read(case,1,10.)
    assert read(case,1,10.)['reason']=='anchor_read_source_not_new'
    read(case,2,10.1)
    assert not read(case,3,10.3)['ready']


@pytest.mark.parametrize('change,reason',[
    ({'source_backend':'screenshot'},'anchor_read_fresh_wgc_source_required'),
    ({'source_id':'different'},'anchor_read_fresh_wgc_source_required'),
    ({'source_time':float('nan')},'anchor_read_fresh_wgc_source_required'),
    ({'source_generation':True},'anchor_read_fresh_wgc_source_required'),
    ({'source_session':8,'source_id':'8:2'},'anchor_read_source_session_changed'),
    ({'now':11.},'anchor_read_source_stale'),
])
def test_invalid_source_clears_previous_votes(setup_reader,change,reason):
    case=setup_reader;read(case,1,10.)
    result=read(case,2,10.2,**change)
    assert not result['ready'] and result['reason']==reason and not case[3]


@pytest.mark.parametrize('packet',[None,[],{'targets':[]},
    {'targets':[],'model_executed':True,'coverage_valid':False},
    {'targets':[],'model_executed':False,'coverage_valid':True}])
def test_model_coverage_is_mandatory(setup_reader,packet):
    result=read(setup_reader,1,10.,target_packet=packet)
    assert result['reason']=='anchor_read_model_coverage_required'


def test_any_weak_target_box_overlap_discards_that_node(setup_reader):
    case=setup_reader
    case[4]['targets']=[dict(kind='inspiration',box=[469,230,2,2],point=[470,231],
                              confidence=.05,confirmable=False)]
    for generation,timestamp in ((1,10.),(2,10.2),(3,10.5)):
        result=read(case,generation,timestamp)
    assert not result['ready'] and '9' not in case[3]


def test_unknown_cell_with_any_target_vote_cannot_become_ordinary(setup_reader):
    case=setup_reader
    case[5][9]['occupant_evidence_counts']={'singularity':1}
    for generation,timestamp in ((1,10.),(2,10.2),(3,10.5)):
        result=read(case,generation,timestamp)
    assert not result['ready'] and '9' not in case[3]


def test_changed_q_or_pose_discards_previous_sources(setup_reader):
    case=setup_reader;read(case,1,10.)
    case[6]['op_to_logical'][9],case[6]['op_to_logical'][10]=10,9
    result=read(case,2,10.2)
    assert result['reason']=='anchor_read_pose_or_q_unstable' and not case[3]


def test_only_unknown_nodes_on_deficient_faces_are_read(setup_reader):
    case=setup_reader
    case[5][9].update(node_status='known',icon_id='purple_ring')
    for generation,timestamp in ((1,10.),(2,10.2),(3,10.5)):
        result=read(case,generation,timestamp)
    assert not result['ready'] and '9' not in case[3]


def test_weak_or_conflicting_new_glyph_discards_votes(monkeypatch,setup_reader):
    case=setup_reader;read(case,1,10.)
    monkeypatch.setattr(reader,'classify_icon',lambda crop:dict(icon_id='blue_scales',confidence=.69))
    result=read(case,2,10.2)
    assert not result['ready'] and '9' not in case[3] and '10' not in case[3]


def test_end_of_computation_age_is_checked_not_just_entry_age(monkeypatch,setup_reader):
    ticks=iter([10.,10.01,10.02,10.03,10.51])
    monkeypatch.setattr(reader.time,'monotonic',lambda:next(ticks))
    result=read(setup_reader,1,10.,now=None)
    assert result['reason']=='anchor_read_source_stale'
    assert result['source_age_sec']==pytest.approx(.51) and not setup_reader[3]


def test_proofs_reference_each_new_source_rgb_not_initial_seed(setup_reader):
    case=setup_reader
    hashes=[]
    for generation,timestamp in ((1,10.),(2,10.2),(3,10.5)):
        case[0][0,0,0]=generation
        hashes.append(reader._digest(case[0]))
        result=read(case,generation,timestamp)
    assert result['ready']
    assert [s['image_digest'] for s in result['cells'][0]['node_read_evidence']['sources']]==hashes
