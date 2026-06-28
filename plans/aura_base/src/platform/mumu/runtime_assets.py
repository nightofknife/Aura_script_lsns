# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path


_HELPER_ABI_ALIASES = {
    "arm64-v8a": "arm64-v8a",
    "aarch64": "arm64-v8a",
    "armeabi-v7a": "armeabi-v7a",
    "armeabi": "armeabi-v7a",
    "armv7l": "armeabi-v7a",
    "x86": "x86",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}


def asset_root() -> Path:
    return Path(__file__).resolve().parents[3] / "assets" / "mumu"


def normalize_android_abi(abi: str) -> str:
    normalized = str(abi or "").strip().lower()
    return _HELPER_ABI_ALIASES.get(normalized, normalized)


def resolve_android_touch_helper_path(abi: str) -> Path:
    normalized = normalize_android_abi(abi)
    return asset_root() / "android_touch" / normalized / "touch"


def resolve_scrcpy_server_jar_path(version: str = "1.24") -> Path:
    normalized_version = str(version or "1.24").strip().lower().lstrip("v")
    return asset_root() / "scrcpy" / f"scrcpy-server-v{normalized_version}.jar"
