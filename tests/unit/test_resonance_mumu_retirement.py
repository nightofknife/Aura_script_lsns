"""Retiring the game plan must preserve the framework emulator provider."""

from pathlib import Path
import os
import subprocess
import sys

import pytest

from plans.aura_base.src.platform.runtime_config import resolve_runtime_config
from scripts.release import validate_release


ROOT = Path(__file__).resolve().parents[2]


def test_pc_bridge_initializes_without_loading_or_dispatching_retired_tasks(monkeypatch):
    from packages.resonance_gui.bridge import RunnerBridge

    calls = []

    class PcRunner:
        def list_runs(self, *, limit, game_name):
            calls.append(("history", game_name))
            return []

        def target_status(self, *, game_name):
            calls.append(("target", game_name))
            return {"ok": False, "game_name": game_name}

    bridge = RunnerBridge(runner_factory=PcRunner)
    failures = []
    bridge.taskFailed.connect(failures.append)
    monkeypatch.setattr(bridge, "_ensure_poll_timer", lambda: None)
    bridge.initialize()
    assert failures == []
    assert calls == [("history", "resonance_pc"), ("target", "resonance_pc")]


def test_framework_emulator_imports_without_the_retired_game_or_device_access():
    code = r'''
import importlib
import importlib.abc
import os
import socket
import subprocess
import sys

class NoRetiredGame(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "plans.resonance" or fullname.startswith("plans.resonance."):
            raise AssertionError("Framework imported retired game: " + fullname)
        return None

def no_device_access(*args, **kwargs):
    raise AssertionError("Importing emulator support must not access a device")

class NoProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        no_device_access()

sys.meta_path.insert(0, NoRetiredGame())
subprocess.Popen = NoProcess
subprocess.run = os.system = no_device_access
socket.create_connection = no_device_access
socket.socket.connect = socket.socket.connect_ex = no_device_access
for name in (
    "plans.aura_base.src.platform.mumu",
    "plans.aura_base.src.platform.mumu.adb_discovery",
    "plans.aura_base.src.platform.mumu.android_touch_input",
    "plans.aura_base.src.platform.mumu.helper_manager",
    "plans.aura_base.src.platform.mumu.scrcpy_capture",
    "plans.aura_base.src.platform.mumu.scrcpy_compat",
    "plans.aura_base.src.platform.mumu.runtime_assets",
    "plans.aura_base.src.services.target_runtime_service",
):
    importlib.import_module(name)
'''
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=ROOT,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("target", [{}, {"mode": "adb_serial", "adb_serial": "127.0.0.1:16384"}])
def test_framework_emulator_config_is_independent_of_the_game_plan(target):
    resolved = resolve_runtime_config({"runtime": {"provider": "mumu", "target": target}})
    assert resolved.family == "android_emulator"
    assert resolved.provider == "mumu"
    assert resolved.target.mode == target.get("mode", "auto")
    assert resolved.target.adb_serial == target.get("adb_serial", "auto")
    assert resolved.capture.backend == "scrcpy_stream"
    assert resolved.input.backend == "android_touch"


def test_packaged_smoke_accepts_pc_only_game_distribution(monkeypatch, tmp_path):
    calls = []

    def run_checked(args, **kwargs):
        calls.append(args)
        return '[{"game_name": "resonance_pc"}]' if "games" in args else ""

    monkeypatch.setattr(validate_release, "_run_checked", run_checked)
    monkeypatch.setattr(validate_release, "_resolve_powershell_executable", lambda: "pwsh")
    validate_release.run_runtime_smoke(tmp_path)
    assert any(args[-2:] == ["tasks", "resonance_pc"] for args in calls)
    assert not any("resonance" in args for args in calls)


def test_packaged_smoke_rejects_a_resurrected_mumu_game(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_release, "_resolve_powershell_executable", lambda: "pwsh")
    monkeypatch.setattr(
        validate_release, "_run_checked",
        lambda *args, **kwargs: '[{"game_name": "resonance_pc"}, {"game_name": "resonance"}]',
    )
    with pytest.raises(ValueError, match="retired Resonance MuMu plan"):
        validate_release.run_runtime_smoke(tmp_path)
