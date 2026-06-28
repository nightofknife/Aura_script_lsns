from __future__ import annotations

import threading
from typing import Optional

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.runtime.profiles import resolve_runtime_profile
from packages.aura_core.scheduler import Scheduler

_lock = threading.RLock()
_runtime: Optional[Scheduler] = None
_runtime_profile: Optional[str] = None


def _default_startup_timeout_sec() -> int:
    return int(
        get_config_value(
            "runtime.startup_timeout_sec",
            get_config_value("backend.scheduler_startup_timeout_sec", 10),
        )
    )


def create_runtime(profile: str = "embedded_full") -> Scheduler:
    global _runtime, _runtime_profile
    resolved = resolve_runtime_profile(profile)
    with _lock:
        if _runtime is None:
            logger.info("Creating runtime with profile: %s", resolved.name)
            _runtime = Scheduler(runtime_profile=resolved.name)
            _runtime_profile = resolved.name
        elif _runtime_profile != resolved.name:
            raise RuntimeError(
                f"Runtime already created with profile '{_runtime_profile}', "
                f"cannot switch to '{resolved.name}' in the same process."
            )
        return _runtime


def get_runtime() -> Scheduler:
    if _runtime is None:
        raise RuntimeError("Runtime has not been created.")
    return _runtime


def peek_runtime() -> Optional[Scheduler]:
    with _lock:
        return _runtime


def start_runtime(profile: str = "embedded_full", startup_timeout_sec: int | None = None) -> Scheduler:
    runtime = create_runtime(profile)
    timeout = _default_startup_timeout_sec() if startup_timeout_sec is None else int(startup_timeout_sec)
    setattr(runtime, "startup_timeout_sec", timeout)
    runtime.start_scheduler()
    if not runtime.startup_complete_event.wait(timeout=timeout):
        raise RuntimeError(f"Runtime startup timed out after {timeout} seconds.")
    return runtime


def stop_runtime() -> None:
    global _runtime, _runtime_profile
    with _lock:
        runtime = _runtime
    if runtime is not None:
        runtime.stop_scheduler()
    with _lock:
        _runtime = None
        _runtime_profile = None


def reset_runtime() -> None:
    stop_runtime()
