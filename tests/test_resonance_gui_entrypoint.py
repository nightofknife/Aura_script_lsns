from __future__ import annotations

import os
from pathlib import Path
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


def test_entrypoint_infers_release_root_before_frozen_process_dispatch(tmp_path):
    release_root = tmp_path / "AuraResonance"
    runtime_dir = release_root / "runtime"
    runtime_dir.mkdir(parents=True)
    (release_root / "plans").mkdir()
    executable = runtime_dir / "AuraResonanceRuntime.exe"
    executable.touch()
    calls: list[tuple[str, str | None]] = []

    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(sys, "executable", str(executable)),
        patch.object(sys, "frozen", True, create=True),
        patch.object(
            gui_entrypoint.multiprocessing,
            "freeze_support",
            side_effect=lambda: calls.append(("freeze_support", os.environ.get("AURA_BASE_PATH"))),
        ),
        patch(
            "packages.resonance_gui.app.launch_resonance_gui",
            side_effect=lambda: calls.append(("launch_gui", os.environ.get("AURA_BASE_PATH"))) or 0,
        ),
        patch.object(sys, "argv", ["AuraResonanceRuntime.exe"]),
    ):
        assert gui_entrypoint.main() == 0

    expected = str(release_root.resolve())
    assert calls == [("freeze_support", expected), ("launch_gui", expected)]


def test_entrypoint_preserves_explicit_base_path(tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()

    with patch.dict(os.environ, {"AURA_BASE_PATH": str(configured)}, clear=True):
        assert gui_entrypoint._ensure_packaged_base_path() == Path(configured).resolve()
        assert os.environ["AURA_BASE_PATH"] == str(configured)


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
