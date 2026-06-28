# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Resonance desktop GUI."""

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


ROOT = Path.cwd().resolve()
ENTRYPOINT = ROOT / "packages" / "resonance_gui" / "__main__.py"

INCLUDE_NVIDIA = os.environ.get("AURA_PKG_INCLUDE_NVIDIA", "").strip().lower() in {
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

hiddenimports += collect_submodules("packages.aura_core")
hiddenimports += collect_submodules("packages.aura_game")
hiddenimports += collect_submodules("packages.resonance_gui")
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


def _collect_optional_package(name: str) -> None:
    global datas, binaries, hiddenimports
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(name)
    except Exception:
        return
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports


for optional_pkg in (
    "PySide6",
    "shiboken6",
    "cv2",
    "dxcam",
    "screeninfo",
    "av",
    "dotenv",
    "yaml",
):
    _collect_optional_package(optional_pkg)

if INCLUDE_NVIDIA:
    binaries += collect_dynamic_libs("nvidia")

for dist_name in _installed_distributions(
    "PySide6",
    "shiboken6",
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
):
    datas += copy_metadata(dist_name)


a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "pyinstaller" / "rthook_aura_external_plans.py")],
    excludes=["tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AuraResonanceGui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AuraResonanceGui",
)
