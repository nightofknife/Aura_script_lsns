"""Verify that repository packages and public task catalogs can be discovered."""

from __future__ import annotations

from pathlib import Path

from packages.aura_core.packaging.core.package_manager import PackageManager


REPO_ROOT = Path(__file__).resolve().parents[2]


def _discover_packages():
    manager = PackageManager(
        packages_dir=REPO_ROOT / "packages",
        plans_dir=REPO_ROOT / "plans",
    )
    return manager._discover_packages()


def test_required_packages_are_discoverable() -> None:
    packages = _discover_packages()

    assert {
        "plans/aura_base",
        "plans/aura_benchmark",
        "plans/resonance",
        "plans/resonance_pc",
    }.issubset(packages)


def test_required_plan_catalogs_are_loadable() -> None:
    packages = _discover_packages()

    assert packages["plans/resonance"].exports.tasks
    assert packages["plans/resonance_pc"].exports.tasks
