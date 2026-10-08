from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_semantic_explanations as semantic
from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision
from plans.resonance_pc.src.actions import _deep_dive_hud_invariant as invariant
from plans.resonance_pc.src.actions._deep_dive_hud_invariant import REGIONS, FIELDS


def full_hud(**changes):
    return dict(dict(status='complete', plane_index=1, rounds_remaining=6,
        moves_used=0, moves_total=1, rotations_used=1, rotations_total=1,
        collected_count=0, inspiration_total=2), **changes)


@pytest.fixture
def case(monkeypatch):
    path = Path(__file__).parents[1]/'fixtures/deep_dive_anchor/established_backface1083.json'
    data = json.loads(path.read_text('utf8'))
    source = data['source']
    clock = [source['frame_time']+.1]
    monkeypatch.setattr(semantic.time, 'monotonic', lambda: clock[0])
    reads = []
    def native(rgb, ocr, *, observation):
        assert ocr is None
        reads.append((rgb, observation))
        return full_hud()
    monkeypatch.setattr(semantic.vision, 'read_hud', native)
    def local_native(rgb,observation,regions):
        parsed=semantic.vision.read_hud(rgb,None,observation=observation)
        return dict({field:parsed.get(field) for name in regions for field in invariant.REGION_FIELDS[name]},
            evidence_source={},decoded_regions=sorted(regions),region_evidence={})
    monkeypatch.setattr(invariant,'_read_changed_native',local_native)
    scene = dict(valid=True, scene='board', player_turn=True,
                 enemy_turn=False, unknown_modal_evidence=False)
    packet = dict(source, image=np.zeros((720,1280,3),np.uint8), scene_observation=scene)
    associations = []
    front = {int(k):v for k,v in data['front_assigned'].items()}
    for target in data['targets']:
        candidate = deepcopy(target)
        candidate['cell_index'] = next((i for i,t in front.items()
            if (t['kind'],t['point'],t['box']) == (target['kind'],target['point'],target['box'])), None)
        associations.append(candidate)
    layout = dict(cells=deepcopy(data['cells']), map_revision=source['map_revision'],
        semantic_source=deepcopy(source), target_clues=deepcopy(data['targets']),
        target_candidate_associations=associations,
        player_cell={'face':'U','row':1,'col':1}, target_marker='unchanged')
    observation = dict(frame_id=source['frame_id'],pose=deepcopy(data['pose']),
        refine_diagnostic=deepcopy(data['anchor_proof']),
        entity_association_diagnostic=deepcopy(data['anchor_proof']),
        target_coverage=deepcopy(data['model_coverage']))
    return packet, layout, observation, clock, reads


def initialize(helper, packet):
    for delta in (2,1):
        earlier = dict(packet, frame_id=packet['frame_id']-delta,
            generation=packet['generation']-delta, frame_time=packet['frame_time']-.1*delta)
        assert not helper.prepare(earlier)['ready']
    return helper.prepare(packet)


def test_real_backface_geometry_receives_actual_same_packet_hud_proof(case):
    packet, layout, observation, clock, reads = case
    helper = semantic.SemanticTargetExplainer()
    assert initialize(helper, packet)['ready']
    assert len(reads) == 2  # Current identical ROIs carry their own pixel proof.
    before = deepcopy(layout)
    output = helper.apply(layout, observation, packet)
    assert output['status'] == 'applied'
    assert output['layout']['target_candidate_associations'][1]['cell_index'] == 31
    assert layout == before
    for key in layout:
        if key != 'target_candidate_associations': assert output['layout'][key] == before[key]
    assert output['layout']['target_candidate_associations'][0] == before['target_candidate_associations'][0]
    audit = output['diagnostics']['explanation']['audit']
    hud = audit['hud_proof']
    assert hud['hud_guard']['proof']['all_fields'] == list(FIELDS)
    assert len(hud['hud_guard']['field_sources']) == 8
    assert hud['source_generation'] == packet['generation']
    assert hud['source_session_id'] == packet['session_id']
    assert hud['source_frame_id'] == packet['frame_id']
    assert output['positive_vote'] is False and output['input_authorized'] is False


@pytest.mark.parametrize('change', ['stale', 'backend', 'missing_backend', 'model_cached', 'anchor_frame',
    'refine_map', 'not_renewed', 'packet_generation', 'result_generation', 'rgb_changed', 'missing_guard'])
def test_failed_current_source_proof_preserves_original_result(case, change):
    packet, layout, observation, clock, reads = case
    helper = semantic.SemanticTargetExplainer()
    assert initialize(helper, packet)['ready']
    if change == 'stale': clock[0] = packet['frame_time']+.801
    elif change == 'backend': packet['capture_backend']='printwindow'
    elif change == 'missing_backend': packet.pop('capture_backend')
    elif change == 'model_cached': observation['target_coverage']['model_executed']=False
    elif change == 'anchor_frame': observation['entity_association_diagnostic']['source_frame_id']-=1
    elif change == 'refine_map': observation['refine_diagnostic']['source_map_revision']+=1
    elif change == 'not_renewed': observation['refine_diagnostic']['renewed']=False
    elif change == 'packet_generation': packet['generation']+=1
    elif change == 'result_generation': layout['semantic_source']['generation']+=1
    elif change == 'rgb_changed': packet['image'][300,500]=[2,3,4]
    elif change == 'missing_guard': helper=semantic.SemanticTargetExplainer()
    before=deepcopy(layout)
    output=helper.apply(layout,observation,packet)
    assert output['status']=='rejected'
    assert output['layout'] is layout and layout==before
    assert output['diagnostics']['reason']


def test_prepare_changed_roi_uses_actual_native_read_and_rejects_changed_hud(case,monkeypatch):
    packet,layout,observation,clock,reads=case
    helper=semantic.SemanticTargetExplainer()
    assert initialize(helper,packet)['ready']
    packet=dict(packet,generation=packet['generation']+1,frame_id=packet['frame_id']+1,
                frame_time=packet['frame_time']+.02,image=packet['image'].copy())
    x,y,_,_=REGIONS['moves'];packet['image'][y,x]=[255,255,255]
    calls=[]
    def native(rgb,ocr,*,observation):
        calls.append(rgb);assert ocr is None
        return full_hud(moves_used=1)
    monkeypatch.setattr(semantic.vision,'read_hud',native)
    prep=helper.prepare(packet)
    assert len(calls)==1 and not prep['ready']
    assert prep['reason']=='hud_invariant_state_changed'


def test_changed_roi_same_native_values_proves_current_source_once(case):
    packet,layout,observation,clock,reads=case
    helper=semantic.SemanticTargetExplainer();assert initialize(helper,packet)['ready']
    packet=dict(packet,generation=packet['generation']+1,frame_id=packet['frame_id']+1,
                frame_time=packet['frame_time']+.02,image=packet['image'].copy())
    x,y,_,_=REGIONS['moves'];packet['image'][y,x]=[255,255,255]
    prepared=helper.prepare(packet)
    assert prepared['ready'] and len(reads)==3
    guarded=prepared['hud_guard']
    assert guarded['field_sources']['moves_used']['source']=='current_native_roi_read'
    assert guarded['proof']['current_source']['generation']==packet['generation']
    # A new unchanged packet advances normally; no second check consumed the
    # changed packet's generation or substituted a newer identity for it.
    following=dict(packet,generation=packet['generation']+1,frame_id=packet['frame_id']+1,
                   frame_time=packet['frame_time']+.01)
    assert helper.prepare(following)['ready']
    assert len(reads)==3


def test_source_changes_require_two_new_baseline_reads(case):
    packet,layout,observation,clock,reads=case
    helper=semantic.SemanticTargetExplainer()
    assert initialize(helper,packet)['ready']
    changed=dict(packet,session_id='new_actual_session',generation=1,frame_id=1)
    assert not helper.prepare(changed)['ready']
    assert len(reads)==3
    changed=dict(changed,generation=2,frame_id=2,frame_time=changed['frame_time']+.01)
    assert not helper.prepare(changed)['ready']
    changed=dict(changed,generation=3,frame_id=3,frame_time=changed['frame_time']+.01)
    assert helper.prepare(changed)['ready']
    assert len(reads)==4


def test_prepare_must_remain_fresh_after_real_native_read(case,monkeypatch):
    packet,layout,observation,clock,reads=case
    def slow(rgb,ocr,*,observation):
        clock[0]=packet['frame_time']+.501
        return full_hud()
    monkeypatch.setattr(semantic.vision,'read_hud',slow)
    prep=semantic.SemanticTargetExplainer().prepare(packet)
    assert not prep['ready'] and prep['reason']=='hud_invariant_source_stale'


def test_native_failure_is_diagnostic_without_guard_or_model_fallback(case,monkeypatch):
    packet,layout,observation,clock,reads=case
    def failed(*args,**kwargs): raise RuntimeError('native_hud_failed')
    monkeypatch.setattr(semantic.vision,'read_hud',failed)
    helper=semantic.SemanticTargetExplainer()
    assert helper.prepare(packet)==dict(ready=False,reason='native_hud_failed')
    assert helper.apply(layout,observation,packet)['layout'] is layout


def test_actual_time_spent_explaining_cannot_expire_a_returned_match(case,monkeypatch):
    packet,layout,observation,clock,reads=case
    helper=semantic.SemanticTargetExplainer();assert initialize(helper,packet)['ready']
    original=semantic.apply_current_explanations
    def slow(result,explanation):
        returned=original(result,explanation);clock[0]=packet['frame_time']+.801
        return returned
    monkeypatch.setattr(semantic,'apply_current_explanations',slow)
    output=helper.apply(layout,observation,packet)
    assert output['status']=='rejected' and output['layout'] is layout
    assert output['diagnostics']['reason']=='semantic_explanation_source_expired_during_apply'


def test_fully_front_associated_result_does_not_run_c_or_clone(case,monkeypatch):
    packet,layout,observation,clock,reads=case
    for candidate in layout['target_candidate_associations']:
        candidate['cell_index']=31 if candidate['kind']=='singularity' else 26
    monkeypatch.setattr(semantic,'explain_established_targets',lambda *a,**k:pytest.fail('unexpected C'))
    output=semantic.SemanticTargetExplainer().apply(layout,observation,packet)
    assert output['status']=='skipped' and output['layout'] is layout


def test_annotate_explicit_fused_result_matches_default_rgb_without_refusion(monkeypatch):
    scanner=vision.LayoutScanner();scanner.last_projected=[dict(index=4,
        quad=np.array([[410.,210.],[450.,210.],[450.,250.],[410.,250.]]),centre=np.array([430.,230.]))]
    scanner.last_targets=[dict(kind='player',box=[420,215,20,20],point=[430.,225.],confidence=.9)]
    monkeypatch.setattr(scanner,'_associated_targets',lambda *a:({},set()))
    rgb=np.zeros((720,1280,3),np.uint8);before=rgb.copy();result=scanner.result()
    legacy=scanner.annotate(rgb)
    monkeypatch.setattr(scanner,'result',lambda:pytest.fail('second fusion'))
    explicit=scanner.annotate(rgb,fused_result=result)
    np.testing.assert_array_equal(explicit,legacy)
    np.testing.assert_array_equal(rgb,before)


def test_default_annotate_never_implicitly_reuses_previous_fused_cache(monkeypatch):
    scanner=vision.LayoutScanner();original=scanner.result;calls=[]
    def fresh(): calls.append(1);return original()
    monkeypatch.setattr(scanner,'result',fresh)
    scanner.last_fused_result={'invalid_stale_cache':True}
    scanner.annotate(np.zeros((720,1280,3),np.uint8))
    scanner.annotate(np.full((720,1280,3),17,np.uint8))
    assert len(calls)==2
