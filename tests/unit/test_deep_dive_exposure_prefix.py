"""SO(3) tests for research-only exposure hints; no capture/recognition."""
import math

import cv2
import numpy as np
import pytest

from research.deep_dive_exposure_prefix import choose_exposure_prefix


RAY = np.array((0., 0., -1.))
REFERENCE = np.array((0., 0., -1.))
AXES = {0: np.array((.01, 0., 0.)), 1: np.array((0., .01, 0.))}


def choice(rotation=np.eye(3), target=(1., 0., 0.), axes=AXES, **kwargs):
    return choose_exposure_prefix(rotation, RAY, REFERENCE, target, axes, **kwargs)


def after(rotation, result, axes=AXES):
    movement = np.column_stack((axes[0], axes[1])) @ result['displacement_px']
    return cv2.Rodrigues(movement)[0] @ rotation


def test_exactly_back_facing_hint_has_bounded_second_order_descent():
    result = choice(target=-REFERENCE)
    assert result['status'] == 'move'
    assert result['target_hint_cosine'] == -1.
    assert -1. < result['predicted_target_hint_cosine'] < 0.
    assert result['predicted_reference_cosine'] > .99
    assert result['predicted_rotation_deg'] <= 5.+1e-12
    assert result['distance_px'] <= 24.+1e-12


def test_adjacent_hint_prediction_matches_independent_SO3():
    rotation = cv2.Rodrigues(np.array((.13, -.08, .72)))[0]
    result = choice(rotation)
    assert result['status'] == 'move'
    predicted = after(rotation, result)
    assert predicted[:, 0] @ RAY == pytest.approx(result['predicted_target_hint_cosine'])
    assert (predicted @ REFERENCE) @ RAY == pytest.approx(result['predicted_reference_cosine'])
    assert result['predicted_target_hint_cosine'] > result['target_hint_cosine']


def test_antipodal_route_stops_before_reference_loss_without_handoff():
    rotation = np.eye(3)
    for _ in range(100):
        result = choice(rotation, target=-REFERENCE)
        assert result['reference_cosine'] >= .20-1e-12
        if result['status'] != 'move':
            break
        rotation = after(rotation, result)
        assert result['predicted_reference_margin'] >= 0.
    else:
        pytest.fail('unbounded antipodal route')
    assert result['status'] == 'rejected'
    assert result['reason'] == 'no_safe_hint_improving_step'
    assert result['target_hint_cosine'] < 0.


def test_hint_ready_grants_no_actual_readiness_or_identity():
    rotation = cv2.Rodrigues(np.array((0., math.radians(40.), 0.)))[0]
    result = choice(rotation)
    assert result['status'] == 'exposure_hint_ready'
    assert result['displacement_px'] == [0., 0.]
    for key in ('unknown_face_projection_valid', 'actual_grid_ready', 'identity_ready',
                'targets_ready', 'full_cube_coordinates_valid', 'current_source_validated'):
        assert result[key] is False
    assert 'quad' not in result and 'translation' not in result


def test_reference_at_silhouette_rejects_before_hint_readiness():
    rotation = cv2.Rodrigues(np.array((0., math.radians(85.), 0.)))[0]
    result = choice(rotation)
    assert result['reason'] == 'reference_visibility_margin_insufficient'
    assert result['status'] == 'rejected'


@pytest.mark.parametrize('axes,reason', [
    ({0:(0., 0., 0.), 1:(0., .01, 0.)}, 'invalid_response_gain'),
    ({0:(.051, 0., 0.), 1:(0., .01, 0.)}, 'invalid_response_gain'),
    ({0:(.01, 0., 0.), 1:(.02, 0., 0.)}, 'uncontrollable_response_axes'),
    ({0:(.04, 0., 0.), 1:(0., .001, 0.)}, 'uncontrollable_response_axes'),
])
def test_invalid_axes_remain_rejected_even_if_hint_ready(axes, reason):
    result = choice(target=REFERENCE, axes=axes)
    assert result['status'] == 'rejected'
    assert result['reason'] == reason


def test_nonorthogonal_axes_are_used_as_full_rotation_vectors():
    axes = {0:np.array((.012, .003, .002)), 1:np.array((.004, .009, -.002))}
    result = choice(axes=axes)
    assert result['status'] == 'move'
    predicted = after(np.eye(3), result, axes)
    assert predicted[:, 0] @ RAY == pytest.approx(result['predicted_target_hint_cosine'])
    assert result['predicted_rotation_deg'] <= 5.+1e-12


def test_shrunk_candidates_preserve_reference_margin():
    rotation = cv2.Rodrigues(np.array((0., math.acos(.225), 0.)))[0]
    result = choice(rotation, target=-REFERENCE)
    assert result['status'] == 'move'
    assert result['predicted_reference_cosine'] >= .2
    assert result['predicted_rotation_deg'] < 5.


@pytest.mark.parametrize('kwargs', [dict(max_step_deg=5.1), dict(max_step_px=25.),
    dict(max_response_condition=31.), dict(min_cosine_improvement=0.)])
def test_caps_and_original_condition_gate_cannot_be_expanded(kwargs):
    assert choice(**kwargs)['reason'] == 'invalid_configuration'


def test_invalid_input_and_zero_normal_do_not_propose_motion():
    assert choice(rotation=np.zeros((3, 3)))['status'] == 'rejected'
    assert choice(target=(0., 0., 0.))['status'] == 'rejected'
    assert choice(axes={0:(np.nan, 0., 0.), 1:(0., .01, 0.)})['status'] == 'rejected'
