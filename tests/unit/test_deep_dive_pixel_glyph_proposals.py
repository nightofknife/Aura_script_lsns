"""Research normalized-component contracts; no live recall claims."""
import inspect
import time

import cv2
import numpy as np
import pytest

from research import deep_dive_pixel_glyph_proposals as proposal


@pytest.fixture(autouse=True)
def single_cv_thread():
    previous=cv2.getNumThreads(); cv2.setNumThreads(1)
    yield
    cv2.setNumThreads(previous)


def native(name):
    path=proposal.TEMPLATE_ROOT/('icon_'+name+'.png')
    return cv2.cvtColor(cv2.imdecode(np.fromfile(path,np.uint8),cv2.IMREAD_COLOR),cv2.COLOR_BGR2RGB)


def canvas(name='green_burst', repeated=False):
    rgb=np.zeros((400,400,3),np.uint8)
    tile=native(name)
    h,w=tile.shape[:2]
    rgb[80:80+h,80:80+w]=tile
    if repeated:
        rgb[220:220+h,220:220+w]=tile
    return rgb


def test_all_seven_original_shape_templates_load():
    templates=proposal._templates()
    assert set(templates)==set(proposal.ICONS)
    assert all(templates[name] for name in proposal.ICONS)
    assert all(entry['shape'].shape==(64,64) for entries in templates.values() for entry in entries)
    assert len(proposal.TEMPLATE_GLYPH_ROIS)==9
    assert all({entry['rotation_degrees'] for entry in entries}==set(range(0,360,10))
               for entries in templates.values())


def test_native_green_proposal_preserves_contour_mapping_but_never_tile_or_face():
    outcome=proposal.propose_pixel_glyphs(canvas(),(0,0,400,400))
    green=[p for p in outcome['proposals'] if p['icon_id']=='green_burst']
    assert green
    p=green[0]
    assert p['contours'] and p['shape_score']>=.78 and p['confidence']>=.70
    assert np.isfinite(p['template_to_image']).all()
    assert np.asarray(p['template_to_image']).shape==(3,3)
    assert p['corners'] is None and p['expected_cell'] is None
    assert not outcome['tile_corners_measured'] and not outcome['targets_ready']
    assert not outcome['face_bound'] and not outcome['source_bound']
    assert p['rotation_alternatives'] and p['top_shape_alternatives']
    assert p['template_annotation_source']=='manual_original_template_pixels_only'


def test_repeated_same_glyph_keeps_both_instances():
    outcome=proposal.propose_pixel_glyphs(canvas(repeated=True),(0,0,400,400))
    green=[p for p in outcome['proposals'] if p['icon_id']=='green_burst']
    assert len(green)==2
    assert np.linalg.norm(np.asarray(green[0]['centre'])-green[1]['centre'])>100
    assert any(not p['rotation_unique'] for p in green)


@pytest.mark.parametrize('color',[(200,25,15),(10,240,30),(245,245,245)])
def test_filled_walls_and_seams_do_not_become_glyphs(color):
    rgb=np.zeros((400,400,3),np.uint8)
    cv2.rectangle(rgb,(50,50),(150,150),color,-1)
    cv2.line(rgb,(10,260),(380,260),color,4)
    assert not proposal.propose_pixel_glyphs(rgb,(0,0,400,400))['proposals']


def test_different_shape_cannot_bypass_native_classifier_or_template_veto():
    rgb=np.zeros((400,400,3),np.uint8)
    cv2.rectangle(rgb,(70,70),(140,140),(20,240,30),3)
    outcome=proposal.propose_pixel_glyphs(rgb,(0,0,400,400))
    assert not outcome['proposals']
    assert any(r['reason'] in ('seven_template_shape_veto','native_classification_rejected') for r in outcome['rejected'])
    assert not any(p['ordinary_support'] for p in outcome['geometry_proposals'])


def test_native_colour_positive_keeps_failed_shape_as_review_only():
    rgb=np.zeros((400,400,3),np.uint8)
    cv2.line(rgb,(70,70),(140,140),(20,240,30),3)
    cv2.line(rgb,(70,140),(140,70),(20,240,30),3)
    outcome=proposal.propose_pixel_glyphs(rgb,(0,0,400,400))
    assert not outcome['proposals'] and outcome['geometry_proposals']
    assert all(not p['shape_veto_passed'] and not p['ordinary_support']
               and p['corners'] is None for p in outcome['geometry_proposals'])


def test_shape_threshold_is_veto_and_cannot_relax_original_point78():
    assert proposal.propose_pixel_glyphs(canvas(),(0,0,400,400),shape_threshold=.5)['reason']=='invalid_input'
    parameters=inspect.signature(proposal.propose_pixel_glyphs).parameters
    assert not any(k in parameters for k in ('rotation','translation','quads','face','expected'))


@pytest.mark.parametrize('roi',[None,(0,0,401,400),(1,2),(0,0,True,400)])
def test_bad_roi_fails_closed(roi):
    assert proposal.propose_pixel_glyphs(canvas(),roi)['status']=='rejected'


def test_work_budget_reports_incomplete_not_negative_coverage():
    result=proposal.propose_pixel_glyphs(canvas(repeated=True),(0,0,400,400),max_components=1)
    assert result['incomplete'] and result['reason']=='component_or_time_budget_exhausted'
    assert not result['targets_ready'] and not result['input_ready']


def test_rectification_keeps_all_valid_types_and_rejects_ambiguity(monkeypatch):
    alternatives=[dict(score=.9,rotation_degrees=a,template_path='review.png') for a in (0,10)]
    monkeypatch.setattr(proposal,'_shape_match',lambda *args:dict(alternatives=alternatives))
    responses=iter([dict(icon_id='red_single_eye',confidence=.8),
                    dict(icon_id='orange_triple_eye',confidence=.8)])
    monkeypatch.setattr(proposal.semantics,'classify_icon',lambda rgb:next(responses))
    reading,match,checks,reason=proposal._warm_reading(
        np.zeros((64,64),np.float32),np.zeros((96,96,3),np.uint8),.78,time.perf_counter()+2)
    assert reading is None and match is None and reason=='ambiguous_warm_type'
    assert len(checks)==2
    assert {c['native_reading']['icon_id'] for c in checks}=={'red_single_eye','orange_triple_eye'}


def test_warm_rectification_deadline_never_selects_partial_hypothesis(monkeypatch):
    alternatives=[dict(score=.9,rotation_degrees=0,template_path='review.png')]
    monkeypatch.setattr(proposal,'_shape_match',lambda *args:dict(alternatives=alternatives))
    reading,match,checks,reason=proposal._warm_reading(
        np.zeros((64,64),np.float32),np.zeros((96,96,3),np.uint8),.78,time.perf_counter()-1)
    assert reading is None and match is None and not checks
    assert reason=='warm_preprocessing_budget_exhausted'
