"""Optional runtime diagnostics for external plans imports.

Enabled only when AURA_PKG_DEBUG_IMPORT=1 is set.
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback
from pathlib import Path


_dll_directory_handles = []


def _add_windows_dll_directory(path: Path) -> None:
    if os.name != "nt" or not path.is_dir():
        return
    resolved = str(path.resolve())
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if callable(add_dll_directory):
        try:
            _dll_directory_handles.append(add_dll_directory(resolved))
        except OSError:
            pass

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if resolved not in path_entries:
        os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")


def _add_nvidia_runtime_directories(root: Path) -> None:
    for relative in (
        "nvidia/cuda_runtime/bin",
        "nvidia/cuda_nvrtc/bin",
        "nvidia/nvjitlink/bin",
        "nvidia/cudnn/bin",
        "nvidia/cublas/bin",
        "nvidia/cufft/bin",
        "nvidia/curand/bin",
        "nvidia/cusolver/bin",
        "nvidia/cusparse/bin",
    ):
        _add_windows_dll_directory(root / relative)

    nvidia_root = root / "nvidia"
    if nvidia_root.is_dir():
        for child in nvidia_root.iterdir():
            if child.is_dir():
                _add_windows_dll_directory(child / "bin")
                _add_windows_dll_directory(child / "lib")


def _prepare_packaged_dll_search_paths() -> None:
    if os.name != "nt":
        return

    executable_dir = Path(sys.executable).resolve().parent
    internal_roots = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        internal_roots.append(Path(meipass))
    internal_roots.append(executable_dir / "_internal")

    seen_roots = set()
    for internal_root in internal_roots:
        if not internal_root.is_dir():
            continue
        root_key = str(internal_root.resolve()).lower()
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)

        for relative in ("onnxruntime/capi", "av.libs", "numpy.libs"):
            _add_windows_dll_directory(internal_root / relative)
        _add_nvidia_runtime_directories(internal_root)

    for env_name, env_value in os.environ.items():
        if env_name == "CUDA_PATH" or env_name.startswith("CUDA_PATH_V"):
            cuda_root = Path(env_value)
            _add_windows_dll_directory(cuda_root / "bin")
            _add_windows_dll_directory(cuda_root / "libnvvp")

    for env_value in os.environ.get("AURA_NVIDIA_RUNTIME_PATH", "").split(os.pathsep):
        if env_value:
            runtime_root = Path(env_value)
            _add_windows_dll_directory(runtime_root)
            _add_windows_dll_directory(runtime_root / "bin")
            if runtime_root.name.lower() == "nvidia":
                _add_nvidia_runtime_directories(runtime_root.parent)
            _add_nvidia_runtime_directories(runtime_root)

    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if path_entry and any(token in path_entry.lower() for token in ("cuda", "cudnn", "nvidia", "tensorrt")):
            _add_windows_dll_directory(Path(path_entry))

    cuda_install_root = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "NVIDIA GPU Computing Toolkit" / "CUDA"
    if cuda_install_root.is_dir():
        for cuda_root in cuda_install_root.glob("v*"):
            _add_windows_dll_directory(cuda_root / "bin")
            _add_windows_dll_directory(cuda_root / "libnvvp")


_prepare_packaged_dll_search_paths()

base_path = os.environ.get("AURA_BASE_PATH")
if base_path:
    resolved = str(Path(base_path).resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)

if os.environ.get("AURA_PKG_DEBUG_IMPORT", "").strip().lower() in {"1", "true", "yes", "on"}:
    try:
        module = importlib.import_module("plans.aura_base.src.services.app_provider_service")
        print(f"[aura-pkg-debug] external plan import ok: {getattr(module, '__file__', '<frozen>')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[aura-pkg-debug] external plan import failed: {exc!r}")
        traceback.print_exc()
