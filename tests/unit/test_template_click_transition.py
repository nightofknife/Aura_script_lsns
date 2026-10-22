"""The common executor consumes evidence without deciding business success."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from packages.aura_core.scheduler.operation_scope import OperationScope

from plans.resonance_pc.src.actions._template_click_transition import (
    TransitionProbe as Probe, run_template_transition, wait_for_transition_probe,
)


def wait(rows, *, allow_source=False, equivalent=None, cancelled=None):
    state = SimpleNamespace(now=0., observations=0)
    rows = iter(rows)
    def observe(allow_source):
        state.observations += 1
        return next(rows, Probe())
    async def sleep(seconds):
        state.now += seconds
    async def execute():
        return await wait_for_transition_probe(
            observe, allow_source=allow_source, timeout_seconds=1., poll_seconds=.1,
            monotonic=lambda: state.now, sleep=sleep,
            check_cancelled=cancelled or (lambda: None), equivalent=equivalent,
        )
    return asyncio.run(execute()), state


def test_unknown_resets_consecutive_confirmation():
    target = Probe("target", key="page")
    result, state = wait([target, Probe(), target, target])
    assert (result.kind, result.key, result.hit, result.frame, result.immediate) == (
        target.kind, target.key, target.hit, target.frame, target.immediate)
    assert state.observations == 4


def test_source_never_confirms_target_even_with_same_key():
    result, state = wait([Probe("source", key="same"), Probe("source", key="same")])
    assert result is None


def test_changed_source_identity_or_point_requires_new_pair():
    rows = [Probe("source", key=(identity, point)) for identity, point in
            [("first", 1), ("second", 1), ("second", 2), ("second", 2)]]
    result, state = wait(rows, allow_source=True)
    assert result.key == ("second", 2) and state.observations == 4


def test_domain_stability_condition_is_required_in_addition_to_key():
    rows = [Probe("source", key="button", frame=frame) for frame in [1, 2, 3, 3]]
    result, state = wait(rows, allow_source=True,
                         equivalent=lambda previous, current: previous.frame == current.frame)
    assert result.frame == 3 and state.observations == 4


def test_single_observation_exception_only_applies_to_authorized_source():
    immediate = Probe("source", hit={"center": [1, 2]}, immediate=True)
    result, state = wait([immediate], allow_source=True)
    assert (result.kind, result.hit, result.immediate) == (immediate.kind, immediate.hit, True)
    assert state.observations == 1
    result, _ = wait([immediate])
    assert result is None
    result, _ = wait([Probe("target", key="page", immediate=True)])
    assert result is None


def test_probe_errors_and_cancellation_propagate():
    def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        wait([Probe("target", key="page")], cancelled=cancelled)
    with pytest.raises(ValueError, match="Invalid transition observation"):
        wait([Probe("capture_failed")])


def execute(probes, *, cancel_after_click=False):
    state = SimpleNamespace(clicks=[], sleeps=[], checks=[], cancelled=False, probes=[])
    observations = iter(probes)
    async def probe(allow_source):
        state.probes.append(allow_source)
        return next(observations)
    def check():
        if state.cancelled:
            raise asyncio.CancelledError()
    def click(observed, attempt):
        state.clicks.append((attempt, observed.hit))
        state.cancelled = cancel_after_click
    async def sleep(seconds):
        state.sleeps.append(seconds)
    async def run():
        await run_template_transition(
            probe, click, max_clicks=2, settle_seconds=.35, sleep=sleep,
            check_cancelled=check, source_unconfirmed=lambda: ValueError("source unknown"),
            exhausted=lambda: RuntimeError("click limit"),
            scope=OperationScope(10, cid=uuid4().hex),
        )
    return state, run


def test_target_seen_before_input_skips_click_and_settle():
    state, run = execute([Probe("target")])
    asyncio.run(run())
    assert state.probes == [True] and not state.clicks and not state.sleeps


def test_supplement_uses_fresh_source_and_target_probes_cannot_authorize_input():
    state, run = execute([Probe("source", hit="first"), None,
                          Probe("source", hit="relocated"), Probe("target")])
    asyncio.run(run())
    assert state.clicks == [(1, "first"), (2, "relocated")]
    assert state.probes == [True, False, True, False]
    assert state.sleeps == [.35, .35]


def test_unknown_after_click_cannot_authorize_another_input():
    state, run = execute([Probe("source", hit="first"), None, None])
    with pytest.raises(ValueError, match="source unknown"):
        asyncio.run(run())
    assert state.clicks == [(1, "first")]


def test_bounded_clicks_report_exhaustion_without_extra_probe_or_input():
    state, run = execute([Probe("source", hit="first"), None,
                          Probe("source", hit="second"), None])
    with pytest.raises(RuntimeError, match="click limit"):
        asyncio.run(run())
    assert len(state.clicks) == 2 and len(state.probes) == 4


def test_cancellation_after_input_prevents_feedback_or_supplement():
    state, run = execute([Probe("source", hit="first"), Probe("target")], cancel_after_click=True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert len(state.clicks) == 1 and state.probes == [True]
