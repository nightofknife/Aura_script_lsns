"""Matmul uses original NCC near class gates and confidence rounding boundaries."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as s


def original_scores(glyph,whole=False):
    names,templates,norms=s._eye_template_matrix(whole)
    if not len(names):return [(0.,name) for name in s._eye_templates(whole)]
    windows=np.ascontiguousarray(np.lib.stride_tricks.sliding_window_view(
        np.pad(glyph,4),(64,64)).reshape(-1,4096))
    denominator=np.maximum(norms[:,None]*np.sqrt(np.sum(windows*windows,axis=1))[None,:],1e-20)
    correlations=cv2.gemm(templates,windows,1.,None,0.,flags=cv2.GEMM_2_T)/denominator
    return [(float(correlations[names==name].max()) if np.any(names==name) else 0.,name)
            for name in s._eye_templates(whole)]


def injected_scores(monkeypatch,best,second):
    glyph=np.ones((64,64),np.float32)
    names,_,norms=s._eye_template_matrix(False)
    windows=np.ascontiguousarray(np.lib.stride_tricks.sliding_window_view(
        np.pad(glyph,4),(64,64)).reshape(-1,4096))
    den=np.maximum(norms[:,None]*np.sqrt(np.sum(windows*windows,axis=1))[None,:],1e-20)
    numerator=np.where((names==names[0])[:,None],best,second)*den
    calls=[];original=cv2.gemm
    def gemm(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(s.np,'matmul',lambda *args:np.asarray(numerator,np.float32))
    monkeypatch.setattr(s.cv2,'gemm',gemm)
    return glyph,calls


@pytest.mark.parametrize('best,second',[
    (.78,.60),(.78-1e-5,.60),(.78+1e-5,.60),
    (.90,.845),(.90,.845-2e-5),(.90,.845+2e-5),
    (.95,.60),(.95-1e-5,.60),(.95+1e-5,.60),
    (float('nan'),.60),(float('inf'),.60),
])
def test_numerical_boundaries_and_nonfinite_use_original_kernel(monkeypatch,best,second):
    glyph,calls=injected_scores(monkeypatch,best,second)
    actual=s._eye_correlation_scores(glyph)
    assert len(calls)==1
    # The original output is finite and returned exactly; no guessed label.
    expected=original_scores(glyph)
    assert actual==expected
    assert all(np.isfinite(score) for score,_ in actual)


@pytest.mark.parametrize('best,second',[(.90,.60),(.80,.60),(1.02,.60)])
def test_clear_decisions_and_clipped_confidence_keep_fast_kernel(monkeypatch,best,second):
    glyph,calls=injected_scores(monkeypatch,best,second)
    actual=s._eye_correlation_scores(glyph)
    assert not calls
    assert sorted((score for score,_ in actual),reverse=True)==pytest.approx([best,second],abs=2e-7)


@pytest.mark.parametrize('whole',[False,True])
def test_blank_normalized_shape_has_no_evidence(whole):
    assert s._eye_correlation_scores(np.zeros((64,64),np.float32),whole)==original_scores(np.zeros((64,64),np.float32),whole)


def test_actual_and_native_class_confidence_preserved(monkeypatch):
    fixtures=Path(__file__).parents[1]/'fixtures/deep_dive_anchor'
    native=Path(s.__file__).resolve().parents[2]/'templates/deep_dive_layout'
    paths=list(native.glob('icon_*eye*.png'))+list(fixtures.glob('whole_warm*.png'))+list((fixtures/'orange_support_17').glob('orange_D22*.png'))
    rgb_cases=[]
    for path in paths:
        rgb=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
        rgb_cases.extend(np.ascontiguousarray(np.rot90(rgb,k)) for k in range(4))
    for hue in (0,7,15,23,170):
        hsv=np.empty((96,96,3),np.uint8);hsv[:]=[hue,220,230]
        rgb_cases.append(cv2.cvtColor(hsv,cv2.COLOR_HSV2RGB))
    current=s._eye_correlation_scores
    for rgb in rgb_cases:
        expected=None
        with monkeypatch.context() as patch:
            patch.setattr(s,'_eye_correlation_scores',original_scores)
            expected=s.classify_icon(rgb)
        assert s.classify_icon(rgb)==expected
        for whole in (False,True):
            glyph=s._warm_glyph(rgb,whole=whole)
            if glyph is None:continue
            np.testing.assert_allclose([score for score,_ in current(glyph,whole)],
                                       [score for score,_ in original_scores(glyph,whole)],atol=1e-6,rtol=0.)
