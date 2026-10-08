"""Exact-HUD same-capture contract; synthetic frames never claim live proof."""
from copy import deepcopy
import hashlib
import numpy as np
import pytest
from plans.resonance_pc.src.actions import _deep_dive_hud_invariant as invariant


def source(generation):
    return dict(session_id=7,generation=generation,frame_time=10.+generation*.1,capture_backend='wgc')


def values():
    return dict(plane_index=1,rounds_remaining=6,moves_used=0,moves_total=1,
                rotations_used=1,rotations_total=1,collected_count=0,inspiration_total=2,status='complete')


def bound(rgb,generation,*,hud=None):
    return dict(hud or values(),_source=source(generation),_rgb_digest=hashlib.sha256(rgb.tobytes()).hexdigest())


def board():return dict(valid=True,scene='board',player_turn=True)


def local_values(values, regions):
    return dict({field:values.get(field) for name in regions for field in invariant.REGION_FIELDS[name]},
                evidence_source={},decoded_regions=sorted(regions),region_evidence={})


@pytest.fixture
def setup_guard():
    rgb=np.zeros((720,1280,3),np.uint8);guard=invariant.HudInvariant()
    first=guard.add_baseline(rgb,source(1),board(),bound(rgb,1),map_revision=3,now=10.15)
    assert first['ready'] is False and first['baseline_reads']==1
    second=guard.add_baseline(rgb,source(2),board(),bound(rgb,2),map_revision=3,now=10.25)
    assert second['ready'] is True and second['input_authorized'] is False
    return guard,rgb


def test_two_actual_distinct_bound_sources_required_and_duplicate_clears_initialization():
    rgb=np.zeros((720,1280,3),np.uint8);guard=invariant.HudInvariant()
    guard.add_baseline(rgb,source(1),board(),bound(rgb,1),now=10.15)
    duplicate=guard.add_baseline(rgb,source(1),board(),bound(rgb,1),now=10.15)
    assert not duplicate['ready'] and duplicate['reason']=='hud_invariant_source_not_advanced'
    assert guard.check(rgb,source(2),board(),now=10.25)['reason']=='hud_invariant_baseline_not_ready'


def test_initializer_requires_matching_hud_rgb_source_and_complete_values():
    rgb=np.zeros((720,1280,3),np.uint8)
    for faulty in (dict(values()),dict(bound(rgb,1),_rgb_digest='old'),dict(bound(rgb,1),_source=source(2)),
                   dict(bound(rgb,1),status='partial'),dict(bound(rgb,1),moves_total=2),
                   dict(bound(rgb,1),rounds_remaining=True)):
        assert not invariant.HudInvariant().add_baseline(rgb,source(1),board(),faulty,now=10.15)['ready']


def test_exact_rois_reuse_current_source_proof_without_read_or_ocr(setup_guard,monkeypatch):
    guard,rgb=setup_guard
    monkeypatch.setattr(invariant.vision,'read_hud',lambda *_,**__:pytest.fail('native executed on identical ROI'))
    current=rgb.copy();current[300,500]=[100,20,33] # unrelated moving cube pixels
    result=guard.check(current,source(3),board(),map_revision=3,now=10.35)
    assert result['ready'] and result['unchanged'] and result['input_authorized'] is False
    assert result['proof']['current_source']==source(3)
    assert result['proof']['source_rgb_digest']==hashlib.sha256(current.tobytes()).hexdigest()
    assert result['proof']['spatial_tolerance']==0
    assert set(result['field_sources'])==set(invariant.FIELDS)
    assert all(r['source']=='exact_current_roi' and r['current_source']==source(3) for r in result['field_sources'].values())
    result['hud']['moves_used']=1
    assert guard.check(current,source(4),board(),map_revision=3,now=10.45)['hud']['moves_used']==0


@pytest.mark.parametrize('name',invariant.REGIONS)
def test_one_new_roi_pixel_requires_local_native_same_rgb(setup_guard,monkeypatch,name):
    guard,rgb=setup_guard;x1,y1,_,_=invariant.REGIONS[name]
    current=rgb.copy();current[y1,x1,0]=1;calls=[]
    def native(image,observation,regions):
        calls.append((image is current,regions,observation));return local_values(values(),regions)
    monkeypatch.setattr(invariant,'_read_changed_native',native)
    result=guard.check(current,source(3),board(),map_revision=3,now=10.35)
    assert result['unchanged'] and calls==[(True,{name},board())]
    assert result['proof']['roi_equal'][name] is False
    for field in invariant.REGION_FIELDS[name]:assert result['field_sources'][field]['source']=='current_native_roi_read'


@pytest.mark.parametrize('field,new', [('plane_index',2),('rounds_remaining',5),('moves_used',1),
    ('rotations_used',0),('collected_count',1),('inspiration_total',3)])
def test_every_real_state_field_change_rejects(setup_guard,monkeypatch,field,new):
    guard,rgb=setup_guard;current=rgb.copy()
    name=next(name for name,fields in invariant.REGION_FIELDS.items() if field in fields)
    x,y,_,_=invariant.REGIONS[name];current[y,x]=255
    monkeypatch.setattr(invariant,'_read_changed_native',lambda image,observation,regions:local_values(dict(values(),**{field:new}),regions))
    result=guard.check(current,source(3),board(),map_revision=3,now=10.35)
    assert not result['unchanged'] and result['reason']=='hud_invariant_state_changed'


def test_unchanged_yellow_quota_uses_exact_current_pixels_when_other_roi_changes(setup_guard,monkeypatch):
    guard,rgb=setup_guard;current=rgb.copy();current[400,1200]=255
    monkeypatch.setattr(invariant,'_read_changed_native',lambda image,observation,regions:local_values(dict(values(),status='partial',rotations_used=None),regions))
    result=guard.check(current,source(3),board(),map_revision=3,now=10.35)
    assert result['unchanged'] and result['hud']['rotations_used']==1
    assert result['field_sources']['rotations_used']['source']=='exact_current_roi'
    assert result['field_sources']['moves_used']['source']=='current_native_roi_read'


def test_changed_yellow_quota_unknown_does_not_borrow_baseline(setup_guard,monkeypatch):
    guard,rgb=setup_guard;current=rgb.copy();current[480,1200,0]=1
    monkeypatch.setattr(invariant,'_read_changed_native',lambda image,observation,regions:local_values(dict(values(),status='partial',rotations_used=None),regions))
    result=guard.check(current,source(3),board(),map_revision=3,now=10.35)
    assert not result['unchanged'] and result['reason']=='hud_invariant_changed_roi_unknown:rotations'


def test_unchanged_roi_is_not_redecoded_and_keeps_current_exact_proof(setup_guard,monkeypatch):
    guard,rgb=setup_guard;current=rgb.copy();current[400,1200,0]=1
    calls=[]
    def native(image,observation,regions):
        calls.append(regions)
        return local_values(dict(values(),rotations_used=0),regions)
    monkeypatch.setattr(invariant,'_read_changed_native',native)
    result=guard.check(current,source(3),board(),map_revision=3,now=10.35)
    assert result['unchanged'] and calls==[{'moves'}]
    assert result['hud']['rotations_used']==1
    assert result['field_sources']['rotations_used']['source']=='exact_current_roi'
    assert result['field_sources']['rotations_used']['current_source']==source(3)


def test_two_native_fields_of_changed_pair_are_both_required(setup_guard,monkeypatch):
    guard,rgb=setup_guard;current=rgb.copy();current[270,1240,0]=1
    monkeypatch.setattr(invariant,'_read_changed_native',lambda image,observation,regions:local_values(dict(values(),status='partial',inspiration_total=None),regions))
    assert not guard.check(current,source(3),board(),map_revision=3,now=10.35)['unchanged']


@pytest.mark.parametrize('bad', [dict(valid=False,scene='board',player_turn=True),
    dict(valid=True,scene='choose_rotate',player_turn=True),dict(valid=True,scene='board',player_turn=False),
    dict(valid=True,scene='board',player_turn=True,enemy_turn=True),
    dict(valid=True,scene='board',player_turn=True,unknown_modal_evidence=True)])
def test_overlay_or_non_player_board_never_reuses_even_exact_pixels(setup_guard,bad):
    guard,rgb=setup_guard
    assert not guard.check(rgb,source(3),bad,map_revision=3,now=10.35)['unchanged']


@pytest.mark.parametrize('src,revision,now', [(source(2),3,10.35),
    (dict(source(3),frame_time=10.2),3,10.35),(dict(source(3),session_id=8),3,10.35),
    (dict(source(3),capture_backend='printwindow'),3,10.35),(source(3),4,10.35),
    (source(3),3,10.81),(source(3),3,10.2)])
def test_session_map_source_advance_and_freshness_are_mandatory(setup_guard,src,revision,now):
    guard,rgb=setup_guard
    assert not guard.check(rgb,src,board(),map_revision=revision,now=now)['unchanged']


def test_state_change_during_two_baseline_reads_needs_new_second_read():
    rgb=np.zeros((720,1280,3),np.uint8);guard=invariant.HudInvariant()
    guard.add_baseline(rgb,source(1),board(),bound(rgb,1),now=10.15)
    changed=dict(values(),rounds_remaining=5)
    result=guard.add_baseline(rgb,source(2),board(),bound(rgb,2,hud=changed),now=10.25)
    assert not result['ready'] and result['baseline_reads']==1
    result=guard.add_baseline(rgb,source(3),board(),bound(rgb,3,hud=changed),now=10.35)
    assert result['ready'] and result['hud']['rounds_remaining']==5


def test_native_exception_and_postcompute_staleness_fail_closed(setup_guard,monkeypatch):
    guard,rgb=setup_guard;current=rgb.copy();current[400,1200]=255
    monkeypatch.setattr(invariant,'_read_changed_native',lambda *_,**__: (_ for _ in ()).throw(RuntimeError('native_failure')))
    assert not guard.check(current,source(3),board(),map_revision=3,now=10.35)['unchanged']
    guard,rgb=setup_guard
    clock=iter([10.45,10.95])
    monkeypatch.setattr(invariant.time,'monotonic',lambda:next(clock))
    assert not guard.check(rgb,source(4),board(),map_revision=3)['unchanged']
