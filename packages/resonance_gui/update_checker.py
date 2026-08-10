from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable
import urllib.request

from .paths import resolve_application_root


LATEST_RELEASE_API = "https://api.github.com/repos/nightofknife/Aura_script_lsns/releases/latest"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


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
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"AuraResonance/{current_tag}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=max(float(timeout_sec), 0.1)) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if bool(payload.get("draft")) or bool(payload.get("prerelease")):
        return None
    latest_tag = str(payload.get("tag_name") or "").strip()
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
