from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "packaging" / "assets" / "mumu-runtime.lock.json"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_lock(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"Unsupported MuMu asset lock schema: {payload.get('schema_version')!r}")
    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("MuMu asset lock must contain at least one asset.")
    return assets


def _asset_path(asset: dict) -> Path:
    path = (REPO_ROOT / str(asset["path"])).resolve()
    if REPO_ROOT not in path.parents:
        raise ValueError(f"MuMu asset path escapes the repository: {path}")
    return path


def _validate_payload(asset: dict, payload: bytes) -> str | None:
    expected_size = int(asset["size"])
    if len(payload) != expected_size:
        return f"size mismatch: expected {expected_size}, got {len(payload)}"
    actual_hash = _sha256(payload)
    expected_hash = str(asset["sha256"]).lower()
    if actual_hash != expected_hash:
        return f"sha256 mismatch: expected {expected_hash}, got {actual_hash}"
    return None


def validate_assets(lock_path: Path = DEFAULT_LOCK) -> list[str]:
    failures: list[str] = []
    for asset in _load_lock(lock_path):
        path = _asset_path(asset)
        if not path.is_file():
            failures.append(f"{asset['path']}: missing")
            continue
        failure = _validate_payload(asset, path.read_bytes())
        if failure:
            failures.append(f"{asset['path']}: {failure}")
    return failures


def fetch_assets(lock_path: Path = DEFAULT_LOCK, *, force: bool = False) -> None:
    for asset in _load_lock(lock_path):
        path = _asset_path(asset)
        if path.is_file() and not force:
            failure = _validate_payload(asset, path.read_bytes())
            if failure is None:
                print(f"Verified existing: {asset['path']}")
                continue
            print(f"Replacing invalid asset {asset['path']}: {failure}")

        print(f"Downloading: {asset['url']}")
        with urllib.request.urlopen(str(asset["url"]), timeout=60) as response:
            payload = response.read()
        failure = _validate_payload(asset, payload)
        if failure:
            raise ValueError(f"Downloaded MuMu asset {asset['path']} failed validation: {failure}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        print(f"Saved: {asset['path']} ({len(payload)} bytes, sha256={_sha256(payload)[:12]})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and verify locked MuMu runtime assets.")
    parser.add_argument("--force", action="store_true", help="Re-download files even when their hashes are valid.")
    parser.add_argument("--check", action="store_true", help="Only verify the locked files.")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()

    lock_path = args.lock_file.resolve()
    if args.check:
        failures = validate_assets(lock_path)
        if failures:
            print("MuMu runtime asset validation failed:", file=sys.stderr)
            for failure in failures:
                print(f"  {failure}", file=sys.stderr)
            return 1
        print("MuMu runtime assets match the lock.")
        return 0

    fetch_assets(lock_path, force=args.force)
    failures = validate_assets(lock_path)
    if failures:
        raise ValueError("MuMu assets are invalid after download: " + "; ".join(failures))
    print("MuMu runtime assets are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
