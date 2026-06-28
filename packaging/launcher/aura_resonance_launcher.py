from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys


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
    runtime_exe = root / "runtime" / "AuraResonanceRuntime.exe"
    if not runtime_exe.is_file():
        _show_error(f"GUI runtime executable was not found:\n{runtime_exe}")
        return 2

    env = os.environ.copy()
    env["AURA_BASE_PATH"] = str(root)
    env["PYTHONNOUSERSITE"] = "1"

    try:
        return subprocess.call([str(runtime_exe), *sys.argv[1:]], cwd=str(root), env=env)
    except OSError as exc:
        _show_error(f"Failed to start Aura Resonance GUI:\n{exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
