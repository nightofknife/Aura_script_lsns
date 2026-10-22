"""Observation-backed input and bounded transitions.

An input receipt records a submitted request, never a confirmed UI effect.
Mutable input/transition state belongs to the caller's operation, not the service.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import cv2

from packages.aura_core.api import service_info
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope

from .observation_service import Frame, Observation, ObservationService
from ._target_identity import read_target_identity


@dataclass(frozen=True)
class InputReceipt:
    attempted: bool = False
    sent: bool = False
    acknowledged: bool | None = None
    call_returned: bool = False
    target_identity: Any = None
    timestamp: float | None = None
    sequence: int = 0
    point: tuple[float, float] | None = None
    frame_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ClickResult:
    """sent is historical input submission; code='timeout' is not success."""
    sent: bool = False
    observation: Observation = field(default_factory=lambda: Observation("unknown", reason="not_observed"))
    receipts: tuple[InputReceipt, ...] = ()
    code: str | None = None
    phase: str = "observe"

    @property
    def last_observation(self):
        return self.observation


class InteractionError(RuntimeError):
    def __init__(self, code: str, phase: str, *, observation=None, receipts=(), message=None):
        super().__init__(message or code)
        self.code, self.phase = code, phase
        self.observation = self.last_observation = observation
        self.receipts = tuple(receipts)


@dataclass
class InputSession:
    """Explicit operation-local sequence and invalidated source-frame IDs."""
    sequence: int = 0
    consumed_frames: set[str] = field(default_factory=set)
    receipts: list[InputReceipt] = field(default_factory=list)


@dataclass
class ClickState:
    observation: Observation = field(default_factory=lambda: Observation("unknown", reason="not_observed"))
    phase: str = "observe"
    inputs: InputSession = field(default_factory=InputSession)


def _identity(app, scope):
    runtime = getattr(app, "target_runtime", None) or scope.target
    return read_target_identity(runtime)


def _checkpoint(scope, check=None):
    scope.checkpoint()
    if check is not None:
        check()
    scope.checkpoint()


def _seconds(value, name):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _count(value, name):
    if isinstance(value, bool) or int(value) != value or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _attach(exception, observation=None, receipts=(), phase=None):
    # Cancellation keeps its native exception type and the last known stage.
    exception.last_observation = observation
    exception.receipts = tuple(receipts)
    if phase is not None:
        exception.phase = phase
    return exception


def _stop_cancelled(scope, exception):
    if isinstance(exception, asyncio.CancelledError):
        return scope.cancellation_error(exception)
    return exception


def _delay(scope, seconds, sleep=None, check=None):
    _checkpoint(scope, check)
    seconds = _seconds(seconds, "sleep")
    remaining = scope.remaining
    seconds = seconds if remaining is None else min(seconds, remaining)
    (sleep or scope.sleep)(seconds)
    _checkpoint(scope, check)


async def _delay_async(scope, seconds, sleep=None, check=None):
    _checkpoint(scope, check)
    seconds = _seconds(seconds, "sleep")
    remaining = scope.remaining
    seconds = seconds if remaining is None else min(seconds, remaining)
    try:
        if sleep is None:
            await scope.sleep_async(seconds)
        elif inspect.iscoroutinefunction(sleep):
            await sleep(seconds)
        else:
            result = await scope.run_async(sleep, seconds)
            if inspect.isawaitable(result):
                await result
        _checkpoint(scope, check)
    except asyncio.CancelledError as exc:
        raise _stop_cancelled(scope, exc)


async def _call_async(scope, callback, *args):
    _checkpoint(scope)
    try:
        if inspect.iscoroutinefunction(callback):
            with scope.activate():
                value = await callback(*args)
        else:
            value = await scope.run_async(callback, *args)
            if inspect.isawaitable(value):
                with scope.activate():
                    value = await value
        _checkpoint(scope)
        return value
    except asyncio.CancelledError as exc:
        raise _stop_cancelled(scope, exc)


def _point(point, *, frame=None, bounds=None):
    if (not isinstance(point, (tuple, list)) or len(point) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in point)):
        raise InteractionError("invalid_input_point", "input")
    x, y = point
    region = frame.roi if frame is not None else bounds
    if region is not None:
        ox, oy, width, height = region
        if not (ox <= x < ox + width and oy <= y < oy + height):
            raise InteractionError("input_point_outside_roi", "input")
    elif x < 0 or y < 0:
        raise InteractionError("invalid_input_point", "input")
    return x, y


def send_input(callback, *, scope: OperationScope, point=None, frame: Frame | None = None,
               observation: Observation | None = None, sequence=1, check_cancelled=None,
               validate=None, bounds=None, app=None, session: InputSession | None = None) -> InputReceipt:
    """Submit a caller-validated input callback without an implicit mouse move.

    Frame-based callers share InputSession across every input in the operation.
    Legacy callbacks without Frame metadata remain responsible for fresh source
    evidence and may provide explicit client bounds and a final validator.
    """
    receipt = InputReceipt(sequence=sequence, point=point, frame_id=frame.id if frame is not None else None)
    receipt_index = None

    def remember(value):
        nonlocal receipt_index
        if session is not None and value.attempted:
            if receipt_index is None:
                receipt_index = len(session.receipts)
                session.receipts.append(value)
            else:
                session.receipts[receipt_index] = value
        return value

    with scope.activate():
        try:
            _checkpoint(scope, check_cancelled)
            if frame is not None:
                if session is None:
                    raise ValueError("Frame-bound input requires an operation-local InputSession")
                if frame.id in session.consumed_frames:
                    raise InteractionError("stale_input_frame", "input", observation=observation)
                if observation is not None and (observation.state != "present" or observation.frame_id != frame.id):
                    raise InteractionError("invalid_input_observation", "input", observation=observation)
            if point is not None:
                point = _point(point, frame=frame, bounds=bounds)
            identity = _identity(app, scope)
            _checkpoint(scope, check_cancelled)
            if frame is not None and identity is not None and frame.target_identity is None:
                raise InteractionError("input_target_unverified", "input", observation=observation)
            if frame is not None and frame.target_identity is not None and identity != frame.target_identity:
                raise InteractionError("input_target_changed", "input", observation=observation)
            if validate is not None:
                validate()
            _checkpoint(scope, check_cancelled)
            if _identity(app, scope) != identity:
                raise InteractionError("input_target_changed", "input", observation=observation)
            _checkpoint(scope, check_cancelled)
            if session is not None:
                session.sequence += 1
                sequence = session.sequence
                if frame is not None:
                    session.consumed_frames.add(frame.id)
            receipt = remember(replace(receipt, attempted=True, timestamp=time.monotonic(), sequence=sequence,
                                       point=point, target_identity=identity))
            returned = callback()
            if inspect.isawaitable(returned):
                if inspect.iscoroutine(returned):
                    returned.close()
                raise TypeError("Input callbacks must be synchronous; async entry points offload their synchronous input")
            acknowledged = returned if isinstance(returned, bool) else getattr(returned, "success", None)
            acknowledged = None if acknowledged is None else bool(acknowledged)
            receipt = remember(replace(receipt, sent=acknowledged is not False, acknowledged=acknowledged, call_returned=True))
            _checkpoint(scope, check_cancelled)
            if _identity(app, scope) != identity:
                raise InteractionError("input_target_changed", "input", observation=observation, receipts=(receipt,))
            _checkpoint(scope, check_cancelled)
            if acknowledged is False:
                raise InteractionError("input_rejected", "input", observation=observation, receipts=(receipt,))
            return receipt
        except (asyncio.CancelledError, OperationTimeout) as exc:
            raise _attach(_stop_cancelled(scope, exc), observation, (receipt,) if receipt.attempted else (), "input")
        except InteractionError:
            raise
        except Exception as exc:
            receipt = remember(replace(receipt, error=f"{type(exc).__name__}: {exc}"))
            raise InteractionError("input_failed", "input", observation=observation,
                                   receipts=(receipt,) if receipt.attempted else (), message=str(exc)) from exc


def _unknown(value):
    return value is None or value is False or (isinstance(value, Observation) and value.state == "unknown")


def confirm(observe, *, scope: OperationScope, stable_scans=2, poll_seconds=.5, accept=None,
            equivalent=None, is_unknown=None, sleep=None, check_cancelled=None, before_probe=True):
    """Confirm arbitrary domain evidence; callback exceptions propagate unchanged."""
    required, interval = _count(stable_scans, "stable_scans"), _seconds(poll_seconds, "poll_seconds")
    previous, count = None, 0
    try:
        with scope.activate():
            first = True
            while True:
                _checkpoint(scope, check_cancelled)
                if before_probe or not first:
                    _delay(scope, interval, sleep, check_cancelled)
                first = False
                value = observe()
                _checkpoint(scope, check_cancelled)
                if (is_unknown or _unknown)(value) or (accept is not None and not accept(value)):
                    previous, count = None, 0
                else:
                    same = count > 0 and bool(equivalent(previous, value) if equivalent else value == previous)
                    count = count + 1 if same else 1
                    previous = value
                    _checkpoint(scope, check_cancelled)
                    if count >= required:
                        return value
                _checkpoint(scope, check_cancelled)
    except OperationTimeout:
        scope.check_cancelled()
        return None
    except asyncio.CancelledError as exc:
        raise _stop_cancelled(scope, exc)


async def confirm_async(observe, *, scope: OperationScope, stable_scans=2, poll_seconds=.5, accept=None,
                        equivalent=None, is_unknown=None, sleep=None, check_cancelled=None, before_probe=True):
    required, interval = _count(stable_scans, "stable_scans"), _seconds(poll_seconds, "poll_seconds")
    previous, count = None, 0
    try:
        with scope.activate():
            first = True
            while True:
                _checkpoint(scope, check_cancelled)
                if before_probe or not first:
                    await _delay_async(scope, interval, sleep, check_cancelled)
                first = False
                value = await _call_async(scope, observe)
                _checkpoint(scope, check_cancelled)
                if (is_unknown or _unknown)(value) or (accept is not None and not accept(value)):
                    previous, count = None, 0
                else:
                    same = count > 0 and bool(equivalent(previous, value) if equivalent else value == previous)
                    count = count + 1 if same else 1
                    previous = value
                    _checkpoint(scope, check_cancelled)
                    if count >= required:
                        return value
                _checkpoint(scope, check_cancelled)
    except OperationTimeout:
        scope.check_cancelled()
        return None
    except asyncio.CancelledError as exc:
        raise _stop_cancelled(scope, exc)


@dataclass(frozen=True)
class TransitionProbe:
    kind: str = "unknown"
    key: Any = None
    hit: Any = None
    frame: Any = None
    immediate: bool = False
    target_identity: Any = None
    target_bound: bool = False


@dataclass(frozen=True)
class TransitionPolicy:
    max_clicks: int = 1
    allow_retry: bool = False
    settle_seconds: float = 0.
    latch_source_loss: bool = False
    max_unknown: int | None = None

    def __post_init__(self):
        _count(self.max_clicks, "max_clicks")
        _seconds(self.settle_seconds, "settle_seconds")
        if self.max_clicks > 1 and not self.allow_retry:
            raise ValueError("Supplementary clicks require an explicit allow_retry policy")
        if self.max_unknown is not None:
            _count(self.max_unknown, "max_unknown")


@dataclass
class TransitionState:
    """Pass the same state to run and every nested raw probe for loss latching."""
    policy: TransitionPolicy
    submitted: bool = False
    source_locked: bool = False
    unknown_count: int = 0
    last: TransitionProbe | None = None
    inputs: InputSession = field(default_factory=InputSession)
    target_identity: Any = None
    target_bound: bool = False

    @property
    def receipts(self):
        return self.inputs.receipts

    def record(self, value):
        self.last = value
        self.unknown_count = self.unknown_count + 1 if value.kind == "unknown" else 0
        if self.submitted and self.policy.latch_source_loss and value.kind != "source":
            self.source_locked = True

    @property
    def unknown_limit(self):
        return self.policy.max_unknown is not None and self.unknown_count >= self.policy.max_unknown


def _collect_source(allow_source, state):
    # A target-only wait still needs raw source evidence to distinguish a
    # persistent source from its first real disappearance/unknown observation.
    return bool(not state.source_locked and (
        allow_source or (state.submitted and state.policy.latch_source_loss)))


def _target_epoch(scope, state, current=None):
    """The runtime object is a lease target; its bound window epoch also matters."""
    identity = _identity(None, scope)
    if state.target_bound and identity != state.target_identity:
        raise InteractionError("input_target_changed", "observe", observation=state.last,
                               receipts=state.receipts)
    if current is not None and current.target_bound and identity != current.target_identity:
        raise InteractionError("input_target_changed", "input", observation=current,
                               receipts=state.receipts)
    state.target_identity, state.target_bound = identity, True
    scope.checkpoint()
    return identity


@dataclass(frozen=True)
class TransitionResult:
    confirmed: bool
    observation: TransitionProbe | None = None
    receipts: tuple[InputReceipt, ...] = ()
    code: str | None = None
    phase: str = "observe"


def _probe_frame_id(value):
    if isinstance(value.frame, Frame):
        return value.frame.id
    return None


def _probe_step(current, previous, count, seen, *, allow_source, equivalent, required, allow_immediate_source):
    if not isinstance(current, TransitionProbe) or current.kind not in {"source", "target", "unknown"}:
        raise ValueError("Invalid transition observation")
    valid = current.kind == "target" or (allow_source and current.kind == "source")
    if not valid:
        return None, 0, set(), False
    if current.immediate and current.kind == "source" and allow_immediate_source:
        return current, required, seen, True
    same = (previous is not None and current.kind == previous.kind and current.key == previous.key
            and (equivalent is None or equivalent(previous, current)))
    if not same:
        count, seen = 0, set()
    frame_id = _probe_frame_id(current)
    if frame_id is None or frame_id not in seen:
        count += 1
        if frame_id is not None:
            seen.add(frame_id)
    return current, count, seen, count >= required


def probe_transition(observe, *, allow_source, scope: OperationScope, stable_scans=2, poll_seconds=.1,
                     equivalent=None, sleep=None, check_cancelled=None, allow_immediate_source=True,
                     on_observation=None, state: TransitionState | None = None):
    required, interval = _count(stable_scans, "stable_scans"), _seconds(poll_seconds, "poll_seconds")
    state = state if state is not None else TransitionState(TransitionPolicy())
    previous, count, seen = None, 0, set()
    try:
        with scope.activate():
            while True:
                _checkpoint(scope, check_cancelled)
                epoch = _target_epoch(scope, state)
                current = observe(_collect_source(allow_source, state))
                _checkpoint(scope, check_cancelled)
                if not isinstance(current, TransitionProbe):
                    raise ValueError("Invalid transition observation")
                _target_epoch(scope, state)
                current = replace(current, target_identity=epoch, target_bound=True)
                state.record(current)
                if on_observation is not None:
                    on_observation(current)
                previous, count, seen, matched = _probe_step(
                    current, previous, count, seen, allow_source=allow_source and not (state and state.source_locked),
                    equivalent=equivalent, required=required, allow_immediate_source=allow_immediate_source)
                _checkpoint(scope, check_cancelled)
                _target_epoch(scope, state)
                if matched:
                    return current
                if state is not None and state.unknown_limit:
                    return None
                _delay(scope, interval, sleep, check_cancelled)
    except OperationTimeout:
        scope.check_cancelled()
        return None
    except asyncio.CancelledError as exc:
        raise _stop_cancelled(scope, exc)


async def probe_transition_async(observe, *, allow_source, scope: OperationScope, stable_scans=2, poll_seconds=.1,
                                 equivalent=None, sleep=None, check_cancelled=None, allow_immediate_source=True,
                                 on_observation=None, state: TransitionState | None = None):
    required, interval = _count(stable_scans, "stable_scans"), _seconds(poll_seconds, "poll_seconds")
    state = state if state is not None else TransitionState(TransitionPolicy())
    previous, count, seen = None, 0, set()
    try:
        with scope.activate():
            while True:
                _checkpoint(scope, check_cancelled)
                epoch = _target_epoch(scope, state)
                permitted = _collect_source(allow_source, state)
                current = await _call_async(scope, observe, permitted)
                _checkpoint(scope, check_cancelled)
                if not isinstance(current, TransitionProbe):
                    raise ValueError("Invalid transition observation")
                _target_epoch(scope, state)
                current = replace(current, target_identity=epoch, target_bound=True)
                state.record(current)
                if on_observation is not None:
                    on_observation(current)
                previous, count, seen, matched = _probe_step(
                    current, previous, count, seen, allow_source=allow_source and not (state and state.source_locked),
                    equivalent=equivalent, required=required, allow_immediate_source=allow_immediate_source)
                _checkpoint(scope, check_cancelled)
                _target_epoch(scope, state)
                if matched:
                    return current
                if state is not None and state.unknown_limit:
                    return None
                await _delay_async(scope, interval, sleep, check_cancelled)
    except OperationTimeout:
        scope.check_cancelled()
        return None
    except asyncio.CancelledError as exc:
        raise _stop_cancelled(scope, exc)


def _transition_failure(factory, code, state, phase):
    error = factory() if factory is not None else InteractionError(
        code, phase, observation=state.last, receipts=state.receipts)
    return _attach(error, state.last, state.receipts, phase)


def _source_point(source):
    if isinstance(source.hit, Observation):
        return source.hit.client_center
    if isinstance(source.hit, Mapping):
        return source.hit.get("center") or source.hit.get("center_point")
    return getattr(source.hit, "center_point", None)


def _transition_decision(scope, state, observed):
    _target_epoch(scope, state, observed)
    if observed is not None:
        state.record(observed)
    if observed is not None and observed.kind == "target":
        return "target"
    if state.unknown_limit:
        return "unknown_limit"
    if state.source_locked:
        return "target_only"
    return "source" if observed is not None and observed.kind == "source" else "unconfirmed"


def _transition_may_continue(scope, state, attempt):
    return attempt < state.policy.max_clicks or (state.source_locked and scope.deadline is not None)


def _sync_input_callback(callback):
    if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(getattr(callback, "__call__", None)):
        raise ValueError("Transition input callbacks must be synchronous; run_transition_async offloads them")


def run_transition(probe, click, *, scope: OperationScope, policy: TransitionPolicy | None = None,
                   sleep=None, check_cancelled=None, source_unconfirmed=None, exhausted=None,
                   state: TransitionState | None = None):
    policy = policy or (state.policy if state is not None else TransitionPolicy())
    state = state if state is not None else TransitionState(policy)
    if state.policy != policy:
        raise ValueError("Transition state and policy differ")
    _sync_input_callback(click)
    try:
        with scope.activate():
            attempt = 0
            while _transition_may_continue(scope, state, attempt):
                _checkpoint(scope, check_cancelled)
                _target_epoch(scope, state)
                observed = probe(not state.source_locked)
                _checkpoint(scope, check_cancelled)
                decision = _transition_decision(scope, state, observed)
                if decision == "target":
                    return TransitionResult(True, observed, tuple(state.receipts))
                if decision == "target_only":
                    if scope.deadline is None:
                        break
                    _delay(scope, .05, sleep, check_cancelled)
                    continue
                if decision == "unknown_limit":
                    raise _transition_failure(exhausted, "observation_unknown_limit", state, "observe")
                if decision != "source":
                    raise _transition_failure(source_unconfirmed, "source_unconfirmed", state, "source")
                attempt += 1
                state.submitted = True
                _target_epoch(scope, state, observed)
                send_input(lambda: click(observed, attempt), scope=scope,
                           point=_source_point(observed), sequence=attempt,
                           frame=observed.frame if isinstance(observed.frame, Frame) else None,
                           check_cancelled=check_cancelled, session=state.inputs)
                _delay(scope, policy.settle_seconds, sleep, check_cancelled)
                _target_epoch(scope, state)
                observed = probe(False)
                _checkpoint(scope, check_cancelled)
                if _transition_decision(scope, state, observed) == "target":
                    return TransitionResult(True, observed, tuple(state.receipts))
            raise _transition_failure(exhausted, "transition_exhausted", state, "target")
    except (asyncio.CancelledError, OperationTimeout) as exc:
        raise _attach(_stop_cancelled(scope, exc), state.last, state.receipts, "transition")
    except InteractionError as exc:
        raise _attach(exc, state.last, state.receipts, exc.phase)


async def run_transition_async(probe, click, *, scope: OperationScope, policy: TransitionPolicy | None = None,
                               sleep=None, check_cancelled=None, source_unconfirmed=None, exhausted=None,
                               state: TransitionState | None = None):
    policy = policy or (state.policy if state is not None else TransitionPolicy())
    state = state if state is not None else TransitionState(policy)
    if state.policy != policy:
        raise ValueError("Transition state and policy differ")
    _sync_input_callback(click)
    try:
        with scope.activate():
            attempt = 0
            while _transition_may_continue(scope, state, attempt):
                _checkpoint(scope, check_cancelled)
                _target_epoch(scope, state)
                observed = await _call_async(scope, probe, not state.source_locked)
                _checkpoint(scope, check_cancelled)
                decision = _transition_decision(scope, state, observed)
                if decision == "target":
                    return TransitionResult(True, observed, tuple(state.receipts))
                if decision == "target_only":
                    if scope.deadline is None:
                        break
                    await _delay_async(scope, .05, sleep, check_cancelled)
                    continue
                if decision == "unknown_limit":
                    raise _transition_failure(exhausted, "observation_unknown_limit", state, "observe")
                if decision != "source":
                    raise _transition_failure(source_unconfirmed, "source_unconfirmed", state, "source")
                attempt += 1
                state.submitted = True
                _target_epoch(scope, state, observed)
                await scope.run_async(send_input, lambda: click(observed, attempt), scope=scope,
                                      point=_source_point(observed), sequence=attempt,
                                      frame=observed.frame if isinstance(observed.frame, Frame) else None,
                                      check_cancelled=check_cancelled, session=state.inputs)
                await _delay_async(scope, policy.settle_seconds, sleep, check_cancelled)
                _target_epoch(scope, state)
                observed = await _call_async(scope, probe, False)
                _checkpoint(scope, check_cancelled)
                if _transition_decision(scope, state, observed) == "target":
                    return TransitionResult(True, observed, tuple(state.receipts))
            raise _transition_failure(exhausted, "transition_exhausted", state, "target")
    except (asyncio.CancelledError, OperationTimeout) as exc:
        raise _attach(_stop_cancelled(scope, exc), state.last, state.receipts, "transition")
    except InteractionError as exc:
        raise _attach(exc, state.last, state.receipts, exc.phase)


@service_info(alias="interaction", public=True, deps={"app": "app", "observation": "observation"})
class InteractionService:
    def __init__(self, app=None, observation=None):
        self.app = app
        self.observation = observation if observation is not None else ObservationService(app=app)

    confirm = staticmethod(confirm)
    confirm_async = staticmethod(confirm_async)
    wait_value = staticmethod(confirm)
    wait_value_async = staticmethod(confirm_async)
    probe_transition = staticmethod(probe_transition)
    probe_transition_async = staticmethod(probe_transition_async)
    run_transition = staticmethod(run_transition)
    run_transition_async = staticmethod(run_transition_async)
    transition = staticmethod(run_transition)
    transition_async = staticmethod(run_transition_async)

    def _scope(self, scope):
        scope = scope if scope is not None else current_operation_scope()
        target = getattr(self.app, "target_runtime", None)
        if scope is None:
            return OperationScope(target=target)
        if target is not None and scope.target is not None and scope.target is not target:
            raise ValueError("Interaction app and operation scope have different targets")
        return OperationScope(parent=scope, target=target) if target is not None and scope.target is None else scope

    def send_input(self, callback, *, scope, **options):
        return send_input(callback, scope=self._scope(scope), app=self.app, **options)

    async def send_input_async(self, callback, *, scope, **options):
        scope = self._scope(scope)
        session = options.pop("session", None) or InputSession()
        try:
            return await scope.run_async(self.send_input, callback, scope=scope, session=session, **options)
        except (asyncio.CancelledError, OperationTimeout) as exc:
            raise _attach(_stop_cancelled(scope, exc), options.get("observation"), session.receipts, "input")

    def click(self, source: Observation, *, frame: Frame, scope: OperationScope,
              session: InputSession, button="left", move_duration=.2):
        scope = self._scope(scope)
        duration = _seconds(move_duration, "move_duration")
        point = _point(source.client_center, frame=frame)
        if source.state != "present" or source.frame_id != frame.id:
            raise InteractionError("invalid_input_observation", "input", observation=source)
        with scope.activate():
            _checkpoint(scope)
            if frame.id in session.consumed_frames:
                raise InteractionError("stale_input_frame", "input", observation=source)
            identity = _identity(self.app, scope)
            if identity is not None and frame.target_identity is None:
                raise InteractionError("input_target_unverified", "input", observation=source)
            if frame.target_identity is not None and identity != frame.target_identity:
                raise InteractionError("input_target_changed", "input", observation=source)
            _checkpoint(scope)
            self.app.move_to(point[0], point[1], duration=duration)
            _checkpoint(scope)
            return self.send_input(lambda: self.app.click(x=point[0], y=point[1], button=button),
                                   scope=scope, point=point, frame=frame, observation=source, session=session)

    async def click_async(self, source, *, frame, scope, session, button="left", move_duration=.2):
        scope = self._scope(scope)
        try:
            return await scope.run_async(self.click, source, frame=frame, scope=scope,
                                         session=session, button=button, move_duration=move_duration)
        except (asyncio.CancelledError, OperationTimeout) as exc:
            raise _attach(_stop_cancelled(scope, exc), source, session.receipts, "input")

    def find_and_click(self, observe, *, scope, frame=None, button="left", move_duration=.2,
                       stable_scans=1, center_tolerance=None, interval=.2, max_probes=None,
                       after_click_sec=0., state: ClickState | None = None):
        scope = self._scope(scope)
        state = state if state is not None else ClickState()
        after = _seconds(after_click_sec, "after_click_sec")
        source, receipts, phase = Observation("unknown", reason="not_observed"), (), "observe"

        def observing():
            value = observe()
            if isinstance(value, Observation):
                state.observation = value
            return value

        try:
            with scope.activate():
                found = self.observation.wait(observing, lambda value: value.state == "present", scope=scope,
                    stable_scans=stable_scans, center_tolerance=center_tolerance, interval=interval, max_probes=max_probes)
                source = state.observation = found.observation
                if not found.matched:
                    return ClickResult(False, source, code=found.reason or "source_unconfirmed")
                current = frame() if callable(frame) else frame
                if not isinstance(current, Frame):
                    raise InteractionError("input_frame_required", "input", observation=source)
                phase = state.phase = "input"
                receipt = self.click(source, frame=current, scope=scope, session=state.inputs,
                                     button=button, move_duration=move_duration)
                receipts = (receipt,)
                phase = state.phase = "after_input"
                _delay(scope, after)
                return ClickResult(receipt.sent, source, receipts, phase="input_sent")
        except OperationTimeout as exc:
            scope.check_cancelled()
            receipts = tuple(state.inputs.receipts) or receipts or tuple(getattr(exc, "receipts", ()))
            return ClickResult(any(item.sent for item in receipts), source, receipts, "timeout", phase)
        except asyncio.CancelledError as exc:
            raise _attach(_stop_cancelled(scope, exc), source, state.inputs.receipts or receipts or getattr(exc, "receipts", ()), phase)

    async def find_and_click_async(self, observe, *, scope, **options):
        return await self._async_click(self.find_and_click, observe, scope=scope, **options)

    async def _async_click(self, callback, *args, scope, **options):
        scope = self._scope(scope)
        state = options.pop("state", None) or ClickState()
        try:
            return await scope.run_async(callback, *args, scope=scope, state=state, **options)
        except OperationTimeout:
            scope.check_cancelled()
            receipts = tuple(state.inputs.receipts)
            return ClickResult(any(item.sent for item in receipts), state.observation, receipts, "timeout", state.phase)
        except asyncio.CancelledError as exc:
            raise _attach(_stop_cancelled(scope, exc), state.observation, state.inputs.receipts, state.phase)

    def _find_click(self, matcher, *, scope, roi, inspect_result, **click_options):
        current = []

        def observe():
            def recognize(frame):
                current[:] = [frame]
                value = matcher(frame)
                return inspect_result(value, frame) if inspect_result is not None else value
            return self.observation.observe(recognize, scope=scope, roi=roi)

        return self.find_and_click(observe, scope=scope, frame=lambda: current[0] if current else None, **click_options)

    def find_template_and_click(self, template, *, scope, roi=None, threshold=.8, use_grayscale=True,
                                match_method=cv2.TM_CCOEFF_NORMED, preprocess="none", mask=None,
                                inspect=None, **click_options):
        scope = self._scope(scope)
        return self._find_click(lambda frame: self.observation.match_template(
            frame, template, scope=scope, threshold=threshold, use_grayscale=use_grayscale,
            match_method=match_method, preprocess=preprocess, mask=mask),
            scope=scope, roi=roi, inspect_result=inspect, **click_options)

    def find_text_and_click(self, text_to_find, *, scope, roi=None, match_mode="contains", normalize=False,
                            min_confidence=0., recognition_only=False, det_resize_long=None,
                            use_textline_orientation=True, rotate_tall_crops=False, inspect=None, **click_options):
        scope = self._scope(scope)
        return self._find_click(lambda frame: self.observation.find_text(
            frame, text_to_find, scope=scope, match_mode=match_mode, normalize=normalize,
            min_confidence=min_confidence, recognition_only=recognition_only, det_resize_long=det_resize_long,
            use_textline_orientation=use_textline_orientation, rotate_tall_crops=rotate_tall_crops),
            scope=scope, roi=roi, inspect_result=inspect, **click_options)

    async def find_template_and_click_async(self, template, *, scope, **options):
        return await self._async_click(self.find_template_and_click, template, scope=scope, **options)

    async def find_text_and_click_async(self, text_to_find, *, scope, **options):
        return await self._async_click(self.find_text_and_click, text_to_find, scope=scope, **options)
