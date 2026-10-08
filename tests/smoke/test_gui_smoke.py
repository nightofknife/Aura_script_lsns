"""Minimal source-tree checks for the GUI entry point and self-check."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from packages.resonance_gui import __main__ as gui_entrypoint
from packages.resonance_gui.app import self_check_resonance_gui


class _FakeRunner:
    def __init__(self) -> None:
        self.closed = False

    def list_games(self, *, include_shared: bool):
        assert include_shared
        return [
            {"game_name": "aura_base"},
            {"game_name": "aura_benchmark"},
            {"game_name": "resonance"},
            {"game_name": "resonance_pc"},
        ]

    def close(self) -> None:
        self.closed = True


def test_gui_entrypoint_dispatches_frozen_process_support_before_launch() -> None:
    calls: list[str] = []
    with (
        patch.object(
            gui_entrypoint.multiprocessing,
            "freeze_support",
            side_effect=lambda: calls.append("freeze_support"),
        ),
        patch(
            "packages.resonance_gui.app.launch_resonance_gui",
            side_effect=lambda: calls.append("launch_gui") or 0,
        ),
        patch.object(sys, "argv", ["AuraResonanceRuntime.exe"]),
    ):
        assert gui_entrypoint.main() == 0

    assert calls == ["freeze_support", "launch_gui"]


def test_gui_entrypoint_dispatches_self_check() -> None:
    calls: list[str] = []
    with (
        patch.object(
            gui_entrypoint.multiprocessing,
            "freeze_support",
            side_effect=lambda: calls.append("freeze_support"),
        ),
        patch(
            "packages.resonance_gui.app.self_check_resonance_gui",
            side_effect=lambda: calls.append("self_check") or 0,
        ),
        patch.object(sys, "argv", ["AuraResonanceRuntime.exe", "--self-check"]),
    ):
        assert gui_entrypoint.main() == 0

    assert calls == ["freeze_support", "self_check"]


def test_gui_self_check_builds_window_and_discovers_plans() -> None:
    # A full-suite QApplication can retain widgets from earlier GUI unit tests.
    # Reconfiguring its global font/style is not representative of startup and
    # can keep old layout events alive. Exercise a fresh process and real IPC.
    source_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, AURA_BASE_PATH=str(source_root), PYTHONPATH=str(source_root), QT_QPA_PLATFORM="offscreen")
    code = (
        f"import sys;sys.path.insert(0,{str(source_root)!r});"
        "from packages.resonance_gui.app import self_check_resonance_gui;"
        "raise SystemExit(self_check_resonance_gui())"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
