from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable
import urllib.request

from packages.aura_core.release_manifest import (
    LATEST_CHECKSUMS_URL,
    MAX_CHECKSUM_BYTES,
    parse_cpu_manifest,
    release_version_key,
)

from . import __version__
from .paths import resolve_application_root


@dataclass(frozen=True)
class UpdateCheckResult:
    current_tag: str
    latest_tag: str
    update_available: bool


def _canonical_version(value: Any) -> str:
    parsed = release_version_key(value, allow_unprefixed=True)
    if parsed is None:
        return ""
    major, minor, patch, stable = parsed
    suffix = "" if stable else "-E"
    return f"v{major}.{minor}.{patch}{suffix}"


def _read_current_tag(root: Path) -> str:
    info_path = root / "BUILD-INFO.json"
    if not info_path.is_file():
        return ""
    payload = json.loads(info_path.read_text(encoding="utf-8-sig"))
    return str(payload.get("release_label") or "").strip()


def current_version_label(*, base_path: Path | None = None) -> str:
    """Return the canonical release label, including ``-E`` for a test build.

    Historical source builds could record a bare numeric version. Keep accepting
    those builds for updates, but normalize every user-facing label to include
    the canonical ``v`` prefix.
    """

    root = Path(base_path).resolve() if base_path is not None else resolve_application_root()
    try:
        packaged_tag = _read_current_tag(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        packaged_tag = ""
    packaged_version = _canonical_version(packaged_tag)
    if packaged_version:
        return packaged_version
    return _canonical_version(__version__)


def _read_latest_tag(contents: bytes) -> str:
    try:
        tag, _ = parse_cpu_manifest(contents)
    except ValueError:
        return ""
    return "" if tag.endswith("-E") else tag


def check_for_update(
    *,
    base_path: Path | None = None,
    opener: Callable[..., Any] | None = None,
    timeout_sec: float = 5.0,
) -> UpdateCheckResult | None:
    root = Path(base_path).resolve() if base_path is not None else resolve_application_root()
    current_tag = _read_current_tag(root)
    current_version = release_version_key(current_tag, allow_unprefixed=True)
    if current_version is None:
        return None

    request = urllib.request.Request(
        LATEST_CHECKSUMS_URL,
        headers={
            "Accept": "text/plain",
            "User-Agent": f"AuraResonance/{current_tag}",
        },
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=max(float(timeout_sec), 0.1)) as response:
        contents = response.read(MAX_CHECKSUM_BYTES + 1)

    if len(contents) > MAX_CHECKSUM_BYTES:
        return None
    latest_tag = _read_latest_tag(contents)
    latest_version = release_version_key(latest_tag)
    if latest_version is None:
        return None
    return UpdateCheckResult(
        current_tag=_canonical_version(current_tag),
        latest_tag=_canonical_version(latest_tag),
        update_available=latest_version > current_version,
    )


def find_available_update(*, base_path: Path | None = None) -> str:
    try:
        result = check_for_update(base_path=base_path)
    except Exception:
        return ""
    if result is None or not result.update_available:
        return ""
    return result.latest_tag
