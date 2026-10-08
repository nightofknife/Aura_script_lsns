"""Optional, best-effort observations; never a game confirmation authority.

Child observations retain the owning phase's stage and use state=progress.
Their own state is started/progress/completed/failed/blocked/skipped/cancelled;
completed means only the described existing boundary returned successfully.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from packages.aura_core.observability.logging.core_logger import logger


_OBSERVER: ContextVar[Callable[..., None] | None] = ContextVar(
    "resonance_pc_operation_observer", default=None,
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def operation_progress(observer: Callable[..., None] | None) -> Iterator[None]:
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def observe_operation(key: str, label: str, state: str, **detail: Any) -> None:
    observer = _OBSERVER.get()
    if observer is not None:
        try:
            observer({"key": key, "label": label, "state": state, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - display must never change execution
            logger.warning("PC operation observation could not be delivered: %s", exc)


def observe_worker_future(future: Any) -> None:
    """Consume a fire-and-forget observation failure without blocking a worker."""
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PC operation progress could not be published: %s", exc)
