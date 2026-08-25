from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable
import urllib.request

from . import __version__
from .paths import resolve_application_root


LATEST_CHECKSUMS_URL = (
    "https://github.com/nightofknife/Aura_script_lsns/releases/latest/download/SHA256SUMS.txt"
)
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
_RELEASE_FILENAME_RE = re.compile(
    r"^AuraResonance-(v?\d+\.\d+\.\d+)-(?:win-x64-(?:cpu|gpu)|nvidia-cu13-overlay)\.zip$",
    re.IGNORECASE,
)
_MAX_CHECKSUM_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class UpdateCheckResult:
    current_tag: str
    latest_tag: str
    update_available: bool


def _parse_version(value: Any) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _read_current_tag(root: Path) -> str:
    info_path = root / "BUILD-INFO.json"
    if not info_path.is_file():
        return ""
    payload = json.loads(info_path.read_text(encoding="utf-8-sig"))
    return str(payload.get("release_label") or "").strip()


def current_version_label(*, base_path: Path | None = None) -> str:
    """Return the packaged release label, with a source-tree version fallback."""

    root = Path(base_path).resolve() if base_path is not None else resolve_application_root()
    try:
        packaged_tag = _read_current_tag(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        packaged_tag = ""
    if _parse_version(packaged_tag) is not None:
        return packaged_tag if packaged_tag.lower().startswith("v") else f"v{packaged_tag}"
    source_version = str(__version__).strip().lstrip("vV")
    return f"v{source_version}"


def _read_latest_tag(contents: bytes) -> str:
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ""

    filenames: set[str] = set()
    tags: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            return ""
        filename = match.group(2).strip()
        if filename != Path(filename).name or filename.casefold() in filenames:
            return ""
        filenames.add(filename.casefold())
        release_match = _RELEASE_FILENAME_RE.fullmatch(filename)
        if release_match is not None:
            tags.add(release_match.group(1))

    if len(tags) != 1:
        return ""
    tag = next(iter(tags))
    required = {
        f"AuraResonance-{tag}-win-x64-cpu.zip".casefold(),
        f"AuraResonance-{tag}-win-x64-gpu.zip".casefold(),
        f"AuraResonance-{tag}-nvidia-cu13-overlay.zip".casefold(),
    }
    return tag if required.issubset(filenames) else ""


def check_for_update(
    *,
    base_path: Path | None = None,
    opener: Callable[..., Any] | None = None,
    timeout_sec: float = 5.0,
) -> UpdateCheckResult | None:
    root = Path(base_path).resolve() if base_path is not None else resolve_application_root()
    current_tag = _read_current_tag(root)
    current_version = _parse_version(current_tag)
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
        contents = response.read(_MAX_CHECKSUM_BYTES + 1)

    if len(contents) > _MAX_CHECKSUM_BYTES:
        return None
    latest_tag = _read_latest_tag(contents)
    latest_version = _parse_version(latest_tag)
    if latest_version is None:
        return None
    return UpdateCheckResult(
        current_tag=current_tag,
        latest_tag=latest_tag,
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
