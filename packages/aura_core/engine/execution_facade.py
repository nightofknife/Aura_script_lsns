"""Context-bound execution of already evaluated Python values.

This handle provides action injection/middleware and full child task lifecycle.
It deliberately adds no node retries, loops, rendering or input replay.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, cancel_for_deadline

if TYPE_CHECKING:
    from packages.aura_core.context.execution import ExecutionContext
    from .execution_engine import ExecutionEngine


async def _await_scoped(awaitable, scope: OperationScope):
    """Cancel the actual child on expiry/stop and await its complete finally.

    Repeated caller cancellation cannot abandon child task finalization. A
    synchronous worker may still be isolated by OperationScope's existing drain
    policy; this helper does not pretend to forcibly stop such a worker.
    """
    try:
        scope.checkpoint()
    except BaseException:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise
    task = asyncio.ensure_future(awaitable)
    owner = asyncio.current_task()
    cancel_count = owner.cancelling() if owner is not None else 0
    failure = None
    result = None
    try:
        while True:
            remaining = scope.remaining
            done, _ = await asyncio.wait({task}, timeout=.05 if remaining is None else min(.05, remaining))
            if done:
                if task.cancelled():
                    task.result()  # Preserve the child's native cancellation object/evidence.
                scope.checkpoint()
                result = task.result()
                scope.checkpoint()
                return result
            scope.checkpoint()
    except asyncio.CancelledError as exc:
        failure = scope.cancellation_error(exc)
        cancel_count = owner.cancelling() if owner is not None else 0
    except OperationTimeout as exc:
        failure = exc

    if not task.done():
        if isinstance(failure, OperationTimeout):
            cancel_for_deadline(task, failure)
        else:
            task.cancel()
    child_error = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            current_count = owner.cancelling() if owner is not None else 0
            if current_count > cancel_count:
                failure = scope.cancellation_error(exc)
                cancel_count = current_count
            if task.done():
                child_error = exc
                break
            # This cancellation came from the caller while the child was
            # already cleaning up. Keep waiting and preserve cancellation over
            # an earlier deadline failure.
            failure = scope.cancellation_error(exc)
        except Exception as exc:
            child_error = exc
            break

    from packages.aura_core.observability.failure_evidence import attach_evidence, evidence_from

    attach_evidence(failure, evidence_from(failure) + evidence_from(child_error) + evidence_from(result))
    if isinstance(failure, OperationTimeout):
        scope.check_cancelled()
    raise failure


class ExecutionFacade:
    """A restricted handle bound to one engine and execution context.

    Obtain it with ``engine.bind_execution(context)``. Keep it in the calling
    operation; never store it in singleton services or context.data.
    """

    __slots__ = ("_engine", "_context")

    def __init__(self, engine: ExecutionEngine, context: ExecutionContext):
        if context is None:
            raise ValueError("ExecutionFacade requires the current execution context")
        self._engine = engine
        self._context = context

    def _injector(self):
        from .action_injector import ActionInjector

        orchestrator = self._engine.orchestrator
        return ActionInjector(
            self._context, self._engine, None, self._engine.services,
            current_package=getattr(orchestrator, "loaded_package", None),
            service_resolver=getattr(orchestrator, "resolve_service", None),
        )

    async def call_action(self, action_ref: str, values: dict[str, Any] | None = None, *,
                          scope: OperationScope | None = None) -> Any:
        """Invoke typed values unchanged, using normal service injection and middleware."""
        return await self._injector().execute_typed(action_ref, {} if values is None else values, scope=scope)

    async def run_task(self, task_ref: str, inputs: dict[str, Any] | None = None, *,
                       scope: OperationScope | None = None,
                       result_assertions: dict[str, Any] | None = None) -> Any:
        """Run a complete canonical child task and return its checked user_data."""
        return await self._injector().run_task_typed(
            task_ref, inputs, scope=scope, result_assertions=result_assertions,
        )
