from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading


APP_TITLE = "Aura Resonance GUI"


def _release_root() -> Path:
    executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    return executable.resolve().parent


def _show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x00000010)
        return
    print(message, file=sys.stderr)


def main() -> int:
    root = _release_root()
    from packages.aura_core.observability.logging.coordinator import (
        close_session, flush, get_current_log_path, session_environment, start_session,
    )
    from packages.aura_core.observability.logging.core_logger import logger
    from packages.aura_core.observability.logging.stream_capture import (
        forward_stream, install_capture, uninstall_capture,
    )
    from packages.aura_core.config.loader import get_config_value
    from packages.aura_core.observability.logging.log_session import resolve_log_root
    log_root = resolve_log_root(root, os.environ.get("AURA_LOG_ROOT") or get_config_value(
        "logging.log_dir", "logs", base_path=str(root),
    ))
    start_session(log_root, role="launcher")
    install_capture(native_stderr=True)
    try:
        runtime_exe = root / "runtime" / "AuraResonanceRuntime.exe"
        if not runtime_exe.is_file():
            message = f"GUI runtime executable was not found:\n{runtime_exe}"
            logger.error("[Launcher] %s", message)
            _show_error(message)
            return 2
        runtime_self_check = "--runtime-self-check" in sys.argv[1:]
        if "--self-check" in sys.argv[1:] and not runtime_self_check:
            marker = f"[Launcher] phase=self_check pid={os.getpid()}"
            logger.info(marker)
            path = Path(get_current_log_path())
            if not flush() or marker not in path.read_text(encoding="utf-8"):
                logger.error("[Launcher] phase=self_check_failed reason=log_readback")
                return 4
            return 0
        env = os.environ.copy()
        env.update(session_environment())
        env["AURA_BASE_PATH"] = str(root)
        env["PYTHONNOUSERSITE"] = "1"
        runtime_args = ["--self-check"] if runtime_self_check else sys.argv[1:]
        try:
            process = subprocess.Popen([str(runtime_exe), *runtime_args], cwd=str(root), env=env,
                                       stderr=subprocess.PIPE)
        except OSError as exc:
            logger.exception("[Launcher] phase=runtime_start_failed")
            _show_error(f"Failed to start Aura Resonance GUI:\n{exc}")
            return 3
        # Capture bootloader/import failures before the runtime can attach its
        # own framework producer. Application logs use the dedicated transport.
        stderr_reader = threading.Thread(
            target=forward_stream, args=(process.stderr,),
            kwargs={"name": "runtime.stderr", "process_id": process.pid},
            name="aura-runtime-stderr", daemon=True,
        )
        stderr_reader.start()
        exitcode = process.wait()
        stderr_reader.join(timeout=5)
        logger.info("[Launcher] phase=runtime_exited pid=%s exitcode=%s", process.pid, exitcode)
        return exitcode
    finally:
        uninstall_capture()
        close_session()


if __name__ == "__main__":
    raise SystemExit(main())
