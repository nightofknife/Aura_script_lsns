"""PC observation budgets; late evidence never authorizes success or input.

Callers retain their own recognition, stability, error and cancellation policies.
An explicitly requested zero-timeout first probe is the only budget exemption.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ObservationBudget:
    def __init__(self, timeout_seconds, *, monotonic, check_cancelled,
                 immediate_first_probe=False, deadline_limit=None):
        seconds = max(float(timeout_seconds), 0.0)
        self.monotonic = monotonic
        self.check_cancelled = check_cancelled
        self.deadline = monotonic() + seconds
        self.deadline_limit = deadline_limit
        if deadline_limit is not None:
            self.deadline = min(self.deadline, deadline_limit)
        self.immediate = seconds == 0 and immediate_first_probe

    def active(self):
        """Cancellation wins even when capture/comparison has also timed out."""
        self.check_cancelled()
        now = self.monotonic()
        # A local zero-timeout probe must never bypass its enclosing operation.
        return ((self.deadline_limit is None or now < self.deadline_limit)
                and (self.immediate or now < self.deadline))

    def pause_seconds(self, interval):
        return max(0.0, min(float(interval), self.deadline - self.monotonic()))


@dataclass(frozen=True)
class WaitResult:
    value: Any = None
    matched: bool = False
    # A caller may retain timely nonterminal evidence for its retry policy.
    # Late raw observations remain diagnostic-only in value.
    in_time: bool = False


def wait_for_observation(*, observe, accept, budget, poll_seconds, sleep):
    value, in_time = None, False
    while budget.active():
        value = observe()
        in_time = budget.active()
        if not in_time:
            break
        accepted = accept(value)
        # Comparison and caller-specific classification are part of the budget.
        in_time = budget.active()
        if not in_time:
            break
        if accepted:
            return WaitResult(value, True, True)
        if budget.immediate:
            break
        sleep(budget.pause_seconds(poll_seconds))
    budget.check_cancelled()
    return WaitResult(value, in_time=in_time)


async def async_wait_for_observation(*, observe, accept, budget, poll_seconds, sleep):
    value, in_time = None, False
    while budget.active():
        value = observe()
        in_time = budget.active()
        if not in_time:
            break
        accepted = accept(value)
        in_time = budget.active()
        if not in_time:
            break
        if accepted:
            return WaitResult(value, True, True)
        if budget.immediate:
            break
        await sleep(budget.pause_seconds(poll_seconds))
    budget.check_cancelled()
    return WaitResult(value, in_time=in_time)
