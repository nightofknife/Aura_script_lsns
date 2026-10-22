"""Exact post-fit reuse changes computation only, never source or evidence."""
from copy import deepcopy

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


@pytest.fixture(autouse=True)
def cv_one_thread():
    previous=cv2.getNumThreads()
    cv2.setNumThreads(1)
    try:yield
    finally:cv2.setNumThreads(previous)


def assert_rows_equal(left,right):
    assert len(left)==len(right)
    for a,b in zip(left,right):
        assert a.keys()==b.keys()
        for key in a:
            if isinstance(a[key],np.ndarray):assert np.array_equal(a[key],b[key])
            else:assert a[key]==b[key]


def counting_visible(scanner,monkeypatch):
    original=scanner.visible;calls=[]
    def counted(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(scanner,'visible',counted)
    return calls


def test_reused_rows_are_pixel_exact_and_private(monkeypatch):
    scanner=vision.LayoutScanner();calls=counting_visible(scanner,monkeypatch)
    image=np.zeros((720,1280,3),np.uint8)
    first=scanner._postfit_visible(image,retain=True)
    expected=deepcopy(first)
    first[0]['quad'][:]=0;first[0]['cosine']=0;first.pop()
    second=scanner._postfit_visible(image)
    assert_rows_equal(second,expected)
    second[0]['centre'][:]=0;second[0]['area']=0;second.clear()
    assert_rows_equal(scanner._postfit_visible(image),expected)
    assert len(calls)==1


@pytest.mark.parametrize('change',('image','rotation','rvec','translation','map',
                                  'source_frame','source_time','source_map','function'))
def test_changed_dependency_recomputes_and_invalidates(monkeypatch,change):
    scanner=vision.LayoutScanner();calls=counting_visible(scanner,monkeypatch)
    image=np.zeros((720,1280,3),np.uint8)
    scanner._anchor_source=dict(source_frame_id=1,source_frame_time=100.,source_map_revision=0)
    scanner._postfit_visible(image,retain=True)
    if change=='image':image=image.copy()
    elif change=='rotation':scanner.rotation=scanner.rotation.copy();scanner.rotation[0,0]+=.001
    elif change=='rvec':scanner.rvec=scanner.rvec.copy();scanner.rvec[0,0]+=.001
    elif change=='translation':scanner.tvec=scanner.tvec.copy();scanner.tvec[0,0]+=.001
    elif change=='map':scanner.identity_corrections+=1
    elif change.startswith('source_'):
        key={'source_frame':'source_frame_id','source_time':'source_frame_time',
             'source_map':'source_map_revision'}[change]
        scanner._anchor_source[key]+=1
    else:
        original=scanner.visible
        monkeypatch.setattr(scanner,'visible',lambda:original())
    actual=scanner._postfit_visible(image)
    assert len(calls)==2
    assert scanner._postfit_visibility is None
    assert_rows_equal(actual,scanner.visible())


def test_new_frame_and_ordinary_calls_never_retain(monkeypatch):
    scanner=vision.LayoutScanner();calls=counting_visible(scanner,monkeypatch)
    image=np.zeros((720,1280,3),np.uint8)
    scanner._postfit_visible(image);scanner._postfit_visible(image)
    assert len(calls)==2 and scanner._postfit_visibility is None
    scanner._postfit_visible(image,retain=True)
    scanner._begin_frame()
    scanner._postfit_visible(image)
    assert len(calls)==4 and scanner._postfit_visibility is None


def test_geometry_tracking_never_consumes_semantic_reuse(monkeypatch):
    scanner=vision.LayoutScanner();scanner.ready=True;scanner.quality=.97
    image=np.zeros((720,1280,3),np.uint8)
    calls=counting_visible(scanner,monkeypatch)
    scanner._postfit_visible(image,retain=True)
    monkeypatch.setattr(scanner,'_track',lambda gray:True)
    monkeypatch.setattr(scanner,'_seed_features',lambda *args,**kwargs:None)
    result=scanner.track_frame(image,2,mask_targets=[])
    assert result['tracking_ok']
    assert len(calls)==2


def prepared_semantic(monkeypatch):
    scanner=vision.LayoutScanner(target_detector=lambda image:pytest.fail('no detector may execute'))
    image=np.zeros((720,1280,3),np.uint8)
    monkeypatch.setattr(vision.time,'monotonic',lambda:100.1)
    scanner.glyph_anchor_at=100.
    scanner.refine_diagnostic=dict(renewed=False,reason='fixture')
    monkeypatch.setattr(scanner,'_refine_centres',lambda *args:None)
    return scanner,image,scanner.pose_snapshot()


@pytest.mark.parametrize('early',('atlas','age','invalid'))
def test_every_early_return_clears_reuse(monkeypatch,early):
    scanner,image,pose=prepared_semantic(monkeypatch)
    if early=='atlas':monkeypatch.setattr(scanner,'_atlas_alignment',lambda *args,**kwargs:False)
    if early=='age':scanner.glyph_anchor_at=90.
    if early=='invalid':
        scanner._postfit_visible(image,retain=True)
        image=image[:1]
    scanner.semantic_view(image,1,pose,targets=[],source_frame_time=100.)
    assert scanner._postfit_visibility is None


def test_replaced_atlas_pose_change_recomputes_before_observing(monkeypatch):
    scanner,image,pose=prepared_semantic(monkeypatch)
    calls=counting_visible(scanner,monkeypatch);seen=[]
    def atlas(rgb,targets,allow_correction=True):
        assert allow_correction is False
        scanner.tvec=scanner.tvec.copy();scanner.tvec[0,0]+=.1
        return True
    def observe(rgb,frame_id,targets):
        seen.extend(scanner._postfit_visible(rgb))
    monkeypatch.setattr(scanner,'_atlas_alignment',atlas)
    # Existing positional-only replacement remains compatible.
    monkeypatch.setattr(scanner,'_observe_cells',observe)
    scanner.semantic_view(image,1,pose,targets=[],source_frame_time=100.)
    assert len(calls)==2
    assert_rows_equal(seen,scanner.visible())
    assert scanner._postfit_visibility is None


