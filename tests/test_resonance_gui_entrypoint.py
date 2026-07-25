from __future__ import annotations

import sys
from unittest.mock import patch

from packages.resonance_gui import __main__ as gui_entrypoint


def test_entrypoint_dispatches_frozen_process_before_launching_gui():
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


def test_entrypoint_self_check_runs_after_frozen_process_dispatch():
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
