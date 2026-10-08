"""SO(3) evidence for a pure two-input normal controller."""
import math

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_front_view_control import choose_front_view


def rotation(vector):
    angle = np.linalg.norm(vector)
    if angle < 1e-12:
        return np.eye(3)
    x, y, z = np.asarray(vector)/angle
    cross = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    return np.eye(3)+math.sin(angle)*cross+(1-math.cos(angle))*(cross@cross)


AXES = {0: [0., .004, 0.], 1: [.003, .001, 0.]}


@pytest.mark.parametrize('roll', [0., .4, 1.7, -2.8])
def test_screen_roll_is_irrelevant_to_normal_control(roll):
    base = rotation([.2, -.1, 0.])
    r = base @ rotation([0., 0., roll])
    baseline = choose_front_view(base, [0., 0., 10.], 'F', AXES)
    actual = choose_front_view(r, [0., 0., 10.], 'F', AXES)
    assert actual['status'] == 'move'
    assert actual['displacement_px'] == pytest.approx(baseline['displacement_px'])
    assert actual['error_deg'] == pytest.approx(baseline['error_deg'])


def test_nonorthogonal_response_converges_under_bounded_pulses():
    r = rotation([.42, -.31, .18])
    errors = []
    for _ in range(40):
        result = choose_front_view(r, [.2, -.1, 10.], 'F', AXES, face_center=[0., 0., -1.5],
                                   max_step_px=12., max_step_deg=2., tolerance_deg=.7)
        errors.append(result['error_deg'])
        if result['status'] == 'aligned':
            break
        assert result['status'] == 'move'
        assert result['distance_px'] <= 12.+1e-9
        assert result['predicted_rotation_deg'] <= 2.+1e-9
        assert result['predicted_error_deg'] < result['error_deg']
        step = np.asarray(result['displacement_px'])
        r = rotation(np.column_stack([AXES[0], AXES[1]])@step)@r
    assert result['status'] == 'aligned'
    assert errors[-1] < .7
    assert all(b < a for a, b in zip(errors, errors[1:]))


@pytest.mark.parametrize('axes', [{0: [0., .004, 0.], 1: [0., .008, 0.]},
                                 {0: [0., 0., .004], 1: [0., 0., .008]}])
def test_rank_deficient_input_response_is_rejected(axes):
    result = choose_front_view(np.eye(3), [0., 0., 10.], 'F', axes, tilt_deg=10.)
    assert result['status'] == 'rejected'
    assert result['reason'] == 'uncontrollable_response_axes'
    assert result['direction'] is None


def test_behind_face_requires_separate_transfer_not_180_degree_pulse():
    result = choose_front_view(np.eye(3), [0., 0., 10.], 'B', AXES)
    assert result['reason'] == 'face_not_front_facing'
    assert result['distance_px'] == 0.


@pytest.mark.parametrize('changes', [dict(rotation=np.diag([1., 1., -1.])),
    dict(translation=[0., 0., -10.]), dict(response_axes={0: [0., 0., 0.], 1: [.003, 0., 0.]}),
    dict(response_axes={0: [0., 1., 0.], 1: [.003, 0., 0.]}), dict(max_step_px=-1.),
    dict(rotation=np.full((3, 3), np.nan))])
def test_invalid_geometry_gain_and_configuration_cannot_move(changes):
    args = dict(rotation=rotation([.2, .1, 0.]), translation=[0., 0., 10.], face='F', response_axes=AXES)
    args.update(changes)
    result = choose_front_view(**args)
    assert result['status'] == 'rejected'
    assert result['direction'] is None
    assert result['displacement_px'] == [0., 0.]


def test_aligned_is_only_geometric_status_with_no_recognition_authority():
    result = choose_front_view(rotation([0., 0., 1.2]), [0., 0., 10.], 'F', AXES, tilt_deg=0.)
    assert result['status'] == 'aligned'
    assert not any(key in result for key in ('ready', 'settled', 'votes', 'frame_id', 'confirmed'))


def test_badly_conditioned_axes_do_not_hide_behind_numerical_full_rank():
    result = choose_front_view(np.eye(3), [0., 0., 10.], 'F',
        {0: [0., .004, 0.], 1: [.000001, .004, 0.]})
    assert result['reason'] == 'uncontrollable_response_axes'
    assert result['response_axes_condition'] > 30.


def test_rank_one_normal_can_descend_with_one_valid_axis_without_ls():
    result = choose_front_view(np.eye(3), [0., 0., 10.], 'F',
                              {0: [0., 0., .004], 1: [.004, 0., 0.]}, tilt_deg=10.)
    assert result['status'] == 'move'
    assert result['response_axes_rank'] == 2
    assert result['jacobian_rank'] == 1
    assert not result['normal_ls_used']
    assert result['displacement_px'][0] == 0.
    assert result['predicted_error_deg'] < result['error_deg']


def test_rank_one_normal_with_no_axis_descent_remains_rejected():
    result = choose_front_view(np.eye(3), [0., 0., 10.], 'F',
                              {0: [0., 0., .004], 1: [0., .004, 0.]}, tilt_deg=10.)
    assert result['response_axes_rank'] == 2
    assert result['jacobian_rank'] == 1
    assert result['status'] == 'rejected'
    assert result['reason'] == 'no_predicted_descent'
    assert not result['normal_ls_used']


def test_actual_trial15_valid_response_has_vertical_descent_despite_normal_condition38():
    # Saved actual trial15 summary/latest_choice: all U/R/F actual fits agree,
    # but the horizontal rotation response is nearly parallel to U's normal.
    r = [[.7380090009518403,.011185306986463917,.6746981572686301],
         [.35393040268395903,.844874740076304,-.4011482813586121],
         [-.5745223969263837,.5348472328512285,.619566342647296]]
    t = [-.002541693916138547,.39923457771205306,42.127658040972356]
    axes = {0:[5.2685410314023604e-05,-.001683877957107418,-.0010059981801867966],
            1:[.0020093975108970118,4.363885191756547e-05,-4.269047920984349e-05]}
    result = choose_front_view(r,t,'U',axes,face_center=[0.,-2.30268665,0.],
        max_step_px=24.,max_step_deg=5.,tilt_deg=30.,tolerance_deg=2.5,min_step_px=2.)
    assert result['status'] == 'move', result
    assert result['jacobian_condition'] == pytest.approx(38.07404769)
    assert result['response_axes_condition'] < 1.1
    assert not result['normal_ls_used']
    assert result['displacement_px'][0] == 0.
    assert result['displacement_px'][1] > 0.
    assert result['predicted_error_deg'] < result['error_deg']-1.
    assert result['distance_px'] <= 24.
    assert result['predicted_rotation_deg'] <= 5.
