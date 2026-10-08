"""Reuse must stay inside a frozen frame and preserve all overlay gates."""
import numpy as np

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as layout
from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as page


def test_same_frame_reuses_icon_but_new_frame_and_corrected_quad_do_not(monkeypatch):
    calls=[]
    def classify(crop):
        calls.append(crop.copy())
        return dict(icon_id='blue_scales',confidence=.8)
    monkeypatch.setattr(layout,'classify_icon',classify)
    scanner=layout.LayoutScanner()
    image=np.zeros((720,1280,3),np.uint8)
    quad=np.array([[450.,200.],[550.,200.],[550.,300.],[450.,300.]])
    scanner._begin_frame()
    first=scanner._read_icon(image,quad)
    assert scanner._read_icon(image,quad.copy()) is first
    assert len(calls)==1
    scanner._read_icon(image,quad+1.)
    assert len(calls)==2
    image[200:300,450:550]=255
    scanner._begin_frame()
    scanner._read_icon(image,quad)
    assert len(calls)==3
    assert not np.array_equal(calls[0],calls[-1])


def test_target_association_cache_invalidates_when_pose_changes(monkeypatch):
    scanner=layout.LayoutScanner();calls=[]
    monkeypatch.setattr(scanner,'_associate_targets',lambda targets,visible:(calls.append(1) or ({},set())))
    targets=[];visible=[dict(index=0)]
    scanner._associated_targets(targets,visible)
    scanner._associated_targets(targets,visible)
    assert len(calls)==1
    scanner.tvec=scanner.tvec.copy();scanner.tvec[0]+=1.
    scanner._associated_targets(targets,visible)
    assert len(calls)==2
    scanner._begin_frame();scanner._associated_targets(targets,visible)
    assert len(calls)==3


def test_constant_grid_cache_cannot_be_mutated():
    point=layout._point(dict(face='U',row=1,col=1))
    assert not point.flags.writeable
    assert layout._point(dict(face='U',row=1,col=1)) is point


def test_page_probes_are_lazy_and_skipped_scores_are_explicit(monkeypatch):
    calls=[]
    monkeypatch.setattr(page,'_match',lambda gray,name:(calls.append(name) or (.2,[1,2])))
    result={};probes=page._FrameProbes(np.zeros((720,1280),np.uint8),result)
    assert not calls
    assert probes['rest_title']==(.2,[1,2])
    assert probes['rest_title']==(.2,[1,2])
    assert calls==['rest_title']
    assert result['planned_scores']=={'rest_title':.2}
    assert 'rest_next' in result['planned_probes_not_evaluated']
    assert 'rest_title' not in result['planned_probes_not_evaluated']
