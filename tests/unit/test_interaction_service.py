"""Interaction contracts with real operation scopes and tracked tiny workers."""
from __future__ import annotations

import asyncio
from collections import deque
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from packages.aura_core.observability import runtime_trace
from packages.aura_core.scheduler import cancellation, operation_scope as operations
from packages.aura_core.scheduler.operation_scope import (
    OperationScope, OperationTimeout, TargetOperationBusy, target_operation_guard,
)
from plans.aura_base.src.services.interaction_service import (
    ClickState, InputSession, InteractionError, InteractionService,
    TransitionPolicy, TransitionProbe, TransitionState,
    confirm, confirm_async, probe_transition, probe_transition_async,
    run_transition, run_transition_async,
)
from plans.aura_base.src.services.observation_service import Frame, Observation, ObservationService
from plans.aura_base.src.services.vision_service import MatchResult


class Clock:
    def __init__(self):
        self.value = 10.

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Target:
    def __init__(self):
        self.epoch = 1

    def target_summary(self):
        return {"pid": 123, "hwnd": self.epoch, "create_time": 456.}


class App:
    def __init__(self):
        self.target_runtime = Target()
        self.captures, self.moves, self.clicks = [], [], []
        self.on_move = self.on_click = None

    def capture(self, rect=None):
        with target_operation_guard(self.target_runtime):
            self.captures.append(rect)
            width, height = rect[2:] if rect else (12, 12)
            return SimpleNamespace(success=True, image=np.zeros((height, width, 3), dtype=np.uint8), relative_rect=rect)

    def move_to(self, x, y, duration):
        with target_operation_guard(self.target_runtime):
            self.moves.append((x, y, duration))
            if self.on_move:
                return self.on_move()

    def click(self, x=None, y=None, button="left"):
        with target_operation_guard(self.target_runtime):
            self.clicks.append((x, y, button))
            if self.on_click:
                return self.on_click()


class Vision:
    def __init__(self, on_match=None):
        self.on_match = on_match
        self.calls = 0

    def load_template_image(self, _template):
        return np.zeros((2, 2, 3), dtype=np.uint8)

    def find_template(self, **_options):
        self.calls += 1
        if self.on_match:
            self.on_match()
        return MatchResult(True, (3, 3), (4, 4), (3, 3, 2, 2), .95)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    monkeypatch.setattr(cancellation, "_cancelled_cids", set())
    monkeypatch.setattr(cancellation, "_sync_actions", {})
    monkeypatch.setattr(runtime_trace, "_events", deque(maxlen=2048))
    monkeypatch.setattr(runtime_trace, "_active", {})
    monkeypatch.setattr(runtime_trace, "_stops", {})
    monkeypatch.setattr(runtime_trace, "_monitor_thread", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(runtime_trace.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "get_config_value", lambda *_args: .25)


def setup(on_match=None, clock=None):
    app, vision = App(), Vision(on_match)
    service = InteractionService(app, ObservationService(app, vision=vision))
    options = {"monotonic": clock} if clock is not None else {}
    scope = OperationScope(3, target=app.target_runtime, cid=uuid4().hex, **options)
    return app, service, scope


def source(app, scope):
    frame = ObservationService(app).capture(scope=scope)
    return frame, Observation("present", frame.id, semantic_key="button", client_center=(4, 4))


async def eventually(predicate):
    deadline = time.monotonic() + 2.
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("worker did not reach the requested lifecycle stage")
        await asyncio.sleep(.001)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_find_click_sync_async_share_input_and_receipt_contract(asynchronous):
    app, service, scope = setup()
    if asynchronous:
        result = asyncio.run(service.find_template_and_click_async("button.png", scope=scope, max_probes=1))
    else:
        result = service.find_template_and_click("button.png", scope=scope, max_probes=1)
    assert result.sent and result.code is None
    assert app.clicks == [(4, 4, "left")]
    assert app.moves == [(4, 4, .2)]
    receipt, = result.receipts
    assert receipt.attempted and receipt.sent and receipt.call_returned
    assert receipt.acknowledged is None
    assert receipt.frame_id == result.observation.frame_id
    assert not cancellation.has_pending_sync_actions(scope.cid)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_late_recognition_never_moves_or_clicks(asynchronous):
    clock = Clock()
    app, service, scope = setup(lambda: clock.advance(4), clock)
    callback = service.find_template_and_click_async if asynchronous else service.find_template_and_click
    result = callback("button.png", scope=scope, max_probes=1)
    result = asyncio.run(result) if asynchronous else result
    assert not result.sent and result.code == "timeout"
    assert app.moves == app.clicks == []
    assert result.receipts == ()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_cancel_during_move_prevents_click(asynchronous):
    app, service, scope = setup()
    app.on_move = lambda: cancellation.request_task_cancel(scope.cid)
    with pytest.raises(asyncio.CancelledError) as error:
        value = service.find_template_and_click_async("button.png", scope=scope) if asynchronous else service.find_template_and_click("button.png", scope=scope)
        if asynchronous:
            asyncio.run(value)
    assert len(app.moves) == 1 and app.clicks == []
    assert error.value.receipts == ()
    assert scope.cancelled


@pytest.mark.parametrize("returned,acknowledged", [(None, None), (True, True), (SimpleNamespace(success=True), True)])
def test_input_return_is_receipt_not_effect_confirmation(returned, acknowledged):
    app, service, scope = setup()
    receipt = service.send_input(lambda: returned, scope=scope, point=(4, 4), bounds=(0, 0, 12, 12))
    assert receipt.sent and receipt.attempted and receipt.call_returned
    assert receipt.acknowledged is acknowledged
    assert not hasattr(receipt, "effect_confirmed")


@pytest.mark.parametrize("returned", [False, SimpleNamespace(success=False)])
def test_explicit_input_rejection_is_a_technical_failure(returned):
    app, service, scope = setup()
    with pytest.raises(InteractionError) as error:
        service.send_input(lambda: returned, scope=scope)
    assert error.value.code == "input_rejected"
    receipt, = error.value.receipts
    assert receipt.attempted and receipt.call_returned
    assert receipt.acknowledged is False and receipt.sent is False


def test_input_exception_preserves_cause_and_attempt_receipt():
    app, service, scope = setup()
    original = RuntimeError("input transport failed")

    def fail():
        raise original

    with pytest.raises(InteractionError) as error:
        service.send_input(fail, scope=scope)
    assert error.value.code == "input_failed" and error.value.__cause__ is original
    receipt, = error.value.receipts
    assert receipt.attempted and not receipt.call_returned and not receipt.sent
    assert "RuntimeError" in receipt.error


def test_find_click_does_not_turn_input_failure_into_source_absence():
    app, service, scope = setup()
    app.on_click = lambda: False
    with pytest.raises(InteractionError, match="input_rejected"):
        service.find_template_and_click("button.png", scope=scope, max_probes=1)
    assert len(app.clicks) == 1


def test_frame_is_consumed_before_another_input_can_reuse_it():
    app, service, scope = setup()
    frame, observed = source(app, scope)
    session = InputSession()
    service.click(observed, frame=frame, scope=scope, session=session)
    with pytest.raises(InteractionError, match="stale_input_frame"):
        service.click(observed, frame=frame, scope=scope, session=session)
    assert len(app.moves) == len(app.clicks) == 1


@pytest.mark.parametrize("stage", ["before_move", "during_move"])
def test_target_epoch_change_prevents_click(stage):
    app, service, scope = setup()
    frame, observed = source(app, scope)
    if stage == "before_move":
        app.target_runtime.epoch += 1
    else:
        app.on_move = lambda: setattr(app.target_runtime, "epoch", 2)
    with pytest.raises(InteractionError, match="input_target_changed"):
        service.click(observed, frame=frame, scope=scope, session=InputSession())
    assert app.clicks == []


def test_final_validator_cannot_rebind_the_target_before_input():
    app, service, scope = setup()
    frame, observed = source(app, scope)
    calls = []
    with pytest.raises(InteractionError, match="input_target_changed"):
        service.send_input(lambda: calls.append(True), scope=scope, point=(4, 4),
            frame=frame, observation=observed, session=InputSession(),
            validate=lambda: setattr(app.target_runtime, "epoch", 2))
    assert calls == []


def test_frame_without_target_identity_cannot_authorize_a_bound_runtime_click():
    app, service, scope = setup()
    frame = Frame.from_image(np.zeros((12, 12, 3), dtype=np.uint8))
    observed = Observation("present", frame.id, client_center=(4, 4))
    with pytest.raises(InteractionError, match="input_target_unverified"):
        service.click(observed, frame=frame, scope=scope, session=InputSession())
    assert app.moves == app.clicks == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_timeout_after_input_keeps_sent_history_without_claiming_success(asynchronous):
    clock = Clock()
    app, service, scope = setup(clock=clock)
    app.on_click = lambda: clock.advance(4)
    if asynchronous:
        result = asyncio.run(service.find_template_and_click_async("button.png", scope=scope))
    else:
        result = service.find_template_and_click("button.png", scope=scope)
    assert result.sent is True and result.code == "timeout"
    receipt, = result.receipts
    assert receipt.sent and receipt.call_returned and receipt.attempted
    assert len(app.clicks) == 1


@pytest.mark.parametrize("phase", ["pending", "returned"])
@pytest.mark.parametrize("entry", ["send_input", "find_click", "transition"])
def test_caller_cancellation_carries_actual_input_phase_after_worker_drain(phase, entry):
    async def scenario():
        app, service, scope = setup()
        entered, release = threading.Event(), threading.Event()
        session = InputSession()
        loop = asyncio.get_running_loop()

        def callback():
            entered.set()
            assert release.wait(2)
            if phase == "returned":
                loop.call_soon_threadsafe(task.cancel)
            return None

        if entry == "send_input":
            task = asyncio.create_task(service.send_input_async(callback, scope=scope, session=session))
        elif entry == "find_click":
            app.on_click = callback
            task = asyncio.create_task(service.find_template_and_click_async(
                "button.png", scope=scope, state=ClickState(inputs=session)))
        else:
            state = TransitionState(TransitionPolicy(), inputs=session)
            task = asyncio.create_task(run_transition_async(
                lambda _allow: TransitionProbe("source", hit={"center": (4, 4)}),
                lambda *_args: callback(), scope=scope, state=state))
        try:
            await eventually(entered.is_set)
            assert session.receipts[0].attempted and not session.receipts[0].call_returned
            if phase == "returned":
                release.set()
            else:
                task.cancel()
                await eventually(lambda: scope.cancelled)
                assert not task.done()
                release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            receipt, = error.value.receipts
            assert receipt.attempted and receipt.call_returned and receipt.sent
            assert receipt.acknowledged is None
            assert not cancellation.has_pending_sync_actions(scope.cid)
            assert runtime_trace.pending_activities(scope.cid) == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_drain_timeout_keeps_unknown_attempt_and_target_quarantine(monkeypatch):
    monkeypatch.setattr(operations, "get_config_value", lambda *_args: .005)

    async def scenario():
        app, service, scope = setup()
        entered, release = threading.Event(), threading.Event()
        session = InputSession()

        def callback():
            entered.set()
            assert release.wait(2)

        task = asyncio.create_task(service.send_input_async(callback, scope=scope, session=session))
        try:
            await eventually(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            receipt, = error.value.receipts
            assert receipt.attempted and not receipt.call_returned
            assert receipt.sent is False and receipt.acknowledged is None
            assert cancellation.has_pending_sync_actions(scope.cid)
            with pytest.raises(TargetOperationBusy) as blocked:
                with OperationScope(target=scope.target, cid=uuid4().hex).activate():
                    pass
            assert blocked.value.quarantined
            release.set()
            await eventually(lambda: not cancellation.has_pending_sync_actions(scope.cid))
            assert session.receipts[0].sent and session.receipts[0].call_returned
            # The published cancellation snapshot remains an unknown attempt;
            # it was never a rollback claim or an optimistic sent receipt.
            assert receipt.call_returned is False
            with OperationScope(target=scope.target, cid=uuid4().hex).activate():
                pass
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_deadline_during_input_preserves_attempt_and_quarantine_until_worker_exit(monkeypatch):
    monkeypatch.setattr(operations, "get_config_value", lambda *_args: .005)

    async def scenario():
        app, service, _ = setup()
        scope = OperationScope(.03, target=app.target_runtime, cid=uuid4().hex)
        entered, release = threading.Event(), threading.Event()
        session = InputSession()

        def callback():
            entered.set()
            assert release.wait(2)

        task = asyncio.create_task(service.send_input_async(callback, scope=scope, session=session))
        try:
            await eventually(entered.is_set)
            with pytest.raises(OperationTimeout) as error:
                await task
            receipt, = error.value.receipts
            assert receipt.attempted and not receipt.sent and not receipt.call_returned
            assert receipt.acknowledged is None
            with pytest.raises(TargetOperationBusy) as blocked:
                with OperationScope(target=scope.target, cid=uuid4().hex).activate():
                    pass
            assert blocked.value.quarantined
            release.set()
            await eventually(lambda: not cancellation.has_pending_sync_actions(scope.cid))
            assert session.receipts[0].sent and session.receipts[0].call_returned
            assert not scope.cancelled
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_public_cancel_without_cid_latches_the_entire_operation_tree():
    parent = OperationScope()
    child = parent.child()
    assert parent.cid == child.cid == "-"
    child.cancel()
    with pytest.raises(asyncio.CancelledError):
        parent.checkpoint()
    with pytest.raises(asyncio.CancelledError):
        child.checkpoint()
    assert not OperationScope().cancelled


@pytest.mark.parametrize("asynchronous", [False, True])
def test_target_already_confirmed_wins_without_any_input(asynchronous):
    app, service, scope = setup()
    calls = []
    probe = lambda _allow: TransitionProbe("target", key="destination")
    click = lambda *_args: calls.append(True)
    result = asyncio.run(run_transition_async(probe, click, scope=scope)) if asynchronous else run_transition(probe, click, scope=scope)
    assert result.confirmed and result.receipts == () and calls == []


def test_supplementary_input_requires_explicit_retry_policy():
    with pytest.raises(ValueError, match="allow_retry"):
        TransitionPolicy(max_clicks=2)
    app, service, scope = setup()
    observed = TransitionProbe("source", hit={"center": (4, 4)})
    calls = []
    with pytest.raises(InteractionError, match="transition_exhausted"):
        run_transition(lambda allow: observed if allow else None,
                       lambda *_args: calls.append(True), scope=scope)
    assert calls == [True]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_retry_reconfirms_and_relocates_source_before_each_input(asynchronous):
    app, service, scope = setup()
    stages = iter([TransitionProbe("source", hit={"center": (4, 4)}), None,
                   TransitionProbe("source", hit={"center": (8, 8)}), TransitionProbe("target")])
    points, flags = [], []

    def probe(allow):
        flags.append(allow)
        return next(stages)

    click = lambda observed, _attempt: points.append(observed.hit["center"])
    policy = TransitionPolicy(max_clicks=2, allow_retry=True)
    result = asyncio.run(run_transition_async(probe, click, scope=scope, policy=policy)) if asynchronous else run_transition(probe, click, scope=scope, policy=policy)
    assert result.confirmed and points == [(4, 4), (8, 8)]
    assert flags == [True, False, True, False]
    assert [receipt.sequence for receipt in result.receipts] == [1, 2]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("interruption", ["unknown", "target"])
def test_raw_source_loss_is_permanent_even_after_source_reappears(asynchronous, interruption):
    app, service, _ = setup()
    clock = Clock()
    scope = OperationScope(3, target=app.target_runtime, cid=uuid4().hex, monotonic=clock)
    policy = TransitionPolicy(max_clicks=3, allow_retry=True, latch_source_loss=True)
    state = TransitionState(policy)
    after_click, flags, clicks = [], [], []

    def raw(collect_source):
        clock.advance(.01)
        flags.append(collect_source)
        if not state.submitted:
            return TransitionProbe("source", key="button", hit={"center": (4, 4)})
        after_click.append(True)
        stage = len(after_click)
        if stage == 1:
            # A still-visible source must be collected during target-only wait;
            # merely disallowing a source result must not fake source loss.
            return TransitionProbe("source", key="button") if collect_source else TransitionProbe()
        if stage == 2:
            return TransitionProbe(interruption, key="destination" if interruption == "target" else None)
        if stage <= 4:
            return TransitionProbe("source", key="button") if collect_source else TransitionProbe()
        return TransitionProbe("target", key="destination")

    def click(*_args):
        clicks.append(True)

    if asynchronous:
        async def probe(allow):
            return await probe_transition_async(raw, allow_source=allow, scope=scope.child(.4),
                poll_seconds=.01, sleep=clock.advance, state=state)
        result = asyncio.run(run_transition_async(probe, click, scope=scope, policy=policy, state=state, sleep=clock.advance))
    else:
        def probe(allow):
            return probe_transition(raw, allow_source=allow, scope=scope.child(.4),
                poll_seconds=.01, sleep=clock.advance, state=state)
        result = run_transition(probe, click, scope=scope, policy=policy, state=state, sleep=clock.advance)
    assert result.confirmed and clicks == [True] and state.source_locked
    assert flags[:4] == [True, True, True, True]
    assert flags[4:] and all(flag is False for flag in flags[4:])


@pytest.mark.parametrize("asynchronous", [False, True])
def test_async_input_callback_is_explicitly_rejected(asynchronous):
    app, service, scope = setup()

    async def click(*_args):
        app.clicks.append("must not run")

    with pytest.raises(ValueError, match="synchronous"):
        if asynchronous:
            asyncio.run(run_transition_async(lambda _allow: TransitionProbe("source"), click, scope=scope))
        else:
            run_transition(lambda _allow: TransitionProbe("source"), click, scope=scope)
    assert app.clicks == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_transition_detects_epoch_change_during_raw_observation(asynchronous):
    app, service, scope = setup()

    def raw(_allow):
        app.target_runtime.epoch += 1
        return TransitionProbe("source", immediate=True)

    with pytest.raises(InteractionError, match="input_target_changed"):
        if asynchronous:
            asyncio.run(probe_transition_async(raw, allow_source=True, scope=scope, poll_seconds=0))
        else:
            probe_transition(raw, allow_source=True, scope=scope, poll_seconds=0)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_domain_confirmation_exception_is_not_suppressed(asynchronous):
    app, service, scope = setup()
    original = RuntimeError("invalid domain screenshot")

    def probe():
        raise original

    with pytest.raises(RuntimeError) as error:
        if asynchronous:
            asyncio.run(confirm_async(probe, scope=scope, before_probe=False))
        else:
            confirm(probe, scope=scope, before_probe=False)
    assert error.value is original


@pytest.mark.parametrize("stage", ["custom_sleep", "async_probe"])
@pytest.mark.parametrize("cid_mode", ["uuid", "default"])
def test_cancellation_inside_async_callbacks_latches_the_scope(stage, cid_mode):
    async def scenario():
        app, service, scope = setup()
        if cid_mode == "default":
            scope = OperationScope(target=app.target_runtime)
            assert scope.cid == "-"
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*_args):
            entered.set()
            await release.wait()
            return TransitionProbe("target")

        if stage == "custom_sleep":
            task = asyncio.create_task(confirm_async(lambda: True, scope=scope, sleep=blocked))
        else:
            task = asyncio.create_task(probe_transition_async(blocked, allow_source=True, scope=scope))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert scope.cancelled
        with pytest.raises(asyncio.CancelledError):
            service.send_input(lambda: app.clicks.append(True), scope=scope)
        assert app.clicks == []

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_comparison_time_is_included_in_transition_confirmation_budget(asynchronous):
    clock = Clock()
    app, service, _ = setup()
    scope = OperationScope(1, monotonic=clock, target=app.target_runtime, cid=uuid4().hex)
    compare_calls = []

    def equivalent(_previous, _current):
        compare_calls.append(True)
        clock.advance(2)
        return True

    options = dict(allow_source=True, scope=scope, equivalent=equivalent, poll_seconds=0)
    raw = lambda _allow: TransitionProbe("source", key="button")
    result = asyncio.run(probe_transition_async(raw, **options)) if asynchronous else probe_transition(raw, **options)
    assert result is None and compare_calls == [True]
