from __future__ import annotations

import ctypes
import os


class AdminPrivilegeRequiredError(RuntimeError):
    """Raised when Aura runtime is started without administrator privileges."""

    def __init__(self, context: str = "Aura framework runtime"):
        self.context = context
        super().__init__(
            f"{context} must be started with administrator privileges on Windows. "
            "Please restart Codex, python, or the Aura CLI with 'Run as administrator'."
        )


def is_running_as_admin() -> bool:
    """Return whether the current process has administrator privileges."""
    if os.name != "nt":
        return True

    shell32 = getattr(ctypes.windll, "shell32", None)
    is_user_an_admin = getattr(shell32, "IsUserAnAdmin", None) if shell32 else None
    if not callable(is_user_an_admin):
        return False

    try:
        return bool(is_user_an_admin())
    except Exception:
        return False


def ensure_admin_startup(context: str = "Aura framework runtime") -> None:
    """Fail fast when the framework is started without admin privileges on Windows."""
    if os.name != "nt":
        return
    if not is_running_as_admin():
        raise AdminPrivilegeRequiredError(context)
