"""Minimal import checks for top-level Aura applications and plans."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    (
        "cli",
        "packages.aura_core",
        "packages.aura_game",
        "packages.resonance_gui",
        "plans.resonance",
        "plans.resonance_pc",
    ),
)
def test_project_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None
