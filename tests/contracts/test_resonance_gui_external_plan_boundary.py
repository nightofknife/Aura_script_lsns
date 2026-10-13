"""Frozen GUI imports must not pull editable external plans into the PYZ."""
from pathlib import Path
import os
import runpy
import subprocess
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_gui_imports_and_freight_projection_do_not_load_external_plans():
    # A fresh process prevents earlier plan tests from satisfying imports through
    # sys.modules and hiding a GUI-to-plan dependency from this architecture check.
    code = r'''
import importlib.abc
import sys

class NoExternalPlans(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "plans" or fullname.startswith("plans."):
            raise AssertionError("GUI imported external plan module: " + fullname)
        return None

sys.meta_path.insert(0, NoExternalPlans())
from packages.resonance_gui import app, logic
from packages.resonance_gui.widgets.trade_page import TradePage
from packages.resonance_shared.freight_parameters import freight_control_state

result = logic.normalize_trade_task_inputs({
    "trade_mode": "free", "book_budget": 0, "book_policy": "fill",
    "auto_bento": False, "bento_priority": ["love_bentos"],
})
assert result["trade_mode"] == "free"
assert "bento_priority" not in result and "book_policy" not in result
assert freight_control_state(mode="free", book_usage="none", target_enabled=False,
    negotiation="auto", water=False, bento=False, medicine=False)["free"]
assert not any(name == "plans" or name.startswith("plans.") for name in sys.modules)
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen")
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("include_gui", [False, True])
def test_pyinstaller_analysis_collects_shared_contracts_in_each_variant(monkeypatch, include_gui):
    """Exercise the real spec setup, stopping before expensive binary analysis."""
    captured = {}
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda *_args, **_kwargs: ([], [], [])
    hooks.copy_metadata = lambda *_args, **_kwargs: []
    hooks.collect_submodules = lambda name: (
        [name, name + ".freight_parameters"] if name == "packages.resonance_shared" else [name]
    )
    for name in ("PyInstaller", "PyInstaller.utils"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    monkeypatch.setenv("AURA_PKG_INCLUDE_GUI", "1" if include_gui else "0")

    class AnalysisReached(Exception):
        pass

    def capture_analysis(*args, **kwargs):
        captured.update(kwargs)
        raise AnalysisReached

    with pytest.raises(AnalysisReached):
        runpy.run_path(str(ROOT / "packaging/pyinstaller/aura.spec"),
                       init_globals={"Analysis": capture_analysis})
    assert "packages.resonance_shared.freight_parameters" in captured["hiddenimports"]
    assert ("packages.resonance_gui" in captured["hiddenimports"]) is include_gui


def test_external_plan_compatibility_exports_use_the_same_contract_objects():
    from packages.resonance_shared import freight_parameters as shared
    from plans.resonance_pc.src.services import resonance_pc_freight_parameters as plan

    for name in plan.__all__:
        assert getattr(plan, name) is getattr(shared, name)
