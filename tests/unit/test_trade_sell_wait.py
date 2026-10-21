"""Sell waits share timing machinery without changing submission or guards."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from plans.resonance_pc.src.actions import _trade_sell_state as sell


def session(monkeypatch, guard, *, overhead=0):
    now, sleeps = [0.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds + overhead
    monkeypatch.setattr(sell, "time", NS(monotonic=lambda: now[0], sleep=sleep))
    instance = sell.SellSession(
        NS(click=lambda **kwargs: pytest.fail("Waiting cannot submit a sale")), None, guard, None,
    )
    return instance, now, sleeps


@pytest.mark.parametrize("seconds", [0, -.1, float("nan")])
def test_expired_or_nonpositive_wait_still_runs_final_guard(monkeypatch, seconds):
    guards = []
    instance, _, sleeps = session(monkeypatch, lambda: guards.append(True))
    instance.pause(seconds)
    assert guards == [True]
    assert sleeps == []


def test_wait_preserves_wall_clock_budget_and_final_guard(monkeypatch):
    guards = []
    instance, now, sleeps = session(monkeypatch, lambda: guards.append(True), overhead=.2)
    instance.pause(.12)
    assert sleeps == [.05]
    assert now[0] == .25 and guards == [True, True]


def test_wait_preserves_requested_slice_limit_and_remainder(monkeypatch):
    guards = []
    instance, now, sleeps = session(monkeypatch, lambda: guards.append(True))
    instance.pause(.12)
    assert sleeps == pytest.approx([.05, .05, .02])
    assert now[0] == pytest.approx(.12) and len(guards) == 4


@pytest.mark.parametrize("after_sleep", [False, True])
def test_cancellation_propagates_without_followup_input(monkeypatch, after_sleep):
    error = asyncio.CancelledError("sell cancelled")
    def guard():
        if not after_sleep or sleeps:
            raise error
    instance, _, sleeps = session(monkeypatch, guard)
    with pytest.raises(asyncio.CancelledError) as caught:
        instance.pause(30)
    assert caught.value is error
    assert sleeps == ([.05] if after_sleep else [])


def test_timeout_from_final_guard_is_not_swallowed(monkeypatch):
    error = RuntimeError("sell deadline expired")
    def guard():
        if now[0] >= .12:
            raise error
    instance, now, sleeps = session(monkeypatch, guard, overhead=.2)
    with pytest.raises(RuntimeError) as caught:
        instance.pause(.12)
    assert caught.value is error
    assert sleeps == [.05]


@pytest.mark.parametrize("seconds", [None, "1"])
def test_invalid_duration_is_not_coerced_before_guard(monkeypatch, seconds):
    instance, _, sleeps = session(monkeypatch, lambda: pytest.fail("Invalid addition precedes the guard"))
    with pytest.raises(TypeError):
        instance.pause(seconds)
    assert sleeps == []
