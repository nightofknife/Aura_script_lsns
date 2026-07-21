# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for zero-framework-change Aura packaging.

This spec intentionally keeps plans external in the assembled release root
so users can edit them. GUI packaging is optional and controlled by
AURA_PKG_INCLUDE_GUI.
"""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


# PyInstaller executes spec files via `exec`, so `__file__` is not guaranteed.
# The build wrapper runs PyInstaller from the repository root.
ROOT = Path.cwd().resolve()
ENTRYPOINT = ROOT / "cli.py"
GUI_ENTRYPOINT = ROOT / "packages" / "resonance_gui" / "__main__.py"

INCLUDE_NVIDIA = os.environ.get("AURA_PKG_INCLUDE_NVIDIA", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
INCLUDE_GUI = os.environ.get("AURA_PKG_INCLUDE_GUI", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _installed_distributions(*names: str) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for name in names:
        normalized = str(name).strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        try:
            metadata.version(normalized)
        except metadata.PackageNotFoundError:
            continue
        resolved.append(normalized)
        seen.add(key)
    return resolved


datas = []
binaries = []
hiddenimports = ["win32timezone"]
datas.append((str(ROOT / "docs" / "schemas" / "task-schema.json"), "docs/schemas"))

# Aura uses several lazy package-level exports and importlib-based lookups.
# Packaging the full framework submodule graph is simpler and more reliable
# than chasing each delayed import one by one.
hiddenimports += collect_submodules("packages.aura_core")
hiddenimports += collect_submodules("packages.aura_game")
hiddenimports += [
    "PIL.ImageGrab",
    "wave",
    "win32api",
    "win32con",
    "win32gui",
    "win32ui",
    "win32process",
    "pythoncom",
    "pywintypes",
]
if INCLUDE_GUI:
    hiddenimports += collect_submodules("packages.resonance_gui")


def _collect_optional_package(name: str) -> None:
    global datas, binaries, hiddenimports
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(name)
    except Exception:
        return
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports


optional_packages = [
    "onnxruntime",
    "numpy",
    "cv2",
    "dxcam",
    "screeninfo",
    "av",
    "dotenv",
    "yaml",
]
if INCLUDE_GUI:
    optional_packages += [
        "PySide6",
        "shiboken6",
    ]

for optional_pkg in optional_packages:
    _collect_optional_package(optional_pkg)

if INCLUDE_NVIDIA:
    binaries += collect_dynamic_libs("nvidia")

metadata_distributions = [
    "onnxruntime-gpu",
    "onnxruntime",
    "numpy",
    "opencv-python",
    "av",
    "pywin32",
    "screeninfo",
    "psutil",
    "watchdog",
    "cachetools",
    "fastjsonschema",
    "python-dotenv",
    "prompt_toolkit",
]
if INCLUDE_GUI:
    metadata_distributions += [
        "PySide6",
        "shiboken6",
    ]

for dist_name in _installed_distributions(*metadata_distributions):
    datas += copy_metadata(dist_name)

analysis_entrypoints = [str(ENTRYPOINT)]
if INCLUDE_GUI:
    analysis_entrypoints.append(str(GUI_ENTRYPOINT))

excluded_modules = [
    "paddle",
    "paddleocr",
    "paddlex",
    "torch",
    "torchvision",
    "ultralytics",
    "matplotlib",
    "pandas",
    "scipy",
    "tests",
]
if not INCLUDE_GUI:
    excluded_modules += [
        "PySide6",
        "shiboken6",
        "packages.resonance_gui",
    ]

a = Analysis(
    analysis_entrypoints,
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "pyinstaller" / "rthook_aura_external_plans.py")],
    excludes=excluded_modules,
    noarchive=False,
    optimize=0,
)

bundled_plan_modules = sorted(
    name for name, *_ in a.pure if name == "plans" or name.startswith("plans.")
)
if bundled_plan_modules:
    raise SystemExit(
        "External Plan modules were unexpectedly bundled by PyInstaller: "
        + ", ".join(bundled_plan_modules[:20])
    )

pyz = PYZ(a.pure)

def _scripts_for(entrypoint: Path):
    target = entrypoint.resolve()
    matches = []
    for script in a.scripts:
        try:
            script_path = Path(script[1]).resolve()
        except Exception:
            continue
        if script_path == target:
            matches.append(script)
    if not matches:
        raise SystemExit(f"PyInstaller script entrypoint was not analyzed: {target}")
    return matches


exe = EXE(
    pyz,
    _scripts_for(ENTRYPOINT),
    [],
    exclude_binaries=True,
    name="aura",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

collected_executables = [exe]

if INCLUDE_GUI:
    gui_exe = EXE(
        pyz,
        _scripts_for(GUI_ENTRYPOINT),
        [],
        exclude_binaries=True,
        name="AuraResonanceRuntime",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
    )
    collected_executables.append(gui_exe)

coll = COLLECT(
    *collected_executables,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="aura",
)
