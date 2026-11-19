from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import file_digest
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from packages.aura_core.release_manifest import (
    EXPERIMENTAL_INDEX_URL,
    LATEST_CHECKSUMS_URL,
    MAX_CHECKSUM_BYTES,
    RELEASES_URL,
    parse_cpu_manifest,
    release_version_key,
)
from packages.aura_core.update_mirror import MAX_MIRROR_ASSET_BYTES, MAX_REPLY_BYTES, NETWORK_ERRORS, parse_mirror_release


def read_source(url, limit):
    request = Request(url, headers={"User-Agent": "AuraResonanceMirror/1"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                content = response.read(limit + 1)
            if len(content) > limit:
                raise ValueError("Upstream metadata is too large")
            return content
        except NETWORK_ERRORS as exc:
            if attempt == 2 or isinstance(exc, HTTPError) and exc.code < 500:
                raise
            time.sleep(2 ** (attempt + 1))


def sync_target(channel):
    expected = None
    manifest_url = LATEST_CHECKSUMS_URL
    if channel == "experimental":
        index = json.loads(read_source(EXPERIMENTAL_INDEX_URL, MAX_REPLY_BYTES))
        expected = index.get("tag") if isinstance(index, dict) else None
        if not isinstance(expected, str) or len(expected) > 48 or expected != expected.strip():
            raise ValueError("Invalid experimental channel index")
        version = release_version_key(expected)
        if version is None or version.stable:
            raise ValueError("Invalid experimental channel tag")
        manifest_url = f"{RELEASES_URL}/download/{quote(expected, safe='')}/SHA256SUMS.txt"
    tag, checksums = parse_cpu_manifest(read_source(manifest_url, MAX_CHECKSUM_BYTES))
    version = release_version_key(tag)
    if len(tag) > 48 or version is None or version.stable != (channel == "stable"):
        raise ValueError("Upstream manifest returned the wrong channel")
    if expected is not None and tag != expected:
        raise ValueError("Upstream index and manifest disagree")
    filename = f"AuraResonance-{tag}-win-x64-cpu.zip"
    return tag, filename, checksums[filename.casefold()]


def verified_size(path, expected_digest):
    with path.open("r+b") as archive:
        size = os.fstat(archive.fileno()).st_size
        if not 0 < size <= MAX_MIRROR_ASSET_BYTES:
            raise ValueError("Unexpected release package size")
        if file_digest(archive, "sha256").hexdigest() != expected_digest:
            raise ValueError("SHA-256 verification failed")
        os.fsync(archive.fileno())
    return size


def download_package(partial, tag, filename, expected_digest):
    # A resumed partial belongs to the same immutable upstream checksum.
    cmd = [
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15",
        "--speed-limit", "1024", "--speed-time", "60",
        "--max-time", "1500", "--max-filesize", str(MAX_MIRROR_ASSET_BYTES),
        "--continue-at", "-", "--output", str(partial),
        f"{RELEASES_URL}/download/{quote(tag, safe='')}/{quote(filename, safe='')}",
    ]
    if partial.is_file():
        try:
            return verified_size(partial, expected_digest)
        except ValueError:
            pass
    result = subprocess.run(cmd, check=False)
    if result.returncode:
        if result.returncode in {22, 33, 63}:
            partial.unlink(missing_ok=True)
        result.check_returncode()
    try:
        return verified_size(partial, expected_digest)
    except ValueError:
        partial.unlink(missing_ok=True)
        raise


def publish_index(index, payload):
    pending = index.with_suffix(".new")
    with pending.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(index)


def prune_packages(versions, channel, keep):
    for directory in versions.iterdir():
        if directory.name == keep or directory.is_symlink() or not directory.is_dir():
            continue
        version = release_version_key(directory.name)
        if version is None or version.stable != (channel == "stable"):
            continue
        (directory / f"AuraResonance-{directory.name}-win-x64-cpu.zip").unlink(missing_ok=True)
        if not any(directory.iterdir()):
            directory.rmdir()


def sync_channel(root, channel):
    tag, filename, digest = sync_target(channel)
    versions = root / "releases" / "download"
    archive = versions / tag / filename
    index = root / "channels" / f"{channel}.json"
    stage = root / ".staging" / channel
    stage.mkdir(parents=True, exist_ok=True)
    index.parent.mkdir(parents=True, exist_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = stage / f"{tag}-{digest}.part"
    for old in stage.glob("*.part"):
        if old != partial:
            old.unlink()

    try:
        with index.open("rb") as stream:
            current = parse_mirror_release(json.loads(stream.read(MAX_REPLY_BYTES + 1)), channel)
        if current.tag == tag and current.sha256 == digest and archive.stat().st_size == current.size:
            partial.unlink(missing_ok=True)
            prune_packages(versions, channel, tag)
            print(f"{channel}: {tag} already ready", flush=True)
            return
    except (OSError, ValueError, RuntimeError):
        pass

    size = None
    if archive.exists():
        try:
            size = verified_size(archive, digest)
        except ValueError:
            pass
    if size is None:
        size = download_package(partial, tag, filename, digest)
        partial.replace(archive)
    else:
        partial.unlink(missing_ok=True)
    ready = parse_mirror_release({"tag": tag, "size": size, "sha256": digest}, channel)
    publish_index(index, asdict(ready))
    prune_packages(versions, channel, tag)
    print(f"{channel}: published and verified {tag} ({size} bytes)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Synchronize and verify Aura CPU releases from GitHub")
    parser.add_argument("--mirror", required=True, type=Path)
    args = parser.parse_args()
    failed = False
    for channel in ("stable", "experimental"):
        try:
            sync_channel(args.mirror, channel)
        except HTTPError as exc:
            if channel == "experimental" and exc.code == 404 and not (
                args.mirror / "channels" / "experimental.json"
            ).exists():
                print("experimental: no test release is available", flush=True)
            else:
                print(f"{channel}: upstream HTTP {exc.code}", flush=True)
                failed = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"{channel}: synchronization failed: {exc}", flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
