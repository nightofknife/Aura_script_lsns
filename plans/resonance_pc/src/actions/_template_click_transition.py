"""Compatibility names for public framework transitions; no local poll loop."""
from __future__ import annotations

from packages.aura_core.scheduler.operation_scope import (
    OperationScope, OperationTimeout, current_operation_scope,
)
from ....aura_base.src.services.interaction_service import (
    TransitionPolicy, TransitionProbe, probe_transition_async, run_transition_async,
)


async def wait_for_transition_probe(observe, *, allow_source, timeout_seconds,
                                    poll_seconds, monotonic, sleep, check_cancelled,
                                    equivalent=None, scope=None, interaction=None):
    parent = scope if scope is not None else current_operation_scope()
    budget = OperationScope(timeout_seconds, parent=parent, monotonic=monotonic)
    probe = interaction.probe_transition_async if interaction is not None else probe_transition_async
    try:
        return await probe(
            observe, allow_source=allow_source, scope=budget, stable_scans=2,
            poll_seconds=poll_seconds, equivalent=equivalent, sleep=sleep,
            check_cancelled=check_cancelled, allow_immediate_source=True,
        )
    except OperationTimeout:
        budget.check_cancelled()
        check_cancelled()
        return None


async def run_template_transition(probe, click, *, max_clicks, settle_seconds,
                                  sleep, check_cancelled, source_unconfirmed,
                                  exhausted, scope=None, interaction=None):
    budget = scope if scope is not None else current_operation_scope()
    if budget is None:
        raise ValueError("Template transition requires a total operation scope")
    run = interaction.run_transition_async if interaction is not None else run_transition_async
    try:
        await run(
            probe, click, scope=budget,
            policy=TransitionPolicy(max_clicks=max_clicks, allow_retry=max_clicks > 1,
                                    settle_seconds=settle_seconds),
            sleep=sleep, check_cancelled=check_cancelled,
            source_unconfirmed=source_unconfirmed, exhausted=exhausted,
        )
    except OperationTimeout as exc:
        budget.check_cancelled()
        check_cancelled()
        error = exhausted()
        error.last_observation = getattr(exc, "last_observation", None)
        error.receipts = tuple(getattr(exc, "receipts", ()))
        error.phase = getattr(exc, "phase", "transition")
        raise error from None
