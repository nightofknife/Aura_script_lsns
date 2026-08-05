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
    _ensure_packaged_base_path()
    multiprocessing.freeze_support()

    from packages.resonance_gui.app import launch_resonance_gui, self_check_resonance_gui

    if "--self-check" in sys.argv[1:]:
        return self_check_resonance_gui()
    return launch_resonance_gui()


if __name__ == "__main__":
    raise SystemExit(main())
