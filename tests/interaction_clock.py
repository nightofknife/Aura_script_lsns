"""Deterministic operation clocks for offline interaction contract tests."""
from contextlib import contextmanager
from contextvars import ContextVar

from packages.aura_core.scheduler import operation_scope

_CLOCK = ContextVar("test_interaction_clock", default=None)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def advance(seconds):
    clock = _CLOCK.get()
    if clock is None:
        raise RuntimeError("A test interaction clock must be installed")
    clock.advance(seconds)


@contextmanager
def install_clock(monkeypatch, *modules):
    clock = Clock()
    base = operation_scope.OperationScope

    class ClockScope(base):
        def __init__(self, timeout=None, **kwargs):
            kwargs.setdefault("monotonic", clock.monotonic)
            super().__init__(timeout, **kwargs)

    for module in modules:
        monkeypatch.setattr(module, "OperationScope", ClockScope)
    monkeypatch.setattr(operation_scope.time, "sleep", clock.advance)
    token = _CLOCK.set(clock)
    try:
        yield clock
    finally:
        _CLOCK.reset(token)
