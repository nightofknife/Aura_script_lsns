"""PC-local observation and bounded supplementary-click execution.

Callers own page recognition, stability keys, input points, timing and errors.
Unknown observations reset confirmation; they never authorize input or success.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TransitionProbe:
    kind: str = "unknown"
    key: Any = None
    hit: Any = None
    frame: Any = None
    immediate: bool = False


async def wait_for_transition_probe(observe, *, allow_source, timeout_seconds,
                                    poll_seconds, monotonic, sleep, check_cancelled,
                                    equivalent=None):
    """Give each probe its own budget; require two equivalent valid observations."""
    deadline = monotonic() + timeout_seconds
    previous, stable = None, 0
    while monotonic() < deadline:
        check_cancelled()
        current = observe(allow_source)
        check_cancelled()
        # Synchronous capture/recognition may consume the remaining budget.
        if monotonic() >= deadline:
            break
        if current.kind not in {"source", "target", "unknown"}:
            raise ValueError("Invalid transition observation")
        valid = current.kind == "target" or (allow_source and current.kind == "source")
        if valid:
            if current.immediate and current.kind == "source":
                stable = 2
            else:
                same = (previous is not None and current.kind == previous.kind
                        and current.key == previous.key
                        and (equivalent is None or equivalent(previous, current)))
                stable = stable + 1 if same else 1
                previous = current
            if stable >= 2:
                check_cancelled()
                # Domain frame comparison is also part of confirmation work.
                if monotonic() >= deadline:
                    break
                return current
        else:
            previous, stable = None, 0
        await sleep(poll_seconds)
    check_cancelled()
    return None


async def run_template_transition(probe, click, *, max_clicks, settle_seconds,
                                  sleep, check_cancelled, source_unconfirmed,
                                  exhausted):
    """Reconfirm and relocate the source for every attempt; target always wins."""
    for attempt in range(1, max_clicks + 1):
        check_cancelled()
        observed = await probe(True)
        check_cancelled()
        if observed is not None and observed.kind == "target":
            return
        if observed is None or observed.kind != "source":
            raise source_unconfirmed()
        check_cancelled()
        click(observed, attempt)
        await sleep(settle_seconds)
        check_cancelled()
        observed = await probe(False)
        check_cancelled()
        if observed is not None and observed.kind == "target":
            return
    raise exhausted()
