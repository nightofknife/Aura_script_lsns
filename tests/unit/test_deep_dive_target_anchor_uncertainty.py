import math

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_target_anchor_uncertainty import (
    PIXEL_QUANTIZATION_RADIUS, certify_target_anchor,
)


def geometry():
    paths = np.full((54, 16, 2), 100.)
    paths[0] = [1., 0.]
    paths[1] = [15., 0.]
    return paths, np.full(54, 20.)


def test_certificate_preserves_gates_throughout_disk():
    paths, scales = geometry()
    result = certify_target_anchor([0., 0.], paths, scales, 0)
    assert result['ready'] and result['valid_corridors'] == 54
    for theta in np.linspace(0., 2*math.pi, 97):
        point = PIXEL_QUANTIZATION_RADIUS * np.array([math.cos(theta), math.sin(theta)])
        errors = np.linalg.norm(paths-point, axis=2).min(axis=1)/scales
        assert np.argmin(errors) == 0
        assert errors[0] < .30 and errors[1:].min()-errors[0] > .12


def test_point_pass_can_fail_entire_disk():
    paths, scales = geometry()
    paths[0] = [3., 0.]
    paths[1] = [-5.5, 0.]
    assert (5.5-3.)/20 > .12
    result = certify_target_anchor([0., 0.], paths, scales, 0)
    assert not result['ready']
    assert result['margin_lower'] < .12


def test_saved_source348_corridor_is_not_quantization_stable():
    paths = np.full((54, 16, 2), 10000.)
    scales = np.full(54, 20.)
    paths[25] = [885.5997319473779, 574.2909451933932]
    paths[26] = [889.4272035725442, 552.7451867773095]
    scales[25] = 51.877968265095504
    scales[26] = 50.22829788451695
    result = certify_target_anchor([886.0746154785156, 566.3212890625], paths, scales, 25)
    assert result['nominal_error'] == pytest.approx(.1538956164611577)
    assert result['margin_lower'] == pytest.approx(.09680380877407876)
    assert not result['ready'] and result['competitor_index'] == 26


def test_every_surface_can_veto_even_when_not_visible():
    paths, scales = geometry()
    paths[53] = [2., 0.]
    result = certify_target_anchor([0., 0.], paths, scales, 0)
    assert not result['ready'] and result['competitor_index'] == 53


def test_unequal_scales_and_multiple_samples_use_rivals_own_bound():
    paths, scales = geometry()
    scales[1] = 100.
    paths[1, 8] = [20., 0.]
    result = certify_target_anchor([0., 0.], paths, scales, 0)
    assert not result['ready']
    assert result['competitor_error_lower'] == pytest.approx(.15-PIXEL_QUANTIZATION_RADIUS/100)


def test_caller_radius_is_explicit_assumption_not_pose_proof():
    paths, scales = geometry()
    result = certify_target_anchor([0., 0.], paths, scales, 0, pixel_radius=3.)
    assert result['radius_assumption'] == 'caller_supplied_pixel_radius'
    assert result['proof'] == 'fixed_geometry_lipschitz_disk'


def test_certificate_never_changes_projection_or_masks():
    paths, scales = geometry()
    mask = np.ones(54, bool)
    before = (paths.copy(), scales.copy(), mask.copy())
    certify_target_anchor([0., 0.], paths, scales, 0, valid=mask)
    for actual, expected in zip((paths, scales, mask), before):
        np.testing.assert_array_equal(actual, expected)


def test_invalid_behind_camera_corridor_can_be_explicitly_masked():
    paths, scales = geometry()
    paths[53] = np.nan
    mask = np.ones(54, bool)
    mask[53] = False
    result = certify_target_anchor([0., 0.], paths, scales, 0, valid=mask)
    assert result['ready'] and result['valid_corridors'] == 53
    assert not certify_target_anchor([0., 0.], paths, scales, 0)['ready']


@pytest.mark.parametrize('radius', [0., .5, -1., math.inf, math.nan, True])
def test_radius_cannot_evade_quantization_floor(radius):
    paths, scales = geometry()
    assert not certify_target_anchor([0., 0.], paths, scales, 0, pixel_radius=radius)['ready']


@pytest.mark.parametrize('change', ['shape', 'nan_point', 'zero_scale', 'winner', 'mask', 'all_invalid'])
def test_malformed_geometry_fails_closed(change):
    paths, scales = geometry()
    point = [0., 0.]
    winner = 0
    mask = None
    if change == 'shape': paths = paths[:53]
    elif change == 'nan_point': point = [np.nan, 0.]
    elif change == 'zero_scale': scales[0] = 0.
    elif change == 'winner': winner = True
    elif change == 'mask': mask = np.ones(54, int)
    elif change == 'all_invalid': mask = np.zeros(54, bool)
    result = certify_target_anchor(point, paths, scales, winner, valid=mask)
    assert result == {'ready': False, 'reason': 'invalid_uncertainty_inputs'}
