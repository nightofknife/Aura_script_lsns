"""Wait policies stay distinct and cancellation never becomes a recovery click."""
import asyncio
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import _cancellable_wait as waits
from plans.resonance_pc.src.actions import _reforming_center_clicks as reforming
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel
from plans.resonance_pc.src.actions import purchase_book_pc_actions as books
from plans.resonance_pc.src.actions import rubbish_recycling_pc_actions as rubbish
from plans.resonance_pc.src.actions import trade_negotiation_pc_actions as negotiation


def test_requested_slices_do_not_shorten_when_system_sleep_overshoots():
    elapsed, slices, checks = [0.0], [], []

    def sleep(seconds):
        slices.append(seconds)
        elapsed[0] += seconds + 1

    waits.sleep_slices(.12, sleep=sleep, check_cancelled=lambda: checks.append(elapsed[0]))
    assert slices == pytest.approx([.05, .05, .02])
    assert len(checks) == 4
    assert checks[0] == 0
    assert checks[-1] == elapsed[0]


def test_deadline_wait_counts_system_sleep_overshoot():
    elapsed, slices, checks = [0.0], [], []

    def sleep(seconds):
        slices.append(seconds)
        elapsed[0] += seconds + 1

    waits.sleep_until_deadline(.12, sleep=sleep, monotonic=lambda: elapsed[0],
                               check_cancelled=lambda: checks.append(elapsed[0]))
    assert slices == [.05]
    assert checks == [0, 1.05]


@pytest.mark.parametrize("duration", [0, -.5])
@pytest.mark.parametrize("policy", [waits.sleep_slices, waits.sleep_until_deadline])
def test_nonpositive_wait_still_checks_cancellation_once(duration, policy):
    events = []
    kwargs = {"monotonic": lambda: 0} if policy is waits.sleep_until_deadline else {}
    policy(duration, sleep=lambda seconds: pytest.fail("Unexpected sleep"),
           check_cancelled=lambda: events.append("check"), **kwargs)
    assert events == ["check"]


@pytest.mark.parametrize("policy", [waits.sleep_slices, waits.sleep_until_deadline])
@pytest.mark.parametrize("cancel_after_sleep", [False, True])
def test_caller_cancellation_exception_passes_through(policy, cancel_after_sleep):
    error = asyncio.CancelledError("domain cancellation")
    slept = []

    def check():
        if not cancel_after_sleep or slept:
            raise error

    kwargs = {"monotonic": lambda: 0} if policy is waits.sleep_until_deadline else {}
    with pytest.raises(asyncio.CancelledError) as caught:
        policy(10, sleep=lambda seconds: slept.append(seconds), check_cancelled=check, **kwargs)
    assert caught.value is error
    assert slept == ([.05] if cancel_after_sleep else [])


def test_async_slices_preserve_requested_duration_and_final_check():
    events = []

    async def sleep(seconds):
        events.append(seconds)

    asyncio.run(waits.async_sleep_slices(.07, sleep=sleep,
                                         check_cancelled=lambda: events.append("check")))
    assert events[::2] == ["check", "check", "check"]
    assert events[1::2] == pytest.approx([.05, .02])


def test_async_slices_propagate_cancellation_from_sleeper():
    async def sleep(seconds):
        raise asyncio.CancelledError("task cancelled")

    with pytest.raises(asyncio.CancelledError, match="task cancelled"):
        asyncio.run(waits.async_sleep_slices(1, sleep=sleep, check_cancelled=lambda: None))


def test_existing_deadline_never_sleeps_past_remaining_time():
    elapsed, slices, checks = [0.0], [], []

    def sleep(seconds):
        slices.append(seconds)
        elapsed[0] += seconds

    waits.sleep_before_deadline(.07, sleep=sleep, monotonic=lambda: elapsed[0],
                                check_cancelled=lambda: checks.append(elapsed[0]))
    assert slices == pytest.approx([.05, .02])
    assert checks == pytest.approx([0, .05, .07])


def test_existing_deadline_final_guard_rejects_operation_timeout():
    error = RuntimeError("operation timeout")
    with pytest.raises(RuntimeError) as caught:
        waits.sleep_before_deadline(1, monotonic=lambda: 1,
                                    sleep=lambda seconds: pytest.fail("Unexpected sleep"),
                                    check_cancelled=lambda: (_ for _ in ()).throw(error))
    assert caught.value is error


@pytest.mark.parametrize("module,name,error,message", [
    (trade, "_sleep_with_cancel", trade.CityTradeFlowError, "Trade input cancelled"),
    (travel, "_sleep_with_cancel", asyncio.CancelledError, "intercity operation was cancelled"),
    (books, "_cancelable_sleep", asyncio.CancelledError, "Purchase-book input cancelled"),
    (negotiation, "_cancelable_sleep", asyncio.CancelledError, "Trade negotiation cancelled"),
    (rubbish, "_sleep", asyncio.CancelledError, "Rubbish recycling cancelled"),
])
def test_adapters_keep_domain_cancellation_even_for_zero_wait(monkeypatch, module, name, error, message):
    monkeypatch.setattr(module, "is_current_task_cancel_requested", lambda: True)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 0,
                                                        sleep=lambda _: pytest.fail("Unexpected sleep")))
    with pytest.raises(error, match=message) as caught:
        getattr(module, name)(0)
    if module is trade:
        assert caught.value.code == "trade_cancelled"


def test_reforming_without_context_preserves_single_sleep_argument(monkeypatch):
    marker = object()
    calls = []
    monkeypatch.setattr(reforming, "time", SimpleNamespace(sleep=calls.append))
    reforming._sleep(marker)
    assert calls == [marker]


def test_reforming_opted_in_context_cancels_after_first_slice_and_resets(monkeypatch):
    events = []
    error = asyncio.CancelledError("reforming cancelled")

    def check():
        events.append("check")
        if any(isinstance(event, float) for event in events):
            raise error

    monkeypatch.setattr(reforming, "time", SimpleNamespace(sleep=events.append))
    with pytest.raises(asyncio.CancelledError) as caught:
        with reforming.cancellation_checks(check):
            reforming._sleep(10)
    assert caught.value is error
    assert events == ["check", "check", .05, "check"]
    reforming._sleep(.2)
    assert events[-1] == .2
