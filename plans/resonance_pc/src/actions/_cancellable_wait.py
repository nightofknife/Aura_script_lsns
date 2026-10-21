"""PC-only UI waits with caller-owned cancellation and timing policies."""
from __future__ import annotations

from typing import Awaitable, Callable


def sleep_slices(seconds: float, *, sleep: Callable[[float], None],
                 check_cancelled: Callable[[], None]) -> None:
    """Wait the requested slices, checking before and after each input-free slice."""
    remaining = max(float(seconds), 0.0)
    check_cancelled()
    while remaining > 0:
        interval = min(remaining, 0.05)
        sleep(interval)
        check_cancelled()
        remaining -= interval


async def async_sleep_slices(seconds: float, *, sleep: Callable[[float], Awaitable[None]],
                             check_cancelled: Callable[[], None]) -> None:
    """Async counterpart of the requested-duration policy."""
    remaining = max(float(seconds), 0.0)
    check_cancelled()
    while remaining > 0:
        interval = min(remaining, 0.05)
        await sleep(interval)
        check_cancelled()
        remaining -= interval


def sleep_until_deadline(seconds: float, *, sleep: Callable[[float], None],
                         monotonic: Callable[[], float],
                         check_cancelled: Callable[[], None]) -> None:
    """Count wall-clock time, including cancellation-check overhead, toward a wait."""
    deadline = monotonic() + max(float(seconds), 0.0)
    while True:
        check_cancelled()
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(0.05, remaining))


def sleep_before_deadline(until: float, *, sleep: Callable[[float], None],
                          monotonic: Callable[[], float],
                          check_cancelled: Callable[[], None]) -> None:
    """Wait under an existing operation deadline, including its final guard."""
    while monotonic() < until:
        check_cancelled()
        sleep(max(0.0, min(0.05, until - monotonic())))
    check_cancelled()
