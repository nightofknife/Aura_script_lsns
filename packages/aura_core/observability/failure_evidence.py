"""Best-effort failure evidence; platform code supplies PNG bytes, never Core."""
from __future__ import annotations

import asyncio
import contextvars
import json
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from .logging.core_logger import logger
from .logging.log_session import get_log_session

failure_scope: contextvars.ContextVar[dict] = contextvars.ContextVar("failure_scope", default={})
# Owned by the submit task, not ExecutionContext/deepcopy. Only the root task's
# timeout completion is deferred until asyncio.timeout classifies cancellation.
task_completion_scope: contextvars.ContextVar[dict | None] = contextvars.ContextVar("task_completion_scope", default=None)
_provider: Callable[[dict], dict] | None = None
_capture_slot = threading.Lock()


def save_evidence_png(png: bytes, *, evidence_id: str | None = None) -> dict:
    """Save supplied pixels under the current leased session, without capturing.

    Diagnostic producers use the same ownership/path contract as terminal errors.
    This function never starts a session or performs retention during a task.
    """
    session = get_log_session()
    ref = {"evidence_id": evidence_id or uuid4().hex[:16],
           "session_id": session.session_id if session else None}
    if session is None or not session.available:
        return {**ref, "status": "skipped", "reason": "log_session_unavailable"}
    if not isinstance(png, bytes) or not png or len(png) > 10 * 1024 * 1024:
        return {**ref, "status": "failed", "reason": "invalid_png_payload"}
    from .logging.log_session import safe_managed_path
    relative = f"errors/{session.session_id}/{ref['evidence_id']}.png"
    created = False
    path = None
    try:
        path = safe_managed_path(session.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = safe_managed_path(session.root, relative)
        with path.open("xb") as output:
            created = True
            output.write(png)
    except Exception as exc:
        if created and path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return {**ref, "status": "failed", "reason": str(exc)}
    return {**ref, "status": "saved", "screenshot": relative}


def register_capture_provider(provider: Callable[[dict], dict]) -> None:
    global _provider
    _provider = provider


def evidence_from(value: Any) -> list[dict]:
    """Read explicit evidence only; never infer failure from arbitrary results."""
    found: dict[str, dict] = {}
    visited: set[int] = set()
    def visit(item: Any) -> None:
        if item is None or id(item) in visited:
            return
        visited.add(id(item))
        refs = item.get("failure_evidence") if isinstance(item, dict) else getattr(item, "failure_evidence", None)
        for ref in refs if isinstance(refs, list) else []:
            if isinstance(ref, dict) and isinstance(ref.get("evidence_id"), str):
                found.setdefault(ref["evidence_id"], dict(ref))
        if isinstance(item, BaseException):
            visit(item.__cause__)
            visit(item.__context__)
            for name in ("detail", "details"):
                visit(getattr(item, name, None))
        elif isinstance(item, dict):
            # These are known failure envelopes, not a recursive result search.
            visit(item.get("error"))
            visit(item.get("detail"))
            visit(item.get("details"))
    visit(value)
    return list(found.values())


def attach_evidence(value: Any, refs: list[dict]) -> None:
    merged = {ref["evidence_id"]: ref for ref in evidence_from(value) + refs}
    if not merged:
        return
    if isinstance(value, dict):
        value["failure_evidence"] = list(merged.values())
    else:
        try:
            value.failure_evidence = list(merged.values())
        except Exception:
            pass


async def capture_failure(value: Any, *, origin: str = "framework_fallback",
                          terminal: bool = False, metadata: dict | None = None,
                          timeout_sec: float = 2.0) -> list[dict]:
    """Capture once per error chain. A timed-out daemon retains its slot until done."""
    existing = evidence_from(value)
    if existing:
        return existing
    scope = dict(failure_scope.get())
    if scope.get("ancestor_may_retry") or (scope.get("may_retry") and not terminal):
        return []
    from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
    # Synchronous action timeouts also request cooperative cancellation of the
    # worker. That cleanup signal is not a user cancellation of the failed node.
    if isinstance(value, asyncio.CancelledError) or (
        is_current_task_cancel_requested() and not isinstance(value, TimeoutError)
    ):
        return []
    if getattr(value, "success", False) is True:
        return []
    if getattr(value, "severity", None) in {"signal", "success"}:
        return []
    scope.update(metadata or {})
    session = get_log_session()
    ref = {"evidence_id": uuid4().hex[:16], "session_id": session.session_id if session else None,
           "origin": origin, "scene_may_have_changed": origin != "source",
           "captured_at": datetime.now(timezone.utc).isoformat(),
           **{key: scope.get(key) for key in ("cid", "parent_cid", "task", "node", "plan",
                                             "execution_count", "retry_count", "stage")}}
    ref["error_code"] = scope.get("error_code") or getattr(value, "code", type(value).__name__)
    # Attach before awaiting, so wrapping/propagation cannot start a second attempt.
    attach_evidence(value, [ref])
    provider = _provider
    if not session or not session.available:
        ref.update(status="skipped", reason="log_session_unavailable")
    elif provider is None:
        ref.update(status="skipped", reason="capture_provider_unavailable")
    elif not _capture_slot.acquire(blocking=False):
        ref.update(status="skipped", reason="capture_busy")
    else:
        responses: queue.Queue = queue.Queue(maxsize=1)
        context = contextvars.copy_context()
        def collect() -> None:
            try:
                responses.put((True, context.run(provider, scope)))
            except Exception as exc:
                responses.put((False, str(exc)))
            finally:
                _capture_slot.release()
        deadline = asyncio.get_running_loop().time() + max(0.05, timeout_sec)
        try:
            try:
                threading.Thread(target=collect, name="aura-failure-capture", daemon=True).start()
            except Exception:
                _capture_slot.release()
                raise
            while responses.empty() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.025)
            if responses.empty():
                ref.update(status="timeout", reason="capture_timeout")
            else:
                ok, payload = responses.get_nowait()
                if not ok:
                    ref.update(status="failed", reason=payload)
                elif asyncio.get_running_loop().time() >= deadline:
                    ref.update(status="timeout", reason="capture_timeout")
                elif not isinstance(payload, dict) or not isinstance(payload.get("png"), bytes):
                    ref.update(status="failed", reason="invalid_capture_payload")
                else:
                    # Only the coordinator writes; expired workers never write or emit.
                    png = payload.pop("png")
                    ref.update(save_evidence_png(png, evidence_id=ref["evidence_id"]),
                               **{key: payload.get(key) for key in ("width", "height", "backend")})
        except asyncio.CancelledError:
            ref.update(status="cancelled", reason="capture_wait_cancelled")
            attach_evidence(value, [ref])
            logger.info("[FailureEvidence] %s", json.dumps(ref, ensure_ascii=False, default=str))
            raise
        except Exception as exc:
            ref.update(status="failed", reason=str(exc))
    attach_evidence(value, [ref])
    logger.info("[FailureEvidence] %s", json.dumps(ref, ensure_ascii=False, default=str))
    return [ref]


def collect_node_evidence(framework: dict | None) -> list[dict]:
    refs = []
    for node in (framework or {}).get("nodes", {}).values():
        if isinstance(node, dict):
            refs.extend(evidence_from(node))
            refs.extend(evidence_from((node.get("run_state") or {}).get("error")))
            refs.extend(evidence_from(node.get("output")))
    return list({ref["evidence_id"]: ref for ref in refs}.values())
