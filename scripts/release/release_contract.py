from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = REPO_ROOT / "packaging" / "release-contract.json"


def load_contract(path: Path | str = DEFAULT_CONTRACT) -> dict:
    contract_path = Path(path).resolve()
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"Unsupported release contract schema: {payload.get('schema_version')!r}")
    if set(payload.get("profiles", {})) != {"cpu", "gpu", "overlay"}:
        raise ValueError("Release contract must define cpu, gpu, and overlay profiles.")
    return payload


def filesystem_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    if os.name == "nt" and not str(resolved).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(resolved))
    return resolved


def path_is_file(path: Path) -> bool:
    return filesystem_path(path).is_file()


def file_sha256(path: Path) -> str:
    digest = sha256()
    with filesystem_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_fingerprint(root: Path, paths: Iterable[Path] | None = None) -> str:
    resolved = root.resolve()
    selected = paths if paths is not None else (path for path in resolved.rglob("*") if path_is_file(path))
    digest = sha256()
    for path in sorted((Path(path).resolve() for path in selected), key=lambda item: item.as_posix().lower()):
        relative = path.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value).strip()).lower()


def parse_hashed_lock(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line[0].isspace() or raw_line.startswith(("#", "--")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", raw_line)
        if not match:
            continue
        name = normalize_distribution_name(match.group(1))
        packages[name] = match.group(2)
    if not packages:
        raise ValueError(f"No pinned packages found in release lock: {path}")
    return packages


def render_contract_value(template: str, *, label: str) -> str:
    return str(template).format(label=label)
