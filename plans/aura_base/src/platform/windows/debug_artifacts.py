# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from packages.aura_core.context.plan import current_plan_name

from ..contracts import TargetRuntimeError
from ..runtime_config import resolve_runtime_config


class DebugArtifactsManager:
    def __init__(self, config: Any):
        self._config = config
        self._repo_root = _discover_repo_root(Path(__file__).resolve())
        self._events = deque(maxlen=0)
        self._lock = threading.RLock()

    def record_input_event(self, event_name: str, payload: dict[str, Any]) -> None:
        debug = resolve_runtime_config(self._config).debug
        if debug.input_trace_size <= 0:
            return
        with self._lock:
            if self._events.maxlen != debug.input_trace_size:
                self._events = deque(self._events, maxlen=debug.input_trace_size)
            self._events.append(
                {
                    "timestamp_ms": int(time.time() * 1000),
                    "event": str(event_name),
                    "payload": dict(payload),
                }
            )

    def capture_error_artifacts(
        self,
        *,
        method_name: str,
        exc: TargetRuntimeError,
        session: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        resolved = resolve_runtime_config(self._config)
        debug = resolved.debug
        if not any(
            (
                debug.capture_on_error,
                debug.dump_window_summary_on_error,
                debug.input_trace_size > 0,
            )
        ):
            return None

        plan_name = current_plan_name.get() or "__global__"
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        base_dir = (self._repo_root / debug.artifact_dir / plan_name / timestamp).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)

        artifact = {
            "method_name": method_name,
            "timestamp": timestamp,
            "plan_name": plan_name,
            "error": exc.to_dict(),
            "extra": dict(extra or {}),
            "runtime": resolved.to_dict(),
        }
        if debug.input_trace_size > 0:
            with self._lock:
                artifact["input_trace"] = list(self._events)

        if debug.dump_window_summary_on_error and session is not None:
            try:
                artifact["session_self_check"] = session.self_check()
            except Exception as session_exc:
                artifact["session_self_check_error"] = str(session_exc)

        image_path = None
        if debug.capture_on_error and session is not None and hasattr(session, "capture"):
            try:
                capture = session.capture()
                if getattr(capture, "success", False) and getattr(capture, "image", None) is not None:
                    image_path = base_dir / "failure_capture.png"
                    capture.save(str(image_path))
            except Exception as capture_exc:
                artifact["capture_error"] = str(capture_exc)

        artifact_path = base_dir / "failure.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "artifact_path": str(artifact_path),
            "capture_path": str(image_path) if image_path is not None else None,
        }


def _discover_repo_root(start_path: Path) -> Path:
    for parent in start_path.parents:
        if (parent / "plans" / "aura_base").is_dir():
            return parent
    return start_path.parents[5]
