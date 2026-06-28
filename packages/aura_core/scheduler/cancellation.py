"""Cooperative task cancellation helpers.

The scheduler can cancel an asyncio task, but synchronous actions run in a
thread pool and cannot be forcibly interrupted by Python.  These helpers give
long-running actions a lightweight way to notice that their current CID was
cancelled and stop themselves at safe checkpoints.
"""

from __future__ import annotations

import threading
from typing import Optional

from packages.aura_core.observability.logging.core_logger import current_cid


_cancelled_cids: set[str] = set()
_lock = threading.RLock()


def _normalize_cid(cid: Optional[str]) -> str:
    return str(cid or "").strip()


def request_task_cancel(cid: Optional[str]) -> bool:
    normalized = _normalize_cid(cid)
    if not normalized or normalized == "-":
        return False
    with _lock:
        _cancelled_cids.add(normalized)
    return True


def clear_task_cancel(cid: Optional[str]) -> None:
    normalized = _normalize_cid(cid)
    if not normalized:
        return
    with _lock:
        _cancelled_cids.discard(normalized)


def is_task_cancel_requested(cid: Optional[str]) -> bool:
    normalized = _normalize_cid(cid)
    if not normalized:
        return False
    with _lock:
        return normalized in _cancelled_cids


def is_current_task_cancel_requested() -> bool:
    return is_task_cancel_requested(current_cid())
