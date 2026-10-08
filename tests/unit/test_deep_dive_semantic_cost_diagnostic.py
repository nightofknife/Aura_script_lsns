"""Cost diagnostics never participate in source-age or classification gates."""
from copy import deepcopy
import itertools

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


@pytest.fixture
def case(monkeypatch):
    scanner=vision.LayoutScanner(target_negative_evidence=False)
    monkeypatch.setattr(vision.time, 'monotonic', lambda:100.)
    scanner.glyph_anchor_at=100.
    scanner.glyph_anchor_reason='fixture'
    scanner.refine_diagnostic={'renewed':True,'reason':'fixture'}
    calls=[]
    monkeypatch.setattr(scanner, '_detect_targets', lambda rgb:calls.append('detect') or [])
    monkeypatch.setattr(scanner, '_target_observation', lambda target:calls.append('supplied_targets') or [])
    monkeypatch.setattr(scanner, '_refine_centres', lambda *args:calls.append('refine'))
    monkeypatch.setattr(scanner, '_atlas_alignment', lambda *args,**kwargs:calls.append('atlas') or True)
    monkeypatch.setattr(scanner, 'visible', lambda:[])
    monkeypatch.setattr(scanner, '_observe_cells', lambda *args:calls.append('observe'))
    monkeypatch.setattr(scanner, 'target_mask_observation', lambda:[])
    result=scanner.result()
    monkeypatch.setattr(scanner, 'result', lambda:calls.append('result') or deepcopy(result))
    pose=dict(rvec=np.zeros((3,1)),tvec=np.array([[0.],[0.],[42.]]),
        rotation=np.eye(3),quality=.9,map_revision=0,correction_epoch=0)
    return scanner,np.zeros((720,1280,3),np.uint8),pose,calls


def test_completed_stages_have_durations_and_total_without_changing_result(case,monkeypatch):
    scanner,rgb,pose,calls=case
    clock=itertools.count(0.,.01)
    monkeypatch.setattr(vision.time,'perf_counter',lambda:next(clock))
    observation=scanner.semantic_view(rgb,7,pose,source_frame_time=99.9)
    costs=observation['semantic_cost_sec']
    assert set(costs)=={'detect','refine','atlas_alignment','associate_observe','result','observation','total'}
    assert all(value==pytest.approx(.01) for key,value in costs.items() if key!='total')
    assert costs['total']>=sum(value for key,value in costs.items() if key!='total')
    assert calls==['detect','refine','atlas','observe','result']
    assert observation['tracking_ok'] is True
    assert observation['glyph_anchor_age_sec']==0.
    assert scanner.last_observation is observation
    assert 'semantic_cost_sec' not in scanner.last_fused_result
    assert not any(scanner.evidence)


def test_supplied_targets_record_preparation_in_detect_stage(case):
    scanner,rgb,pose,calls=case
    observation=scanner.semantic_view(rgb,7,pose,targets=[],source_frame_time=99.9)
    assert calls[0]=='supplied_targets'
    assert observation['semantic_cost_sec']['detect']>=0.


@pytest.mark.parametrize('early',('atlas','anchor'))
def test_early_returns_record_only_completed_stages(case,monkeypatch,early):
    scanner,rgb,pose,calls=case
    if early=='atlas':
        monkeypatch.setattr(scanner,'_atlas_alignment',lambda *args,**kwargs:False)
        scanner.last_error='fixture_reject'
    else:
        scanner.glyph_anchor_at=90.
    observation=scanner.semantic_view(rgb,7,pose,source_frame_time=99.9)
    assert set(observation['semantic_cost_sec'])=={'detect','refine','atlas_alignment','total'}
    assert observation['tracking_ok'] is False
    assert 'observe' not in calls and 'result' not in calls
    assert scanner.last_fused_result is None
    assert scanner.last_observation is observation


def test_paused_fusion_does_not_fabricate_observe_stage(case):
    scanner,rgb,pose,calls=case
    scanner.glyph_anchor_at=97.
    observation=scanner.semantic_view(rgb,7,pose,source_frame_time=99.9)
    assert observation['fusion_paused'] is True
    assert 'associate_observe' not in observation['semantic_cost_sec']
    assert 'result' in observation['semantic_cost_sec']
    assert 'observe' not in calls


def test_invalid_frame_has_total_only_and_preserves_existing_scanner_state(case):
    scanner,rgb,pose,calls=case
    previous=scanner.last_observation
    observation=scanner.semantic_view(rgb[:1],7,pose)
    assert observation['reason']=='invalid_frame'
    assert set(observation['semantic_cost_sec'])=={'total'}
    assert scanner.last_observation is previous
    assert not calls
