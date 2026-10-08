"""Research matcher retains original rejection; it has no scanner authority."""
from copy import deepcopy
from pathlib import Path
import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_known_glyph_matcher as matcher
from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics

FIXTURES=Path(__file__).parents[1]/'fixtures/deep_dive_anchor'
NATIVE=Path(semantics.__file__).resolve().parents[2]/'templates/deep_dive_layout'


def rgb(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize('name', matcher.ICONS)
def test_native_known_hypotheses_retain_truth_and_all_other_classes_reject(name):
    image=rgb(NATIVE/f'icon_{name}.png')
    for k in range(4):
        rotated=np.ascontiguousarray(np.rot90(image,k))
        old=semantics.classify_icon(rotated)
        assert old['icon_id']==name
        for expected in matcher.ICONS:
            result=matcher.match_known_icon(rotated,expected)
            assert result['icon_id']==(name if name==expected else None)
            if expected==name: assert result['confidence']==old['confidence']
            assert result['status']=='proposal_only' and result['input_authorized'] is False


@pytest.mark.parametrize('name', ['large_purple_217','large_purple_222','large_purple_241',
                                  'whole_warm_D10_386','whole_warm_D10_391',
                                  'whole_warm_boss_glow_130','purple_wall_0','purple_wall_1',
                                  'purple_wall_2','purple_wall_3','purple_partial_hud_883'])
def test_real_manually_audited_positive_and_negative_fixtures_preserve_rejection(name):
    image=rgb(FIXTURES/f'{name}.png')
    old=semantics.classify_icon(image)
    for expected in matcher.ICONS:
        result=matcher.match_known_icon(image,expected)
        assert result['icon_id']==(expected if old['icon_id']==expected else None)
        if result['icon_id']:assert result['confidence']==old['confidence']


@pytest.mark.parametrize('hue',[0,7,15,23,40,105,135,170])
def test_filled_wall_and_border_seams_never_supply_known_glyph(hue):
    hsv=np.zeros((96,96,3),np.uint8);hsv[:]=[hue,220,230]
    wall=cv2.cvtColor(hsv,cv2.COLOR_HSV2RGB)
    border=np.zeros_like(wall)
    border[:12]=wall[:12];border[84:]=wall[84:];border[:,:12]=wall[:,:12];border[:,84:]=wall[:,84:]
    for image in (wall,border):
        for expected in matcher.ICONS:
            assert matcher.match_known_icon(image,expected)['icon_id'] is None


def test_pixel_template_can_only_veto_never_override_old_shape_or_identity():
    source=dict(session_id=7,frame_id=12,frame_time=10.,map_revision=0,cell_index=4)
    image=rgb(NATIVE/'icon_green_burst.png')
    bank=matcher.make_known_template(image,'green_burst',source=source)
    source['session_id']=8
    assert bank['source']['session_id']==7 and not bank['shape'].flags.writeable
    accepted=matcher.match_known_icon(image,'green_burst',pixel_template=bank)
    assert accepted['icon_id']=='green_burst' and accepted['template_score']>.99
    assert matcher.match_known_icon(image,'yellow_hex',pixel_template=bank)['icon_id'] is None
    corrupted=dict(bank,shape=np.ones((64,64),np.float32))
    assert matcher.match_known_icon(image,'green_burst',pixel_template=corrupted)['icon_id'] is None
    with pytest.raises(ValueError,match='source_missing'):
        matcher.make_known_template(image,'green_burst',source={})


def test_known_matching_does_not_call_public_all_class_classifier(monkeypatch):
    image=rgb(NATIVE/'icon_red_single_eye.png')
    monkeypatch.setattr(semantics,'classify_icon',lambda *_:pytest.fail('full classifier invoked'))
    assert matcher.match_known_icon(image,'red_single_eye')['icon_id']=='red_single_eye'


def scene_item():
    image=np.zeros((720,1280,3),np.uint8)
    image[200:296,400:496]=rgb(NATIVE/'icon_green_burst.png')
    item=dict(index=4,quad=np.float32([[400,200],[495,200],[495,295],[400,295]]),
              centre=np.array([447.5,247.5]),cosine=.8,area=95*95)
    return image,item


def test_actual_pixel_center_keeps_original_bound_without_state_authority():
    image,item=scene_item()
    result=matcher.locate_known_glyph(image,item,'green_burst')
    assert result is not None
    # Native reference contains some off-centre decoration: preserve measured
    # production percentile support instead of inventing geometric truth.
    from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision
    mask=vision._icon_mask(vision._crop(image,item['quad']),'green_burst')
    mask[:10]=0;mask[86:]=0;mask[:,:10]=0;mask[:,86:]=0
    yy,xx=np.nonzero(mask)
    expected=np.array([(np.percentile(xx,3)+np.percentile(xx,97))/2+400,
                       (np.percentile(yy,3)+np.percentile(yy,97))/2+200])
    np.testing.assert_allclose(result['point'],expected,atol=1e-5)
    assert result['input_authorized'] is False and result['status']=='proposal_only'
    assert result['requires_original_pose_support_and_same_frame_guards'] is True
    assert matcher.locate_known_glyph(image,dict(item,centre=item['centre']+[30,0]),'green_burst') is None
    assert matcher.locate_known_glyph(image,dict(item,cosine=.29),'green_burst') is None
    assert matcher.locate_known_glyph(image,dict(item,area=899),'green_burst') is None


@pytest.mark.parametrize('confidence',[.01,.25,.9])
def test_any_weak_box_overlap_prevents_current_center_evidence(confidence):
    image,item=scene_item()
    assert matcher.locate_known_glyph(image,item,'green_burst',targets=[
        dict(box=[450,220,20,20],confidence=confidence)]) is None
    assert matcher.locate_known_glyph(image,item,'green_burst',targets=[dict(box=[float('nan'),0,2,2])]) is None


def test_hud_overlap_and_offscreen_quad_prevent_current_center_evidence():
    image,item=scene_item()
    for delta in ([200,310],[-200,0],[0,450]):
        changed=deepcopy(item);changed['quad']+=delta;changed['centre']+=delta
        assert matcher.locate_known_glyph(image,changed,'green_burst') is None
