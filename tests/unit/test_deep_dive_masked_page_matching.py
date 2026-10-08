"""Native masked text matching retains positions and decision scores."""
import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as page


@pytest.mark.parametrize('name',['insufficient_roles','rest_title','enemy_turn_title','plane_1'])
def test_binary_masked_correlation_matches_opencv_on_shifted_native_text(name):
    reference,mask=page._template(name)
    rng=np.random.default_rng(38)
    roi=rng.integers(0,100,size=(reference.shape[0]+25,reference.shape[1]+30),dtype=np.uint8)
    roi[11:11+reference.shape[0],17:17+reference.shape[1]]=reference
    expected=cv2.matchTemplate(roi,reference,cv2.TM_CCOEFF_NORMED,mask=mask)
    actual=page._binary_masked_ncc(roi,name)
    a=cv2.minMaxLoc(actual);b=cv2.minMaxLoc(expected)
    assert a[3]==b[3]==(17,11)
    assert abs(a[1]-b[1])<1e-5
    finite=np.isfinite(expected)
    np.testing.assert_allclose(actual[finite],expected[finite],atol=1e-5,rtol=1e-5)


def test_constant_page_does_not_recognize_native_text():
    template,_=page._template('insufficient_roles')
    for value in (0,80,255):
        roi=np.full((template.shape[0]+25,template.shape[1]+30),value,np.uint8)
        response=page._binary_masked_ncc(roi,'insufficient_roles')
        assert float(response.max())<.8
