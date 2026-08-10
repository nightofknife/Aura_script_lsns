from __future__ import annotations

import os
from pathlib import Path
import sys


def resolve_application_root() -> Path:
    """Resolve the portable Aura installation/workspace root."""
    configured = str(os.environ.get("AURA_BASE_PATH") or "").strip()
    if configured:
        return Path(configured).resolve()

    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        for candidate in (executable_dir, executable_dir.parent):
            if (candidate / "config.yaml").is_file() or (candidate / "plans").is_dir():
                return candidate.resolve()
        return executable_dir

    return Path(__file__).resolve().parents[2]
