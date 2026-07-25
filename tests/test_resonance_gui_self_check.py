from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


def test_gui_self_check_builds_window_and_verifies_subprocess_runner():
    runner = _FakeRunner()
    with patch("packages.resonance_gui.app.SubprocessGameRunner", return_value=runner):
        assert self_check_resonance_gui() == 0
    assert runner.closed


def test_gui_self_check_requires_windows_capture():
    with patch(
        "packages.resonance_gui.app._import_required_wgc_module",
        side_effect=ModuleNotFoundError("No module named 'windows_capture'"),
    ):
        try:
            self_check_resonance_gui()
        except RuntimeError as exc:
            assert "windows_capture" in str(exc)
        else:
            raise AssertionError("self-check should fail when windows_capture is unavailable")
