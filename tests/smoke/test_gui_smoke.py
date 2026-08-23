"""Minimal source-tree checks for the GUI entry point and self-check."""

from __future__ import annotations

import os
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
    runner = _FakeRunner()
    with patch("packages.resonance_gui.app.SubprocessGameRunner", return_value=runner):
        assert self_check_resonance_gui() == 0
    assert runner.closed is True
