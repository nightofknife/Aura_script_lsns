"""Stage immutable native files so the installed package can be updated in use."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from ....aura_base.src.platform.contracts import TargetRuntimeError

ASSETS = Path(__file__).resolve().parents[2] / "assets" / "input_bridge"
BOOTSTRAP_DLL = "resonance_bridge_bootstrap.dll"
HOST_DLL = "resonance_bridge_host.dll"


def _cache_root() -> Path:
    override = os.environ.get("AURA_INPUT_BRIDGE_CACHE")
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return (Path(override) if override else local_app_data / "AuraResonance" / "input_bridge").resolve()


def _store(path: Path, data: bytes) -> None:
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        temp = Path(staged.name)
        staged.write(data)
    try:
        try:
            os.replace(temp, path)
        except OSError:
            # A concurrent loader may have published and mapped the same DLL.
            if not path.is_file() or path.read_bytes() != data:
                raise
    finally:
        temp.unlink(missing_ok=True)


def stage_bootstrap(assets: Path) -> Path:
    try:
        binary = (assets / BOOTSTRAP_DLL).read_bytes()
        profiles = sorted((assets / "profiles").glob("*.json"))
        if not profiles:
            raise FileNotFoundError("No Unity profiles were supplied")
        files = {path.name: path.read_bytes() for path in profiles}
        digest = hashlib.sha256(binary)
        for name, data in files.items():
            digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
        directory = _cache_root() / "bootstrap" / digest.hexdigest().upper()
        for name, data in files.items():
            _store(directory / "profiles" / name, data)
        path = directory / BOOTSTRAP_DLL
        _store(path, binary)
        return path
    except OSError as exc:
        raise TargetRuntimeError("input_bridge_stage_failed", f"Cannot stage bridge bootstrap: {exc}") from exc


def stage_host(assets: Path, cache_root: str) -> str:
    try:
        cache = Path(cache_root)
        if not cache.is_absolute():
            raise ValueError("The bootstrap must report an absolute cache path")
        binary = (assets / HOST_DLL).read_bytes()
        digest = hashlib.sha256(binary).hexdigest().upper()
        _store(cache / "host" / digest / HOST_DLL, binary)
        return digest
    except (OSError, ValueError) as exc:
        raise TargetRuntimeError("input_bridge_stage_failed", f"Cannot stage bridge DLL: {exc}") from exc
