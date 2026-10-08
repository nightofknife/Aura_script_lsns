"""Input-effect prototype: no renderer acknowledgement is inferred from time."""

import math

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_input_ack import InputAckLedger, _log


AXES = {0: np.array([math.radians(.2), 0., 0.]),
        1: np.array([0., math.radians(.2), 0.])}


def rot(degrees, axis=(1., 0., 0.)):
    axis = np.asarray(axis)/np.linalg.norm(axis)
    theta = math.radians(degrees)
    x, y, z = axis
    skew = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    return np.eye(3)+math.sin(theta)*skew+(1.-math.cos(theta))*(skew @ skew)


def observe(ledger, angle=0., seq=0, now=0., **kwargs):
    fields = dict(session_id='session', map_revision=0, correction_epoch=0)
    fields.update(kwargs)
    return ledger.observe(rot(angle), seq=seq, now=now, **fields)


def seed(**kwargs):
    ledger = InputAckLedger(**kwargs)
    assert observe(ledger)['accepted']
    return ledger


def issue(ledger, pixels=10., now=.01):
    return ledger.issued([pixels, 0.], AXES, now=now)


def test_without_actual_geometry_no_input_is_permitted():
    ledger = InputAckLedger()
    assert not ledger.allowance([1., 0.], AXES, now=0.)['allowed']
    assert not issue(ledger)['recorded']


def test_new_capture_generations_do_not_reset_debt_or_credit():
    ledger = seed()
    assert issue(ledger, 50.)['recorded']
    for seq in (1, 2):
        result = observe(ledger, seq=seq, now=.01+seq*.07)
        assert result['accepted'] and result['credited_deg'] == 0.
        assert ledger.debt_px == pytest.approx(50.)
        assert ledger.allowance([1., 0.], AXES, now=.02+seq*.07)['allowed_px'] == 0.


def test_three_fresh_frames_without_effect_freeze_even_with_unused_credit():
    ledger = seed()
    issue(ledger)
    for seq in range(1, 4):
        observe(ledger, seq=seq, now=.01+seq*.07)
    result = ledger.allowance([1., 0.], AXES, now=.23)
    assert result['state'] == 'awaiting_effect'
    assert result['decision_reason'] == 'waiting_for_actual_effect'
    assert result['allowed_px'] == 0.
    assert ledger.debt_px == pytest.approx(10.)


def test_actual_positive_effect_can_resume_a_frozen_leg():
    ledger = seed()
    issue(ledger)
    for seq in range(1, 4):
        observe(ledger, seq=seq, now=seq*.06)
    result = observe(ledger, angle=1., seq=4, now=.24)
    assert result['credited_deg'] == pytest.approx(1.)
    assert result['debt_px'] == pytest.approx(5.)
    assert result['no_progress_frames'] == 0
    assert ledger.allowance([1., 0.], AXES, now=.25)['allowed']


def test_frozen_effect_wait_is_bounded_and_does_not_clear_debt():
    ledger = seed(effect_wait_sec=.3)
    issue(ledger)
    for seq in range(1, 4):
        observe(ledger, seq=seq, now=seq*.05)
    result = ledger.allowance([1., 0.], AXES, now=.46)
    assert result['decision_reason'] == 'input_effect_timeout'
    assert result['debt_px'] == pytest.approx(10.)
    assert not observe(ledger, angle=2., seq=4, now=.47)['accepted']


def test_jitter_does_not_pay_debt_and_small_real_motion_accumulates():
    ledger = seed()
    issue(ledger)
    for seq, angle in enumerate((.04, -.06, .08), 1):
        assert observe(ledger, angle=angle, seq=seq, now=seq*.05)['credited_deg'] == 0.
    result = observe(ledger, angle=.3, seq=4, now=.2)
    assert result['credited_deg'] == pytest.approx(.3)
    assert ledger.debt_angle == pytest.approx(math.radians(1.7))


def test_back_and_forth_motion_does_not_repay_the_same_progress_twice():
    ledger = seed()
    issue(ledger, 20.)
    assert observe(ledger, angle=1., seq=1, now=.05)['credited_deg'] == pytest.approx(1.)
    assert observe(ledger, angle=0., seq=2, now=.1)['credited_deg'] == 0.
    assert observe(ledger, angle=1., seq=3, now=.15)['credited_deg'] == 0.
    assert observe(ledger, angle=1.5, seq=4, now=.2)['credited_deg'] == pytest.approx(.5)
    assert ledger.debt_angle == pytest.approx(math.radians(2.5))


def test_orthogonal_motion_and_wrong_signed_motion_do_not_pay_debt():
    ledger = seed()
    issue(ledger)
    assert observe(ledger, angle=-1., seq=1, now=.05)['credited_deg'] == 0.
    result = ledger.observe(rot(2., (0., 1., 0.)), seq=2, now=.1,
                            session_id='session', map_revision=0, correction_epoch=0)
    assert result['credited_deg'] == 0.
    assert ledger.debt_px == pytest.approx(10.)


def test_same_frame_cannot_ack_more_motion_or_reset_freshness():
    ledger = seed()
    issue(ledger)
    assert observe(ledger, angle=1., seq=1, now=.05)['credited_deg'] == pytest.approx(1.)
    result = observe(ledger, angle=2., seq=1, now=.1)
    assert not result['accepted'] and result['credited_deg'] == 0.
    assert ledger.debt_px == pytest.approx(5.)
    assert ledger.allowance([1., 0.], AXES, now=.41)['decision_reason'] == 'geometry_age_exceeded'


@pytest.mark.parametrize('identity', [dict(correction_epoch=1), dict(map_revision=1),
                                    dict(session_id='other')])
def test_context_change_with_debt_is_not_an_ack_and_cannot_rebase(identity):
    ledger = seed()
    issue(ledger)
    result = observe(ledger, angle=2., seq=1, now=.05, **identity)
    assert result['reason'] == 'context_changed_with_unconfirmed_input'
    assert result['debt_px'] == pytest.approx(10.)
    assert not ledger.rebase(rot(2.), seq=1, now=.06, session_id='other',
                             map_revision=1, correction_epoch=1)['accepted']
    assert not ledger.allowance([1., 0.], AXES, now=.07)['allowed']


def test_context_change_without_debt_requires_explicit_rebase():
    ledger = seed()
    assert not observe(ledger, seq=1, now=.05, correction_epoch=1)['accepted']
    assert not ledger.allowance([1., 0.], AXES, now=.06)['allowed']
    assert ledger.rebase(rot(4.), seq=0, now=.07, session_id='new',
                         map_revision=1, correction_epoch=1)['accepted']
    assert ledger.allowance([1., 0.], AXES, now=.08)['allowed']


def test_semantic_or_predicted_rotation_cannot_ack():
    ledger = seed()
    issue(ledger)
    assert not observe(ledger, angle=2., seq=1, now=.05, source_kind='semantic')['accepted']
    assert not observe(ledger, angle=2., seq=1, now=.1, source_kind='predicted')['accepted']
    assert ledger.debt_px == pytest.approx(10.)
    assert not ledger.allowance([1., 0.], AXES, now=.11)['allowed']
    assert observe(ledger, angle=1., seq=2, now=.15)['credited_deg'] == pytest.approx(1.)


def test_reversal_stays_pending_and_opposite_input_cannot_cancel_debt():
    ledger = seed()
    issue(ledger)
    result = ledger.allowance([-1., 0.], AXES, now=.02)
    assert result['decision_reason'] == 'queued_direction_change'
    assert result['pending_direction'] == [-1., 0.]
    assert ledger.debt_px == pytest.approx(10.)
    assert observe(ledger, angle=2., seq=1, now=.05)['debt_px'] == 0.
    assert ledger.status()['pending_direction'] == [-1., 0.]
    assert not ledger.allowance([1., 0.], AXES, now=.06)['allowed']
    assert issue(ledger, -10., now=.07)['recorded']
    assert observe(ledger, angle=0., seq=2, now=.1)['credited_deg'] == pytest.approx(2.)


def test_illegally_issued_reverse_fails_closed_and_does_not_cancel_debt():
    ledger = seed()
    issue(ledger)
    assert not issue(ledger, -10., now=.02)['recorded']
    assert ledger.debt_px == pytest.approx(10.)
    assert ledger.status()['reason'] == 'issued_without_credit'
    assert not ledger.allowance([1., 0.], AXES, now=.03)['allowed']


def test_angle_and_pixel_caps_both_apply_and_queries_do_not_reserve_credit():
    ledger = seed(angle_cap_deg=16.)
    fast = {0: np.array([math.radians(1.), 0., 0.]), 1: AXES[1]}
    for _ in range(3):
        assert ledger.allowance([1., 0.], fast, now=.01)['allowed_px'] == pytest.approx(16.)
    assert ledger.issued([16., 0.], fast, now=.02)['recorded']
    assert not ledger.issued([1., 0.], fast, now=.03)['recorded']
    assert ledger.debt_angle == pytest.approx(math.radians(16.))


def test_fifo_credit_preserves_pixel_debt_when_response_scale_changes():
    ledger = seed()
    assert issue(ledger)['recorded']  # ten px, two estimated degrees
    faster = {0: AXES[0]*2., 1: AXES[1]}
    assert ledger.issued([10., 0.], faster, now=.02)['recorded']  # four degrees
    result = observe(ledger, angle=3., seq=1, now=.05)
    assert result['debt_angle_deg'] == pytest.approx(3.)
    assert result['debt_px'] == pytest.approx(7.5)


def test_overshoot_does_not_prepay_future_commands():
    ledger = seed()
    issue(ledger)
    assert observe(ledger, angle=5., seq=1, now=.05)['credited_deg'] == pytest.approx(2.)
    assert issue(ledger, now=.06)['recorded']
    assert observe(ledger, angle=5., seq=2, now=.1)['credited_deg'] == 0.
    assert ledger.debt_px == pytest.approx(10.)


@pytest.mark.parametrize('fields', [dict(tracking_ok=False), dict(source_age_sec=.4)])
def test_failed_or_stale_geometry_does_not_ack_and_freezes_allowance(fields):
    ledger = seed()
    issue(ledger)
    assert not observe(ledger, angle=2., seq=1, now=.05, **fields)['accepted']
    assert ledger.debt_px == pytest.approx(10.)
    assert not ledger.allowance([1., 0.], AXES, now=.06)['allowed']


def test_sequence_regression_invalid_rotation_and_clock_regression_fail_closed():
    ledger = seed()
    issue(ledger)
    observe(ledger, seq=2, now=.05)
    assert not observe(ledger, seq=1, now=.06)['accepted']
    assert not ledger.allowance([1., 0.], AXES, now=.07)['allowed']
    result = ledger.observe(np.ones((3, 3)), seq=3, now=.08, session_id='session',
                            map_revision=0, correction_epoch=0)
    assert not result['accepted']
    assert result['reason'] == 'invalid_observed_rotation'
    assert not ledger.allowance([1., 0.], AXES, now=.07)['allowed']
    assert ledger.status()['reason'] == 'nonmonotonic_clock'


def test_body_compensation_math_matches_actual_track_then_right_correction_order():
    # Research identity only; the prototype still rejects epoch changes.
    previous = rot(30., (0., 1., 0.))
    body = rot(5., (0., 0., 1.))
    actual_motion = rot(2.)
    corrected = actual_motion @ previous @ body
    recovered = corrected @ (previous @ body).T
    assert np.allclose(recovered, actual_motion)
    assert np.linalg.norm(_log(corrected @ previous.T)) > math.radians(4.)
    assert np.linalg.norm(_log(recovered)) == pytest.approx(math.radians(2.))
    no_motion_corrected = previous @ body
    assert np.linalg.norm(_log(no_motion_corrected @ (previous @ body).T)) < 1e-8


def test_long_continuous_leg_does_not_wrap_or_lose_debt_above_180_degrees():
    ledger = seed()
    assert issue(ledger, 40.)['recorded']
    for seq in range(1, 111):
        result = observe(ledger, angle=seq*2., seq=seq, now=seq*.1)
        assert result['credited_deg'] == pytest.approx(2.)
        assert result['debt_px'] == pytest.approx(30.)
        assert issue(ledger, 10., now=seq*.1+.01)['recorded']
    assert ledger.debt_px == pytest.approx(40.)
