from copy import deepcopy

import cv2
import numpy as np
import pytest

from research.deep_dive_local_grip import select_local_grip, official_ui_mask


SOURCE=dict(session_id='wgc-session',generation=10,frame_id=3,frame_time=12.5,map_revision=0,
            capture_backend='wgc')


@pytest.fixture(autouse=True)
def cv_single_thread():
    old=cv2.getNumThreads();cv2.setNumThreads(1)
    yield
    cv2.setNumThreads(old)


def row(quad=((400,200),(500,200),(500,300),(400,300)),face='F',cosine=.25):
    quad=np.asarray(quad,float)
    return dict(face=face,row=1,col=1,quad=quad.tolist(),centre=quad.mean(0).tolist(),
                cosine=cosine,source=deepcopy(SOURCE),
                projection_kind='local_face_geometry_shared_orientation_unknown',
                full_cube_coordinates_valid=False,target_evidence=False)


def pick(rows=None,direction=(1,0),distance=24,**kwargs):
    return select_local_grip([row()] if rows is None else rows,direction,distance,SOURCE,**kwargs)


def test_official_mask_exact_production_parity():
    from plans.resonance_pc.src.actions._deep_dive_layout_vision import _ui_mask
    np.testing.assert_array_equal(official_ui_mask(),_ui_mask())


@pytest.mark.parametrize('distance',[2,3,7,19,24])
def test_short_grip_cosine_point_two_and_actual_integer_complete_path(distance):
    output=pick([row(cosine=.20)],distance=distance)
    assert output['status']=='local_grip_proposal'
    grip=output['grip']; assert grip['approved_distance_px']==distance
    assert all(type(n) is int for n in grip['start']+grip['end'])
    samples=np.asarray(grip['path_samples'])
    assert np.linalg.norm(np.diff(samples,axis=0),axis=1).max()<=1+1e-9
    assert grip['quad_margin_px']>=3 and grip['mask_margin_px']>=0
    assert grip['source']==SOURCE
    assert not output['input_authorized'] and not output['target_evidence']
    assert not output['full_cube_coordinates_valid'] and not output['freshness_evaluated']


def test_diagonal_integer_distance_respects_maximum():
    grip=pick(direction=(1,1),distance=24)['grip']
    assert 2<=grip['approved_distance_px']<=24
    assert abs(grip['approved_distance_px']-np.linalg.norm(np.subtract(grip['end'],grip['start'])))<1e-9


def test_real_thin_face_can_drag_but_less_than_six_pixel_width_cannot():
    thin=row(((410,200),(430,200),(430,260),(410,260)),cosine=.21)
    assert pick([thin],direction=(0,1))['grip']['approved_distance_px']==24
    too_thin=row(((410,200),(415,200),(415,400),(410,400)),cosine=.21)
    assert pick([too_thin],direction=(0,1))['grip'] is None


def test_actual_quad_area_overrules_fabricated_reported_area():
    small=row(((400,200),(420,200),(420,230),(400,230)))
    small['area']=10000
    assert pick([small])['rejections'][0]['reason']=='insufficient_quad_area'


def test_current_source_requires_exact_all_fields_not_only_generation():
    for key,value in [('session_id','other'),('frame_time',12.6),('map_revision',1),
                      ('generation',11),('frame_id',4),('capture_backend','historical')]:
        item=row();item['source'][key]=value
        assert pick([item])['grip'] is None
    item=row();item['source']['extra']='unknown'
    assert pick([item])['grip'] is None


def test_missing_historical_kind_and_boolean_source_rejected():
    for kind in (None,'historical_face_plane','predicted_full_cube'):
        item=row();item['projection_kind']=kind
        assert pick([item])['grip'] is None
    item=row();item['source']['map_revision']=False
    assert pick([item])['grip'] is None
    item=row();item['current']=False
    assert pick([item])['grip'] is None


def test_current_d_near_frontal_allowed_without_shared_cube_authority():
    output=pick([row(face='D',cosine=.97)])
    assert output['grip']['face']=='D' and not output['full_cube_coordinates_valid']


def test_inset_candidate_avoids_target_covering_centre():
    wide=row(((350,150),(750,150),(750,450),(350,450)))
    grip=pick([wide],target_boxes=[(540,290,20,20)])['grip']
    assert grip is not None and grip['start']!=[550,300]
    for x,y in grip['path_samples']:
        assert not 528<=x<=572 or not 278<=y<=322


def test_entire_target_crossing_checked_not_only_start():
    item=row(((400,200),(480,200),(480,280),(400,280)))
    result=pick([item],target_boxes=[(457,190,2,100)])
    if result['grip'] is not None:
        assert all(p[0]<445 or p[0]>471 for p in result['grip']['path_samples'])


def test_target_expanded_twelve_boundary_excluded_and_mismatch_fails_closed():
    item=row(((400,200),(500,200),(500,300),(400,300)))
    assert pick([item],target_boxes=[(380,180,140,140)])['grip'] is None
    bad_source=dict(SOURCE,generation=9)
    out=pick(target_boxes=[dict(box=[10,10,20,20],source=bad_source)])
    assert out['reason']=='target_source_mismatch'


def test_hud_reset_and_extra_erosion_are_never_overridden_by_allowed_all():
    item=row(((605,495),(675,495),(675,575),(605,575)))
    assert pick([item],allowed_mask=np.ones((720,1280),np.uint8))['grip'] is None
    # Reset begins494; 6px erosion excludes488 onward, not487.
    above=row(((605,485),(675,485),(675,497),(605,497)))
    assert pick([above])['grip'] is None


def test_caller_can_only_add_exclusions():
    allowed=official_ui_mask();allowed[170:330,370:530]=0
    assert pick(allowed_mask=allowed)['grip'] is None


def test_entire_hud_segment_shortens_grip_before_reset():
    item=row(((580,440),(700,440),(700,500),(580,500)))
    out=pick([item],direction=(0,1))
    assert out['grip'] is not None
    for x,y in out['grip']['sample_pixels']:
        assert not (599<=x<=682 and 488<=y<=584)


def test_distance_priority_never_forces_shorter_preferred_face():
    short=row(((400,200),(420,200),(420,260),(400,260)),face='D',cosine=.97)
    longer=row(face='F',cosine=.25)
    result=pick([short,longer])
    assert result['grip']['approved_distance_px']==24
    assert result['grip']['face']=='F'


@pytest.mark.parametrize('quad',[
    ((400,200),(500,300),(500,200),(400,300)),
    ((400,200),(500,200),(500,200),(400,300)),
    ((400,200),(500,200),(np.nan,300),(400,300)),
    ((-1,200),(500,200),(500,300),(400,300))])
def test_invalid_convexity_finite_roi_fails_closed(quad):
    assert pick([row(quad)])['grip'] is None


@pytest.mark.parametrize('direction',[(0,0),(True,False),(np.nan,0),(1,2,3),np.array([True,False])])
def test_invalid_direction(direction):
    assert pick(direction=direction)['reason']=='invalid_direction'


@pytest.mark.parametrize('distance',[1,25,True,np.nan])
def test_invalid_distance(distance):
    assert pick(distance=distance)['reason']=='invalid_distance'


@pytest.mark.parametrize('target',[None,(1,2,0,3),(1,2,-1,3),(1,2,np.nan,3),(True,2,3,4)])
def test_invalid_target_boxes(target):
    assert pick(target_boxes=[target])['reason']=='invalid_target_box'


def test_invalid_mask_and_source_configuration():
    assert pick(allowed_mask=np.zeros((1,2),np.uint8))['reason']=='invalid_allowed_mask'
    assert pick(allowed_mask=np.ones((720,1280),float))['reason']=='invalid_allowed_mask'
    source=dict(SOURCE);source.pop('frame_time')
    assert select_local_grip([row()],(1,0),24,source)['reason']=='invalid_current_source'


def test_no_alias_or_mutation_of_source_quad_masks():
    item=row();copy=deepcopy(item);mask=official_ui_mask();before=mask.copy()
    output=pick([item],allowed_mask=mask)
    output['grip']['source']['generation']=999
    assert item==copy and SOURCE['generation']==10
    np.testing.assert_array_equal(mask,before)
