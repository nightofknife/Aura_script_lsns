"""Process-local cooperative battle stop flags, shared by runner IPC threads.

These flags do not cancel an asyncio task or its input operation scope. Only a
registered battle operation may perform its normal, bounded native-stop cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time


_PENDING_LIMIT = 128
_PENDING_TTL_SECONDS = 600.0
_lock = threading.RLock()


@dataclass
class _Control:
    stop: threading.Event = field(default_factory=threading.Event)
    active: bool = False
    created_at: float = field(default_factory=time.monotonic)


_controls: dict[str, _Control] = {}


def _cid(value: str) -> str:
    cid = str(value or "").strip()
    if not cid:
        raise ValueError("Battle control requires a task CID")
    return cid


def _prune_pending() -> None:
    now = time.monotonic()
    for cid, control in tuple(_controls.items()):
        if not control.active and now - control.created_at > _PENDING_TTL_SECONDS:
            del _controls[cid]


def register_control(cid: str) -> None:
    """Register at root task entry without discarding a queued stop request."""
    cid = _cid(cid)
    with _lock:
        _prune_pending()
        control = _controls.setdefault(cid, _Control())
        control.active = True


def request_stop(cid: str, *, allow_pending: bool = False) -> bool:
    """Set the flag; pending creation is reserved for validated runner requests."""
    cid = _cid(cid)
    with _lock:
        _prune_pending()
        control = _controls.get(cid)
        if control is None:
            if not allow_pending:
                return False
            if sum(not value.active for value in _controls.values()) >= _PENDING_LIMIT:
                return False
            control = _controls[cid] = _Control()
        control.stop.set()
        return True


def stop_requested(cid: str) -> bool:
    with _lock:
        control = _controls.get(_cid(cid))
        return control is not None and control.stop.is_set()


def release_control(cid: str) -> None:
    """Release only after the root operation has finished its cleanup."""
    with _lock:
        _controls.pop(_cid(cid), None)
