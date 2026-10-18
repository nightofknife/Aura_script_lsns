"""Slow synchronous recognition must not authorize input beyond its probe budget."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import black_moon_local_exit_pc_actions as exits
from plans.resonance_pc.src.actions import black_moon_local_prepare_pc_actions as prepare
from plans.resonance_pc.src.actions._template_click_transition import (
    TransitionProbe, wait_for_transition_probe,
)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0., cancelled=False, sleeps=[])

    async def sleep(seconds):
        state.now += seconds
        state.sleeps.append(seconds)

    timer = SimpleNamespace(monotonic=lambda: state.now)
    monkeypatch.setattr(prepare, "time", timer)
    monkeypatch.setattr(exits, "time", timer)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(prepare, "is_current_task_cancel_requested", lambda: state.cancelled)
    return state


@pytest.mark.parametrize("kind,immediate", [("source", False), ("target", False), ("source", True)])
@pytest.mark.parametrize("finish", [7., 8.2])
def test_probe_rejects_evidence_at_or_after_deadline(clock, kind, immediate, finish):
    calls = []

    def observe(allow_source):
        calls.append(allow_source)
        clock.now = finish if immediate or len(calls) == 2 else 0.
        return TransitionProbe(kind, key="same", immediate=immediate)

    result = asyncio.run(wait_for_transition_probe(
        observe, allow_source=True, timeout_seconds=7., poll_seconds=.2,
        monotonic=lambda: clock.now, sleep=asyncio.sleep, check_cancelled=prepare._cancel_check,
    ))
    assert result is None
    assert len(calls) == (1 if immediate else 2)


@pytest.mark.parametrize("finish", [6.999, 7.])
def test_equivalence_work_must_finish_before_deadline(clock, finish):
    def equivalent(previous, current):
        clock.now = finish
        return True

    result = asyncio.run(wait_for_transition_probe(
        lambda _: TransitionProbe("source", key="button"), allow_source=True,
        timeout_seconds=7., poll_seconds=.2, monotonic=lambda: clock.now,
        sleep=asyncio.sleep, check_cancelled=prepare._cancel_check, equivalent=equivalent,
    ))
    assert (result is not None) is (finish < 7.)


def app_with_capture(clock, capture_seconds):
    state = SimpleNamespace(captures=0, clicks=[])
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    def capture():
        state.captures += 1
        clock.now += capture_seconds
        return SimpleNamespace(success=True, image=frame)

    state.capture = capture
    state.click = lambda **point: state.clicks.append(point)
    return state


@pytest.mark.parametrize("step,capture_seconds,code", [
    ("enable_batch", 4., "black_moon_prepare_source_unconfirmed"),
    ("enter_shop", 7., "black_moon_prepare_source_unconfirmed"),
    ("verify_ready", 4., "black_moon_prepare_not_ready"),
])
def test_prepare_never_clicks_or_succeeds_on_late_capture(clock, step, capture_seconds, code):
    app = app_with_capture(clock, capture_seconds)
    reader = SimpleNamespace(
        observe=lambda _: dict(page_state="local_shop", local_selected=True,
                               batch_enabled=step == "verify_ready", ready=step == "verify_ready"),
        match=lambda *_: dict(found=True, center=(700, 170)),
    )
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as caught:
        asyncio.run(prepare.execute_black_moon_prepare_step(step, app, reader))
    assert caught.value.code == code
    assert not app.clicks


@pytest.mark.parametrize("page,code", [
    ("local_shop", "black_moon_exit_source_unknown"),
    ("rest_menu", "black_moon_exit_source_unknown"),
])
def test_exit_never_clicks_or_succeeds_on_late_capture(clock, page, code):
    app = app_with_capture(clock, 4.)
    reader = SimpleNamespace(
        VIEWPORT=(594, 143, 684, 500),
        observe=lambda _: dict(page_state=page, shop_ready=True, total_zero=True),
        button=lambda *_: dict(found=True, center=(90, 36)),
    )
    with pytest.raises(exits.BlackMoonLocalExitError) as caught:
        asyncio.run(exits.execute_black_moon_exit(
            dict(success=True, ready_to_exit=True), "rest_menu", app, reader))
    assert caught.value.code == code
    assert not app.clicks


@pytest.mark.parametrize("finish", [29.999, 30., 32.2])
@pytest.mark.parametrize("slow_stage", ["observe", "equivalence"])
def test_purchase_wait_requires_all_confirmation_work_inside_thirty_seconds(clock, monkeypatch, finish, slow_stage):
    app = app_with_capture(clock, 0.)

    def observe(_):
        if app.captures == 2 and slow_stage == "observe":
            clock.now = finish
        return dict(page_state="local_shop", shop_ready=True, total_zero=True)

    def same_view(*_):
        if slow_stage == "equivalence":
            clock.now = finish
        return True

    monkeypatch.setattr(exits, "_same_view", same_view)
    reader = SimpleNamespace(VIEWPORT=(594, 143, 684, 500), observe=observe)
    run = exits.execute_black_moon_wait_purchase(dict(success=True, purchase_submitted=True), app, reader)
    if finish < 30.:
        assert asyncio.run(run)["purchase_verified"] is True
    else:
        with pytest.raises(exits.BlackMoonLocalExitError) as caught:
            asyncio.run(run)
        assert caught.value.code == "black_moon_purchase_not_settled"
    assert app.captures == 2 and not app.clicks


@pytest.mark.parametrize("operation", ["probe", "purchase"])
def test_cancellation_after_slow_recognition_takes_priority_over_timeout(clock, operation):
    def observe(_):
        clock.now = 35.
        clock.cancelled = True
        if operation == "probe":
            return TransitionProbe("source", immediate=True)
        return dict(page_state="local_shop", shop_ready=True, total_zero=True)

    if operation == "probe":
        run = wait_for_transition_probe(
            observe, allow_source=True, timeout_seconds=7., poll_seconds=.2,
            monotonic=lambda: clock.now, sleep=asyncio.sleep, check_cancelled=prepare._cancel_check,
        )
    else:
        app = app_with_capture(clock, 0.)
        reader = SimpleNamespace(VIEWPORT=(594, 143, 684, 500), observe=observe)
        run = exits.execute_black_moon_wait_purchase(dict(success=True, purchase_submitted=True), app, reader)
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as caught:
        asyncio.run(run)
    assert caught.value.code == "black_moon_prepare_cancelled"


@pytest.mark.parametrize("operation", ["probe", "purchase"])
def test_cancellation_during_frame_equivalence_takes_priority(clock, monkeypatch, operation):
    def equivalent(*_):
        clock.now = 35.
        clock.cancelled = True
        return True

    if operation == "probe":
        run = wait_for_transition_probe(
            lambda _: TransitionProbe("source", key="same"), allow_source=True,
            timeout_seconds=7., poll_seconds=.2, monotonic=lambda: clock.now,
            sleep=asyncio.sleep, check_cancelled=prepare._cancel_check, equivalent=equivalent,
        )
    else:
        monkeypatch.setattr(exits, "_same_view", equivalent)
        app = app_with_capture(clock, 0.)
        reader = SimpleNamespace(VIEWPORT=(594, 143, 684, 500), observe=lambda _: dict(
            page_state="local_shop", shop_ready=True, total_zero=True))
        run = exits.execute_black_moon_wait_purchase(dict(success=True, purchase_submitted=True), app, reader)
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as caught:
        asyncio.run(run)
    assert caught.value.code == "black_moon_prepare_cancelled"


def test_slow_purchase_screenshots_cannot_be_accepted_after_thirty_seconds(clock):
    app = app_with_capture(clock, 16.)
    reader = SimpleNamespace(VIEWPORT=(594, 143, 684, 500), observe=lambda _: dict(
        page_state="local_shop", shop_ready=True, total_zero=True))
    with pytest.raises(exits.BlackMoonLocalExitError) as caught:
        asyncio.run(exits.execute_black_moon_wait_purchase(
            dict(success=True, purchase_submitted=True), app, reader))
    assert caught.value.code == "black_moon_purchase_not_settled"
    assert clock.now == pytest.approx(32.2)
    assert app.captures == 2 and not app.clicks
