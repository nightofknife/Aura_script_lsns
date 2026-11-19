from __future__ import annotations

import re
from typing import NamedTuple


RELEASES_URL = "https://github.com/nightofknife/Aura-Resonance/releases"
LATEST_CHECKSUMS_URL = f"{RELEASES_URL}/latest/download/SHA256SUMS.txt"
EXPERIMENTAL_INDEX_URL = (
    "https://raw.githubusercontent.com/nightofknife/Aura-Resonance/"
    "release-channels/experimental.json"
)
MAX_CHECKSUM_BYTES = 2 * 1024 * 1024

_VERSION_CORE = r"([0-9]+)\.([0-9]+)\.([0-9]+)"
_EXPERIMENTAL_SUFFIX = r"(-E(?:\.([1-9][0-9]*))?)?"
_VERSION_RE = re.compile(rf"(v?){_VERSION_CORE}{_EXPERIMENTAL_SUFFIX}")
_CHECKSUM_RE = re.compile(r"([0-9a-fA-F]{64})\s+\*?(.+?)\s*")
_CPU_ASSET_RE = re.compile(rf"AuraResonance-(v{_VERSION_CORE}{_EXPERIMENTAL_SUFFIX})-win-x64-cpu\.zip")


class ReleaseVersionKey(NamedTuple):
    major: int
    minor: int
    patch: int
    stable: bool
    serial: int


def release_version_key(
    value: object, *, allow_unprefixed: bool = False,
) -> ReleaseVersionKey | None:
    """Sort test serials numerically. The matching formal build has precedence."""
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    prefix, major, minor, patch, experimental, serial = match.groups()
    if not prefix and not allow_unprefixed:
        return None
    return ReleaseVersionKey(int(major), int(minor), int(patch), experimental is None, int(serial or 0))


def parse_cpu_manifest(contents: bytes) -> tuple[str, dict[str, str]]:
    try:
        lines = contents.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("SHA256SUMS.txt 编码无效") from exc

    checksums = {}
    tags = set()
    for line in lines:
        if not line.strip():
            continue
        match = _CHECKSUM_RE.fullmatch(line)
        if match is None:
            raise ValueError("SHA256SUMS.txt 中有无效的校验行")
        checksum, filename = match.groups()
        filename = filename.strip()
        key = filename.casefold()
        if not filename or any(char in filename for char in "/\\:") or key in checksums:
            raise ValueError("SHA256SUMS.txt 中有无效或重复的文件名")
        checksums[key] = checksum.lower()
        if asset := _CPU_ASSET_RE.fullmatch(filename):
            tags.add(asset.group(1))

    if len(tags) != 1:
        raise ValueError("无法从 SHA256SUMS.txt 确定唯一的 CPU 完整包版本")
    return tags.pop(), checksums
