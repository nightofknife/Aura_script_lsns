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


def test_gui_self_check_builds_window_without_starting_runtime_bridge():
    runner = _FakeRunner()
    with patch("packages.resonance_gui.app.EmbeddedGameRunner", return_value=runner):
        assert self_check_resonance_gui() == 0
    assert runner.closed
