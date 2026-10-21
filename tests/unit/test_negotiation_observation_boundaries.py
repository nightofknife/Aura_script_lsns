"""Negotiation must separate invalid/late evidence from business transitions."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_negotiation_pc_actions as negotiation


@pytest.fixture
def rig(monkeypatch):
    state = NS(now=0.0, cancelled=False, captures=0, clicks=[], matches=[],
               read=lambda name: False, capture_delay=0.0)

    def sleep(seconds):
        state.now += seconds

    def capture(rect):
        state.captures += 1
        state.now += state.capture_delay
        return NS(success=True, image=np.zeros((rect[3], rect[2], 3), np.uint8))

    def find_template(**kwargs):
        name = Path(kwargs["template_image"]).name
        state.matches.append((name, kwargs["threshold"], kwargs["use_grayscale"]))
        value = state.read(name)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, bool):
            return NS(found=value, confidence=.99, center_point=(10, 20))
        return value

    state.app = NS(capture=capture, click=lambda **point: state.clicks.append(point))
    state.vision = NS(find_template=find_template)
    monkeypatch.setattr(negotiation, "time", NS(monotonic=lambda: state.now, sleep=sleep))
    monkeypatch.setattr(negotiation, "is_current_task_cancel_requested", lambda: state.cancelled)
    return state


def wait(rig, *, expected=True, timeout=1, exhausted=None):
    return negotiation._wait_for_template_state(
        app=rig.app, vision=rig.vision, template="button.png",
        region=negotiation._BUTTON_REGION, threshold=.86,
        expected_found=expected, timeout_sec=timeout, poll_interval_sec=.2,
        exhausted_template=exhausted,
    )


@pytest.mark.parametrize("result", [
    NS(found=False, confidence=.0, debug_info={"error": "broken-template"}),
    NS(found=False, confidence=float("nan")),
    NS(found=False, confidence=float("inf")),
    NS(found=None, confidence=.9),
    ValueError("decode failed"),
])
def test_technical_failure_never_proves_animation_start(rig, result):
    rig.read = lambda _: result
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        wait(rig, expected=False)
    assert caught.value.code == "negotiation_template_match_failed"
    assert caught.value.detail["valid"] is False
    assert caught.value.detail["found"] is None
    assert rig.clicks == []


def test_real_negative_is_valid_animation_start_evidence(rig):
    result = wait(rig, expected=False)
    assert result["valid"] is True
    assert result["found"] is False
    assert result["state_confirmed"] is True
    assert rig.captures == 1


def test_capture_failure_keeps_existing_scene_error(rig):
    rig.app.capture = lambda **_: NS(success=False)
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        wait(rig, expected=False)
    assert caught.value.code == "negotiation_page_lost"


def test_positive_button_without_valid_point_cannot_authorize_input(rig):
    rig.read = lambda _: NS(found=True, confidence=.99, center_point=None)
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        wait(rig)
    assert caught.value.code == "negotiation_template_match_failed"
    assert caught.value.detail["reason"] == "invalid_match_center"
    assert rig.clicks == []


@pytest.mark.parametrize("delay", [1.0, 1.1])
@pytest.mark.parametrize("expected", [False, True])
def test_late_observation_does_not_confirm_either_state(rig, delay, expected):
    rig.capture_delay = delay
    rig.read = lambda _: expected
    result = wait(rig, expected=expected)
    assert result["state_confirmed"] is False
    assert result["valid"] is False and result["found"] is None
    assert result["reason"] == "observation_timeout"


def test_zero_timeout_keeps_explicit_single_probe(rig):
    rig.read = lambda _: True
    result = wait(rig, timeout=0)
    assert result["state_confirmed"] is True
    assert rig.captures == 1


def test_exhausted_marker_needs_two_frames_and_overrides_shared_button(rig):
    rig.read = lambda _: True
    result = wait(rig, exhausted="exhausted.png")
    assert result["attempts_exhausted"] is True
    assert result["found"] is False
    assert result["state_confirmed"] is True
    assert [name for name, _, _ in rig.matches] == [
        "button.png", "exhausted.png", "button.png", "exhausted.png",
    ]
    assert rig.now == pytest.approx(.2)
    assert rig.clicks == []


def test_one_exhausted_hit_never_becomes_terminal_or_blocks_later_valid_button(rig):
    exhausted = iter([True, False])
    rig.read = lambda name: next(exhausted) if name == "exhausted.png" else True
    result = wait(rig, exhausted="exhausted.png")
    assert result["found"] is True
    assert result.get("attempts_exhausted") is not True
    assert result["center"] == [1100, 445]


def test_late_second_exhausted_frame_never_becomes_terminal(rig):
    count = [0]

    def read(name):
        if name == "exhausted.png":
            count[0] += 1
            if count[0] == 2:
                rig.now += 1
        return True

    rig.read = read
    result = wait(rig, exhausted="exhausted.png")
    assert result["state_confirmed"] is False
    assert not result.get("attempts_exhausted")


@pytest.mark.parametrize("delay", [1.0, 1.1])
def test_late_button_match_never_causes_click(rig, delay):
    def read(name):
        if name == Path(negotiation._NEGOTIATION_CONFIG["raise"]["button_template"]).name:
            rig.now += delay
            return True
        return False

    rig.read = read
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(
            requested_to_cap=True, app=rig.app, vision=rig.vision,
            total_timeout_sec=10, button_wait_timeout_sec=1,
        )
    assert caught.value.code == "negotiation_button_not_found"
    assert rig.clicks == []


def test_click_guard_rechecks_phase_budget_after_wait_returns(rig, monkeypatch):
    monkeypatch.setattr(negotiation, "_confirm_cap", lambda **_: {"confirmed": False})

    def delayed_return(**_):
        rig.now += 1
        return {"valid": True, "found": True, "center": [1100, 445], "state_confirmed": True}

    monkeypatch.setattr(negotiation, "_wait_for_template_state", delayed_return)
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(
            requested_to_cap=True, app=rig.app, vision=rig.vision,
            total_timeout_sec=10, button_wait_timeout_sec=1,
        )
    assert caught.value.code == "negotiation_button_not_found"
    assert rig.clicks == []


@pytest.mark.parametrize("kind", ["raise", "bargain"])
def test_late_two_frame_terminal_state_cannot_claim_success_or_degraded_completion(rig, kind):
    def read(name):
        if "cap20" in name or "rebargain" in name:
            rig.now += .4
            return True
        return False

    rig.read = read
    execute = negotiation.execute_raise_to_cap if kind == "raise" else negotiation.execute_bargain_to_cap
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        execute(requested_to_cap=True, app=rig.app, vision=rig.vision, total_timeout_sec=1)
    assert caught.value.code == "negotiation_cap_detection_timeout"
    assert rig.clicks == []


def test_parent_deadline_rejects_late_cap_even_if_confirmation_adapter_returns_true(rig, monkeypatch):
    def confirm(**_):
        rig.now += 1
        return {"confirmed": True, "confidence": .99}

    monkeypatch.setattr(negotiation, "_confirm_cap", confirm)
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(
            requested_to_cap=True, app=rig.app, vision=rig.vision, total_timeout_sec=1,
        )
    assert caught.value.code == "negotiation_cap_detection_timeout"
    assert rig.clicks == []


def test_zero_timeout_phase_cannot_click_after_parent_expires_during_input_guard(rig, monkeypatch):
    checks = [None]
    monkeypatch.setattr(negotiation, "_confirm_cap", lambda **_: {"confirmed": False})

    def checked_cancel():
        if checks[0] is not None:
            checks[0] += 1
            if checks[0] == 3:
                # The total guard was timely; the local input guard itself
                # consumed the last remaining time before the negotiation input.
                rig.now = 1.

    def immediate_button(**_):
        checks[0] = 0
        return {"valid": True, "found": True, "center": [1100, 445], "state_confirmed": True}

    monkeypatch.setattr(negotiation, "_check_cancelled", checked_cancel)
    monkeypatch.setattr(negotiation, "_wait_for_template_state", immediate_button)
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(
            requested_to_cap=True, app=rig.app, vision=rig.vision,
            total_timeout_sec=1, button_wait_timeout_sec=0,
        )
    assert caught.value.code == "negotiation_cap_detection_timeout"
    assert caught.value.detail["attempts_used"] == 0
    assert caught.value.detail["actual_fatigue_used"] == 0
    assert rig.clicks == []


def test_cancellation_wins_when_matching_also_expires_budget(rig):
    def read(name):
        if name == Path(negotiation._NEGOTIATION_CONFIG["raise"]["button_template"]).name:
            rig.now += 1
            rig.cancelled = True
            return True
        return False

    rig.read = read
    with pytest.raises(asyncio.CancelledError):
        negotiation.execute_raise_to_cap(
            requested_to_cap=True, app=rig.app, vision=rig.vision, button_wait_timeout_sec=1,
        )
    assert rig.clicks == []


def test_normal_raise_keeps_thresholds_confirmation_and_fatigue_accounting(rig):
    cap = iter([False, True, True])
    button = iter([True, False, True])
    rig.read = lambda name: next(cap) if "cap20" in name else next(button)
    result = negotiation.execute_raise_to_cap(requested_to_cap=True, app=rig.app, vision=rig.vision)
    assert result["completed_to_cap"] is True
    assert result["stop_reason"] == "cap_reached"
    assert result["attempts_used"] == 1 and result["actual_fatigue_used"] == 8
    assert rig.clicks == [{"x": 1100, "y": 445}]
    assert all(gray for _, _, gray in rig.matches)
    assert all(threshold == (.88 if "cap20" in name else .86)
               for name, threshold, _ in rig.matches)


def test_attempt_limit_retains_only_confirmed_input_fatigue(rig):
    button = iter([True, False, True])
    rig.read = lambda name: False if "cap20" in name else next(button)
    result = negotiation.execute_raise_to_cap(
        requested_to_cap=True, app=rig.app, vision=rig.vision, max_attempts=1,
    )
    assert result["stop_reason"] == "attempt_limit_reached"
    assert result["completed_to_cap"] is False and result["degraded"] is True
    assert result["attempts_used"] == 1 and result["actual_fatigue_used"] == 8
    assert len(rig.clicks) == 1


def test_exhausted_before_first_input_keeps_zero_fatigue_and_does_not_click(rig):
    rig.read = lambda name: "rebargain" in name
    result = negotiation.execute_bargain_to_cap(requested_to_cap=True, app=rig.app, vision=rig.vision)
    assert result["stop_reason"] == "attempts_exhausted"
    assert result["actual_fatigue_used"] == 0
    assert result["attempts_used"] == 0
    assert rig.clicks == []


def test_invalid_animation_evidence_after_click_does_not_replay_input(rig):
    button_calls = [0]

    def read(name):
        if "cap20" in name:
            return False
        button_calls[0] += 1
        if button_calls[0] == 1:
            return True
        return NS(found=False, confidence=0, debug_info={"error": "broken-template"})

    rig.read = read
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(requested_to_cap=True, app=rig.app, vision=rig.vision)
    assert caught.value.code == "negotiation_template_match_failed"
    assert caught.value.detail["kind"] == "raise"
    assert caught.value.detail["attempts_used"] == 1
    assert caught.value.detail["actual_fatigue_used"] == 8
    assert caught.value.detail["max_attempts"] == negotiation.DEFAULT_NEGOTIATION_MAX_ATTEMPTS
    assert len(rig.clicks) == 1


def test_invalid_evidence_before_input_reports_zero_spent_fatigue(rig):
    rig.read = lambda _: NS(found=False, confidence=0, debug_info={"error": "broken-template"})
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(requested_to_cap=True, app=rig.app, vision=rig.vision)
    assert caught.value.code == "negotiation_template_match_failed"
    assert caught.value.detail["kind"] == "raise"
    assert caught.value.detail["attempts_used"] == 0
    assert caught.value.detail["actual_fatigue_used"] == 0
    assert caught.value.detail["max_attempts"] == negotiation.DEFAULT_NEGOTIATION_MAX_ATTEMPTS
    assert rig.clicks == []


def test_total_timeout_after_input_retains_spent_fatigue(rig):
    def click(**point):
        rig.clicks.append(point)
        rig.now += 1

    rig.app.click = click
    rig.read = lambda name: "raise_button" in name
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(
            requested_to_cap=True, app=rig.app, vision=rig.vision, total_timeout_sec=1,
        )
    assert caught.value.code == "negotiation_cap_detection_timeout"
    assert caught.value.detail["attempts_used"] == 1
    assert caught.value.detail["actual_fatigue_used"] == 8
    assert len(rig.clicks) == 1
