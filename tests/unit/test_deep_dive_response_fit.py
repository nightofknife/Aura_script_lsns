"""Synthetic numerical contracts; not capture, input or live accuracy proof."""
import numpy as np
import pytest

from research.deep_dive_response_fit import fit_response_axes


AXES = np.array([[.001, .004], [.006, -.001], [.002, .002]])


def records(inputs, matrix=AXES):
    return [dict(dx=float(dx), dy=float(dy), rotation_delta=(matrix@[dx, dy]).tolist())
            for dx, dy in inputs]


def test_nonorthogonal_axes_and_mixed_drags_are_fitted_together():
    samples = records([(12, 0), (0, 12), (10, 7), (-8, 12), (14, -5)])
    outcome = fit_response_axes(samples)
    assert outcome['accepted'] and not outcome['rejected']
    np.testing.assert_allclose(outcome['matrix'], AXES, atol=1e-15)
    np.testing.assert_allclose(outcome['axes'][0], AXES[:, 0], atol=1e-15)
    assert outcome['diagnostics']['weighted_rms_deg'] < 1e-10


def test_two_diagonal_directions_need_no_pure_axis_measurement():
    outcome = fit_response_axes(records([(12, 8), (-8, 12)]))
    assert outcome['accepted']
    np.testing.assert_allclose(outcome['matrix'], AXES, atol=1e-15)


def test_rank_one_rejects_even_with_previous_axes():
    outcome = fit_response_axes(records([(12, 6), (16, 8), (-12, -6)]),
                                previous_axes=AXES)
    assert not outcome['accepted'] and outcome['axes'] is None
    assert outcome['reason'] == 'independent_directions_missing'


def test_nearly_parallel_measurements_reject_condition():
    outcome = fit_response_axes(records([(12, 0), (12, .01)]))
    assert outcome['reason'] == 'design_ill_conditioned'


@pytest.mark.parametrize('bad,reason', [
    ({'dx': 10, 'dy': 0, 'rotation_delta': [np.nan, 0, 0]}, 'nonfinite_measurement'),
    ({'dx': 10, 'dy': 0, 'rotation_delta': [0, 0]}, 'invalid_rotation_shape_or_type'),
    ({'dx': 10, 'dy': 0, 'rotation_delta': [1, 0, 0]}, 'rotation_too_large'),
    ({'dx': 10, 'dy': 0, 'rotation_delta': [.05, 0, 0], 'rotation_unit': 'degrees'}, 'rotation_unit_not_radians'),
    ({'dx': True, 'dy': 0, 'rotation_delta': [.05, 0, 0]}, 'invalid_displacement'),
    ({'dx': 1, 'dy': 0, 'rotation_delta': [.05, 0, 0]}, 'input_too_small'),
    ({'dx': 10, 'dy': 0, 'rotation_delta': [0, 0, 0]}, 'rotation_too_small'),
])
def test_bad_measurement_is_not_support(bad, reason):
    outcome = fit_response_axes([records([(0, 12)])[0], bad])
    assert not outcome['accepted'] and outcome['axes'] is None
    assert reason in outcome['diagnostics']['rejected_records'][0]['reason']


def test_valid_records_and_bad_record_have_explicit_exclusion():
    samples = records([(12, 0), (0, 12)])+[{'dx': 4, 'dy': 2}]
    outcome = fit_response_axes(samples)
    assert outcome['accepted']
    assert outcome['diagnostics']['used_indices'] == [0, 1]
    assert len(outcome['diagnostics']['rejected_records']) == 1


def test_high_gain_rejected_despite_zero_fit_residual():
    matrix = np.diag([.06, .06, 0])[:, :2]
    outcome = fit_response_axes(records([(2, 0), (0, 2)], matrix))
    assert outcome['reason'] == 'gain_out_of_bounds'


def test_conflicting_actual_rotation_rejects_residual():
    samples = records([(12, 0), (0, 12), (12, 0), (0, 12)])
    samples[-1]['rotation_delta'] = [-v for v in samples[-1]['rotation_delta']]
    outcome = fit_response_axes(samples)
    assert outcome['reason'] == 'response_inconsistent'


def test_independent_inputs_but_parallel_response_axes_cannot_control_two_directions():
    matrix = np.column_stack([AXES[:, 0], AXES[:, 0]*.8])
    outcome = fit_response_axes(records([(12, 0), (0, 12)], matrix))
    assert outcome['reason'] == 'response_axes_not_independent'


def test_recent_gain_change_has_more_influence_than_unweighted_fit():
    inputs = [(12, 0), (0, 12)]*4
    recent = AXES*1.3
    samples = records(inputs[:4])+records(inputs[4:], recent)
    outcome = fit_response_axes(samples, recency_decay=.5)
    unweighted = fit_response_axes(samples, recency_decay=1.)
    assert outcome['accepted'] and unweighted['accepted']
    assert np.linalg.norm(np.asarray(outcome['matrix'])-recent) < np.linalg.norm(
        np.asarray(unweighted['matrix'])-recent)


def test_recent_window_cannot_borrow_old_independent_axis():
    samples = records([(0, 12)]+[(12, 0)]*8)
    assert fit_response_axes(samples)['reason'] == 'independent_directions_missing'


@pytest.mark.parametrize('kwargs', [{'max_records': 1}, {'recency_decay': 0},
                                   {'max_gain': float('nan')}, {'min_input_px': True}])
def test_invalid_configuration_fails_closed(kwargs):
    assert fit_response_axes(records([(12, 0), (0, 12)]), **kwargs)['reason'] == 'invalid_parameters'


def test_previous_axes_are_diagnostics_not_a_prior_or_mutable_alias():
    previous = {0: [1., 0., 0.], 1: [0., 1., 0.]}
    snapshot = {k: list(v) for k, v in previous.items()}
    outcome = fit_response_axes(records([(12, 0), (0, 12)]), previous_axes=previous)
    assert outcome['accepted'] and outcome['diagnostics']['previous_delta'] > 1
    np.testing.assert_allclose(outcome['matrix'], AXES, atol=1e-15)
    outcome['axes'][0][0] = 999
    assert previous == snapshot
