import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


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
