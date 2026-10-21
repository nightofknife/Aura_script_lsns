"""Slow evidence, comparisons and cancellation share one operation budget."""
import asyncio

import pytest

from plans.resonance_pc.src.actions._bounded_observation import (
    ObservationBudget, async_wait_for_observation, wait_for_observation,
)


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def run_wait(request):
    def run(**kwargs):
        if request.param:
            sleep = kwargs.pop("sleep")
            async def async_sleep(seconds):
                sleep(seconds)
            return asyncio.run(async_wait_for_observation(**kwargs, sleep=async_sleep))
        return wait_for_observation(**kwargs)
    return run


@pytest.mark.parametrize("duration,matched", [(.999, True), (1., False), (1.001, False)])
def test_slow_positive_observation_must_finish_before_deadline(run_wait, duration, matched):
    now, inputs = [0.], []
    budget = ObservationBudget(1, monotonic=lambda: now[0], check_cancelled=lambda: None)
    def capture():
        now[0] += duration
        return {"found": True}
    result = run_wait(observe=capture, accept=lambda value: value["found"], budget=budget,
                      poll_seconds=.2, sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    if result.matched and budget.active():
        inputs.append("click")
    assert result.matched is matched
    assert result.in_time is matched
    assert inputs == (["click"] if matched else [])


def test_slow_comparison_cannot_confirm_in_time_from_an_early_capture(run_wait):
    now = [0.]
    budget = ObservationBudget(1, monotonic=lambda: now[0], check_cancelled=lambda: None)
    def compare(_):
        now[0] = 1.
        return True
    result = run_wait(observe=lambda: "frame", accept=compare, budget=budget,
                      poll_seconds=.15, sleep=lambda _: pytest.fail("No sleep after deadline"))
    assert result.value == "frame" and result.matched is False and result.in_time is False


@pytest.mark.parametrize("stage", ["observe", "compare", "sleep"])
def test_cancellation_at_deadline_preserves_scene_cancel(run_wait, stage):
    now, cancelled = [0.], [False]
    def check():
        if cancelled[0]:
            raise asyncio.CancelledError("original scene cancellation")
    budget = ObservationBudget(1, monotonic=lambda: now[0], check_cancelled=check)
    def advance(where):
        if stage == where:
            now[0], cancelled[0] = 1., True
    def observe():
        advance("observe")
        return "frame"
    def compare(_):
        advance("compare")
        return False
    with pytest.raises(asyncio.CancelledError, match="original scene cancellation"):
        run_wait(observe=observe, accept=compare, budget=budget, poll_seconds=.2,
                 sleep=lambda _: advance("sleep"))


def test_poll_sleep_uses_remaining_budget_without_an_extra_probe(run_wait):
    now, probes, sleeps = [0.], [], []
    budget = ObservationBudget(.25, monotonic=lambda: now[0], check_cancelled=lambda: None)
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    result = run_wait(observe=lambda: probes.append(now[0]), accept=lambda _: False,
                      budget=budget, poll_seconds=.2, sleep=sleep)
    assert not result.matched and result.in_time is True and probes == [0., .2]
    assert sleeps == pytest.approx([.2, .05])


@pytest.mark.parametrize("immediate,found", [(False, True), (True, True), (True, False)])
def test_zero_timeout_only_explicitly_allows_one_probe(run_wait, immediate, found):
    probes = []
    budget = ObservationBudget(0, monotonic=lambda: 0., check_cancelled=lambda: None,
                               immediate_first_probe=immediate)
    result = run_wait(observe=lambda: (probes.append("probe"), found)[1], accept=bool,
                      budget=budget, poll_seconds=.2,
                      sleep=lambda _: pytest.fail("Zero timeout must never poll"))
    assert len(probes) == int(immediate)
    assert result.matched is (immediate and found)


def test_input_guard_rejects_budget_expired_after_success(run_wait):
    now = [0.]
    budget = ObservationBudget(1, monotonic=lambda: now[0], check_cancelled=lambda: None)
    result = run_wait(observe=lambda: True, accept=bool, budget=budget,
                      poll_seconds=.2, sleep=lambda _: None)
    assert result.matched
    now[0] = 1.
    assert budget.active() is False


@pytest.mark.parametrize("duration,matched", [(.999, True), (1., False), (1.001, False)])
def test_zero_timeout_probe_never_bypasses_enclosing_operation(run_wait, duration, matched):
    now = [0.]
    budget = ObservationBudget(0, monotonic=lambda: now[0], check_cancelled=lambda: None,
                               immediate_first_probe=True, deadline_limit=1.)
    def probe():
        now[0] += duration
        return True
    result = run_wait(observe=probe, accept=bool, budget=budget,
                      poll_seconds=.2, sleep=lambda _: pytest.fail("Only one probe"))
    assert result.matched is matched and budget.active() is matched
