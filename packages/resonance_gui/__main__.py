from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import sys


def _ensure_packaged_base_path() -> Path | None:
    configured = str(os.environ.get("AURA_BASE_PATH") or "").strip()
    if configured:
        return Path(configured).resolve()

    if not getattr(sys, "frozen", False):
        return None

    executable_dir = Path(sys.executable).resolve().parent
    for candidate in (executable_dir.parent, executable_dir):
        if (candidate / "plans").is_dir():
            resolved = candidate.resolve()
            os.environ["AURA_BASE_PATH"] = str(resolved)
            return resolved
    return None


def main() -> int:
    base = _ensure_packaged_base_path() or Path(__file__).resolve().parents[2]
    multiprocessing.freeze_support()
    # Bootstrap before importing Qt, capture backends, or application modules.
    from packages.aura_core.observability.logging.coordinator import close_session, get_session_descriptor, start_session
    from packages.aura_core.observability.logging.stream_capture import install_capture, uninstall_capture
    from packages.aura_core.observability.logging.core_logger import logger
    from packages.aura_core.observability.logging.log_session import resolve_log_root
    from packages.aura_core.config.loader import get_config_value

    owns_session = get_session_descriptor() is None
    start_session(resolve_log_root(base, os.environ.get("AURA_LOG_ROOT") or get_config_value(
        "logging.log_dir", "logs", base_path=str(base),
    )), role="gui")
    install_capture(native_stderr=True)
    try:
        from packages.resonance_gui.app import launch_resonance_gui, self_check_resonance_gui

        if "--self-check" in sys.argv[1:]:
            return self_check_resonance_gui()
        return launch_resonance_gui()
    except Exception:
        logger.exception("[GuiStartup] phase=failed")
        raise
    finally:
        if owns_session:
            uninstall_capture()
            close_session()


if __name__ == "__main__":
    raise SystemExit(main())
