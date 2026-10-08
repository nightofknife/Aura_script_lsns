"""A current changed ROI uses the same strict native decoder, never a quota guess."""
from copy import deepcopy
import hashlib

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_hud_invariant as hud


def source(generation):
    return dict(session_id=7,generation=generation,frame_time=10.+generation*.1,capture_backend='wgc')


def values():
    return dict(plane_index=1,rounds_remaining=6,moves_used=0,moves_total=1,
        rotations_used=1,rotations_total=1,collected_count=0,inspiration_total=2,status='complete')


def board():
    return dict(valid=True,scene='board',player_turn=True,plane_evidence=dict(plane_index=1,status='recognized'))


@pytest.fixture
def ready_guard():
    rgb=np.zeros((720,1280,3),np.uint8);guard=hud.HudInvariant()
    for generation in (1,2):
        native=dict(values(),_source=source(generation),_rgb_digest=hashlib.sha256(rgb.tobytes()).hexdigest())
        reply=guard.add_baseline(rgb,source(generation),board(),native,now=10.+generation*.1+.01)
    assert reply['ready']
    return guard,rgb


def changed(rgb,*regions):
    current=rgb.copy()
    for name in regions:
        x,y,_,_=hud.REGIONS[name];current[y,x,0]=1
    return current


def test_only_changed_pair_invokes_original_decoder_and_binds_all_fields(ready_guard,monkeypatch):
    guard,rgb=ready_guard;current=changed(rgb,'moves');calls=[]
    monkeypatch.setattr(hud.vision,'read_hud',lambda *a,**k:pytest.fail('full HUD decoded'))
    monkeypatch.setattr(hud.vision,'read_rounds_template',lambda *a:pytest.fail('unchanged rounds decoded'))
    def pair(image,name):
        calls.append((image is current,name))
        return (0,1),dict(status='recognized',text='0/1',confidence=.9)
    monkeypatch.setattr(hud.templates,'_read_pair',pair)
    reply=guard.check(current,source(3),board(),now=10.35)
    assert reply['ready'] and calls==[(True,'moves')]
    assert reply['proof']['native_regions_decoded']==['moves']
    assert set(reply['field_sources'])==set(hud.FIELDS)
    for name,fields in hud.REGION_FIELDS.items():
        for field in fields:
            assert reply['field_sources'][field]['current_source']==source(3)
            assert reply['field_sources'][field]['source']==('current_native_roi_read' if name=='moves' else 'exact_current_roi')
    assert reply['proof']['source_rgb_digest']==hashlib.sha256(current.tobytes()).hexdigest()


def test_changed_rounds_uses_original_template_component(ready_guard,monkeypatch):
    guard,rgb=ready_guard;current=changed(rgb,'rounds');calls=[]
    monkeypatch.setattr(hud.templates,'_read_pair',lambda *a:pytest.fail('unchanged pair decoded'))
    monkeypatch.setattr(hud.vision,'read_rounds_template',lambda image:calls.append(image is current) or dict(value=6,status='recognized'))
    reply=guard.check(current,source(3),board(),now=10.35)
    assert reply['ready'] and calls==[True]
    assert reply['proof']['native_regions_decoded']==['rounds']


def test_changed_plane_requires_same_scene_actual_native_plane_value(ready_guard):
    guard,rgb=ready_guard;current=changed(rgb,'plane')
    assert guard.check(current,source(3),dict(valid=True,scene='board',player_turn=True),now=10.35)['reason']=='hud_invariant_changed_roi_unknown:plane'


@pytest.mark.parametrize('pair', [None,(None,1),(0,None),(False,1),(0,2),(2,1),(0,0)])
def test_unknown_noninteger_or_changed_pair_never_borrows_baseline(ready_guard,monkeypatch,pair):
    guard,rgb=ready_guard;current=changed(rgb,'moves')
    monkeypatch.setattr(hud.templates,'_read_pair',lambda *a:(pair,dict(status='unknown')))
    reply=guard.check(current,source(3),board(),now=10.35)
    assert not reply['ready'] and not reply['unchanged']


def test_real_numeric_decoder_rejects_unreadable_changed_roi(ready_guard):
    guard,rgb=ready_guard
    reply=guard.check(changed(rgb,'moves'),source(3),board(),now=10.35)
    assert not reply['ready'] and reply['reason']=='hud_invariant_changed_roi_unknown:moves'


def test_changed_roi_native_compute_cannot_expire_source(ready_guard,monkeypatch):
    guard,rgb=ready_guard;current=changed(rgb,'moves');clock=[10.35]
    monkeypatch.setattr(hud.time,'monotonic',lambda:clock[0])
    def slow(*args):
        clock[0]=10.801
        return (0,1),dict(status='recognized')
    monkeypatch.setattr(hud.templates,'_read_pair',slow)
    reply=guard.check(current,source(3),board())
    assert not reply['ready'] and reply['reason']=='hud_invariant_source_stale'


def test_same_current_generation_cannot_be_checked_twice(ready_guard,monkeypatch):
    guard,rgb=ready_guard;current=changed(rgb,'moves')
    monkeypatch.setattr(hud.templates,'_read_pair',lambda *a:((0,1),dict(status='recognized')))
    assert guard.check(current,source(3),board(),now=10.35)['ready']
    assert guard.check(current,source(3),board(),now=10.35)['reason']=='hud_invariant_source_not_advanced'
