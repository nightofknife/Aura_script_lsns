"""Per-operation budgets, cooperative worker drain and target exclusion.

Cancelling an asyncio waiter cannot stop its synchronous worker. A target lease
therefore belongs to the actual worker, not to the task awaiting its result.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import threading
import time
import weakref

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.logging.core_logger import current_cid, reset_cid, set_cid
from packages.aura_core.observability.runtime_trace import (
    log_event, note_cancel_observed, tracked_to_thread,
)
from .cancellation import (
    begin_sync_action, end_sync_action, is_task_cancel_requested, request_task_cancel,
)


class OperationTimeout(TimeoutError):
    """The operation's total budget has expired; late results are unusable."""


@dataclass(frozen=True)
class _DeadlineCancellation:
    error: OperationTimeout


def cancel_for_deadline(task, error: OperationTimeout):
    """Interrupt a child for cleanup without requesting user cancellation."""
    task.cancel(_DeadlineCancellation(error))


class TargetOperationBusy(RuntimeError):
    """Another operation still owns this target, possibly after drain timeout."""

    def __init__(self, *, quarantined=False):
        self.quarantined = quarantined
        self.code = "target_quarantined" if quarantined else "target_operation_busy"
        super().__init__(self.code)


class _StopToken:
    def __init__(self):
        self.cancelled = threading.Event()
        self.error = None


@dataclass
class _TargetState:
    reference: weakref.ReferenceType
    owner: object
    users: int = 0
    quarantined: bool = False


_target_lock = threading.RLock()
_targets: dict[int, _TargetState] = {}
_current_scope: ContextVar[OperationScope | None] = ContextVar("operation_scope", default=None)
_current_owner: ContextVar[object | None] = ContextVar("operation_target_owner", default=None)


def current_operation_scope():
    """Return the explicitly bound operation, including inside tracked workers."""
    return _current_scope.get()


def _forget_target(key, reference):
    with _target_lock:
        state = _targets.get(key)
        if state is not None and state.reference is reference:
            _targets.pop(key, None)


class _TargetLease:
    def __init__(self, target, owner):
        self.key = None if target is None else id(target)
        self.state = None
        self.released = False
        if target is None:
            return
        with _target_lock:
            state = _targets.get(self.key)
            if state is not None and state.reference() is not target:
                _targets.pop(self.key, None)
                state = None
            if state is not None and (state.quarantined or state.owner is not owner):
                raise TargetOperationBusy(quarantined=state.quarantined)
            if state is None:
                key = self.key
                reference = weakref.ref(target, lambda ref: _forget_target(key, ref))
                state = _TargetState(reference, owner)
                _targets[key] = state
            state.users += 1
            self.state = state

    def release(self):
        with _target_lock:
            if self.released:
                return
            self.released = True
            if self.state is not None:
                self.state.users -= 1
                if self.state.users == 0 and _targets.get(self.key) is self.state:
                    _targets.pop(self.key, None)

    def quarantine(self):
        with _target_lock:
            if not self.released and self.state is not None:
                self.state.quarantined = True
                return True
            return False


@contextmanager
def target_operation_guard(target, *, cleanup=False):
    """Reject a busy/stopped target at runtime entry; releases always remain legal.

    Cleanup must only release existing held input on the already bound session;
    it must not rebind or issue fresh input. A lease is held across the call so
    another operation cannot pass the guard while this operation is in flight.
    """
    if cleanup:
        yield
        return
    scope = _current_scope.get()
    if scope is not None:
        if scope.target is not None and scope.target is not target:
            raise ValueError("Operation scope and runtime target differ")
        scope.checkpoint()
    owner = scope._stop if scope is not None else (_current_owner.get() or object())
    lease = _TargetLease(target, owner)
    token = _current_owner.set(owner)
    try:
        if scope is not None:
            scope.checkpoint()
        yield
    finally:
        _current_owner.reset(token)
        lease.release()


def _seconds(value, name):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return seconds


class OperationScope:
    """A value owned by one operation; children share stop state and total budget.

    ``timeout=None`` adds no local deadline. Zero expires immediately; legacy
    one-probe adapters should use no local deadline and limit their probe count.
    Targets are runtime service objects, not CID strings or transient frames.
    """

    def __init__(self, timeout: float | None = None, *, parent=None, cid=None,
                 target=None, cancel_check=None, monotonic=time.monotonic):
        self._monotonic = parent._monotonic if parent is not None else monotonic
        self._stop = parent._stop if parent is not None else _StopToken()
        self.cid = str(cid if cid is not None else (parent.cid if parent is not None else current_cid()))
        self.target = target if target is not None else (parent.target if parent is not None else None)
        self._cancel_check = cancel_check if cancel_check is not None else (
            parent._cancel_check if parent is not None else None
        )
        deadline = None if timeout is None else self._now() + _seconds(timeout, "timeout")
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("Operation deadline must be finite")
        inherited = parent.deadline if parent is not None else None
        self._deadline = inherited if deadline is None else (
            deadline if inherited is None else min(deadline, inherited)
        )

    def _now(self):
        now = float(self._monotonic())
        if not math.isfinite(now):
            raise ValueError("monotonic clock must return a finite value")
        return now

    @property
    def deadline(self):
        return self._deadline

    @property
    def remaining(self):
        return None if self.deadline is None else max(0.0, self.deadline - self._now())

    @property
    def expired(self):
        return self.deadline is not None and self._now() >= self.deadline

    @property
    def cancelled(self):
        requested = self._stop.cancelled.is_set() or is_task_cancel_requested(self.cid)
        if not requested and self._cancel_check is not None:
            try:
                requested = bool(self._cancel_check())
            except asyncio.CancelledError as exc:
                self._stop.error = exc
                self._stop.cancelled.set()
                requested = True
        if requested:
            self._stop.cancelled.set()
        return requested

    def child(self, timeout=None):
        return type(self)(timeout, parent=self)

    def cancel(self):
        """Latch cancellation for this operation and its children, even without a CID."""
        self._stop.cancelled.set()
        request_task_cancel(self.cid)

    def cancellation_error(self, error: asyncio.CancelledError):
        """Preserve the source of an interruption through nested async cleanup.

        Only our explicit deadline marker is a timeout. An unmarked task.cancel,
        a child's own cancellation, or an already requested user stop still
        cancels the shared operation, even when a deadline has also expired.
        """
        marker = error.args[0] if error.args else None
        if isinstance(marker, _DeadlineCancellation):
            if not self.cancelled:
                return marker.error
            self.cancel()
            return self._stop.error or asyncio.CancelledError("Operation cancelled")
        self.cancel()
        if self._stop.error is None:
            self._stop.error = error
        return error

    def check_cancelled(self):
        if self.cancelled:
            note_cancel_observed(self.cid)
            raise self._stop.error or asyncio.CancelledError("Operation cancelled")

    def checkpoint(self):
        self.check_cancelled()
        if self.expired:
            raise OperationTimeout("Operation deadline expired")

    @contextmanager
    def activate(self):
        """Bind this scope and exclude unrelated use of its target synchronously."""
        self.checkpoint()
        lease = _TargetLease(self.target, self._stop)
        token = _current_scope.set(self)
        owner_token = _current_owner.set(self._stop)
        cid_token = set_cid(self.cid)
        try:
            self.checkpoint()
            yield self
        finally:
            reset_cid(cid_token)
            _current_owner.reset(owner_token)
            _current_scope.reset(token)
            lease.release()

    def sleep(self, seconds):
        until = self._now() + _seconds(seconds, "seconds")
        while True:
            self.checkpoint()
            remaining = until - self._now()
            if remaining <= 0:
                return
            time.sleep(min(remaining, self.remaining if self.remaining is not None else remaining, 0.05))

    async def sleep_async(self, seconds):
        until = self._now() + _seconds(seconds, "seconds")
        try:
            while True:
                self.checkpoint()
                remaining = until - self._now()
                if remaining <= 0:
                    return
                await asyncio.sleep(min(remaining, self.remaining if self.remaining is not None else remaining, 0.05))
        except asyncio.CancelledError as exc:
            raise self.cancellation_error(exc)

    async def _drain(self, completed, lease):
        try:
            grace = _seconds(get_config_value("execution.sync_action_cancel_grace_sec", 2.0), "cancel grace")
        except (TypeError, ValueError):
            grace = 2.0
        until = time.monotonic() + grace
        waiter = asyncio.create_task(completed.wait())
        try:
            while not completed.is_set():
                remaining = until - time.monotonic()
                if remaining <= 0:
                    quarantined = lease.quarantine()
                    log_event("operation.drain_timeout", cid=self.cid, grace_sec=grace,
                              target_quarantined=quarantined)
                    return
                try:
                    await asyncio.wait({waiter}, timeout=remaining)
                except asyncio.CancelledError as exc:
                    # Repeated cancellation never shortens cleanup or extends grace.
                    self.cancellation_error(exc)
            log_event("operation.drained", cid=self.cid)
        finally:
            waiter.cancel()

    async def run_async(self, call, /, *args, **kwargs):
        """Run one synchronous callback with trace, total budget and actual drain.

        Exceptions are retrieved even after drain timeout. Target exclusion ends
        only in the worker's finally block, never merely because its waiter left.
        """
        self.checkpoint()
        lease = _TargetLease(self.target, self._stop)
        completed = asyncio.Event()
        started = threading.Event()
        worker_exited = threading.Event()
        dispatched = False
        finish_lock = threading.Lock()
        finished = False
        task = None
        begin_sync_action(self.cid)

        def finish():
            nonlocal finished
            with finish_lock:
                if finished:
                    return
                finished = True
            lease.release()
            end_sync_action(self.cid)

        def invoke():
            started.set()
            try:
                with self.activate():
                    self.checkpoint()
                    try:
                        value = call(*args, **kwargs)
                    except Exception:
                        self.checkpoint()
                        raise
                    self.checkpoint()
                    return value
            finally:
                worker_exited.set()
                if task is not None and task.cancelled():
                    # An externally cancelled dispatcher cannot attest to trace
                    # completion; only the actual callback may release its lease.
                    finish()

        async def dispatch():
            nonlocal dispatched
            dispatched = True
            cid_token = set_cid(self.cid)
            try:
                return await tracked_to_thread(invoke)
            except Exception:
                if not started.is_set():
                    finish()  # Executor submission failed, so no worker owns it.
                raise
            finally:
                reset_cid(cid_token)

        def retrieve(task):
            if not dispatched:
                finish()  # Cancelled before the dispatch coroutine ever started.
                completed.set()
            elif not task.cancelled() or not task.cancelling():
                if not task.cancelled():
                    task.exception()
                # The callback's finally runs inside tracked_to_thread's own
                # trace lifetime. Normal drain must also wait for that outer
                # worker bookkeeping and exception delivery to finish. A worker
                # raising CancelledError is also normal completion of that
                # trace; it differs from explicitly cancelling the dispatcher.
                finish()
                completed.set()
            elif worker_exited.is_set():
                finish()
                # No normal completion signal: the cancelled dispatcher cannot
                # prove its underlying trace wrapper has finished. Drain remains
                # conservative and bounded by the existing cleanup grace.

        try:
            task = asyncio.create_task(dispatch())
        except BaseException:
            finish()
            raise
        task.add_done_callback(retrieve)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.remaining)
            if not done:
                self.checkpoint()
                raise OperationTimeout("Operation worker exceeded its deadline")
            self.checkpoint()
            value = task.result()
            self.checkpoint()
            return value
        except asyncio.CancelledError as exc:
            failure = self.cancellation_error(exc)
            await self._drain(completed, lease)
            if isinstance(failure, OperationTimeout):
                self.check_cancelled()
            raise failure
        except OperationTimeout:
            await self._drain(completed, lease)
            self.check_cancelled()
            raise
