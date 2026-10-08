"""Exact post-fit reuse changes computation only, never source or evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


FIXTURE=Path(__file__).resolve().parents[1]/'fixtures/deep_dive_anchor'


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


def test_real_fixture_full_semantic_and_crop_decisions_match_old_path(monkeypatch,tmp_path):
    data=json.loads((FIXTURE/'oblique_630.json').read_text(encoding='utf8'))
    image=cv2.cvtColor(cv2.imread(str(FIXTURE/'oblique_630.png')),cv2.COLOR_BGR2RGB)
    monkeypatch.setattr(vision.time,'monotonic',lambda:100.1)
    classifier=vision.classify_icon;project=cv2.projectPoints
    classify_crops=[];project_calls=[]
    def classify(crop):
        classify_crops.append(hashlib.sha256(crop.tobytes()).hexdigest())
        return classifier(crop)
    def projecting(*args,**kwargs):
        project_calls.append(1)
        return project(*args,**kwargs)
    monkeypatch.setattr(vision,'classify_icon',classify)
    monkeypatch.setattr(cv2,'projectPoints',projecting)
    outputs=[];measurements=[];elapsed=[]
    for reuse in (False,True):
        scanner=vision.LayoutScanner(target_detector=lambda image:pytest.fail('no model/detector'))
        for key in ('rvec','tvec','rotation','pivot_reference'):
            setattr(scanner,key,np.asarray(data['pose'][key],float))
        scanner.ready=True;scanner.quality=.97
        scanner.icon_anchors={int(k):v for k,v in data['known_icons'].items()}
        calls=counting_visible(scanner,monkeypatch)
        if not reuse:
            monkeypatch.setattr(scanner,'_postfit_visible',lambda image,retain=False:scanner.visible())
        classify_crops.clear();project_calls.clear()
        started=time.perf_counter()
        observation=scanner.semantic_view(image,630,scanner.pose_snapshot(),targets=[],source_frame_time=100.)
        elapsed.append(time.perf_counter()-started)
        observation=deepcopy(observation);observation.pop('semantic_cost_sec')
        # Timing-only diagnostics are the sole excluded observation fields.
        outputs.append((observation,deepcopy(scanner.last_fused_result),deepcopy(scanner.evidence),
                        scanner.rotation.copy(),scanner.rvec.copy(),scanner.tvec.copy(),
                        deepcopy(scanner.last_projected)))
        measurements.append((len(calls),len(project_calls),list(classify_crops)))
        assert scanner._postfit_visibility is None
    old,new=outputs
    serialize=lambda value:json.dumps(value,sort_keys=True,default=lambda item:item.tolist())
    assert serialize(old[:3])==serialize(new[:3])
    for a,b in zip(old[3:6],new[3:6]):assert np.array_equal(a,b)
    assert_rows_equal(old[6],new[6])
    assert old[0]['refine_diagnostic']['renewed']
    assert old[0]['refine_diagnostic']['accepted_confirmed_cell_indices']
    assert measurements[0][0]-measurements[1][0]==2
    assert measurements[0][1]-measurements[1][1]==2
    assert measurements[0][2]==measurements[1][2]
    (tmp_path/'visibility_counts.json').write_text(json.dumps(dict(
        source='tests/fixtures/deep_dive_anchor/oblique_630.png',
        rgb_sha256=hashlib.sha256(image.tobytes()).hexdigest(),
        supplied_targets=[],model_executed=False,cv_threads=1,
        visible_calls=[row[0] for row in measurements],
        projection_calls=[row[1] for row in measurements],
        classifier_calls=[len(row[2]) for row in measurements],
        full_output_equal_except_timing=True,exact_crop_sequence_equal=True,
        single_pair_wall_seconds=elapsed,
        timing_limit='single offline pair, baseline first; no speedup claim'),indent=2),encoding='utf8')
