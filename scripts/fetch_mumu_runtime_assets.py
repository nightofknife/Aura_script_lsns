from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = REPO_ROOT / "plans" / "aura_base" / "assets" / "mumu"

SCRCPY_SERVER_URL = "https://github.com/Genymobile/scrcpy/releases/download/v1.24/scrcpy-server-v1.24"
SCRCPY_SERVER_PATH = ASSET_ROOT / "scrcpy" / "scrcpy-server-v1.24.jar"

ANDROID_TOUCH_URLS = {
    "arm64-v8a": "https://raw.githubusercontent.com/BobbleKeyboard/android_touch/master/libs/arm64-v8a/touch",
    "armeabi-v7a": "https://raw.githubusercontent.com/BobbleKeyboard/android_touch/master/libs/armeabi-v7a/touch",
    "x86": "https://raw.githubusercontent.com/BobbleKeyboard/android_touch/master/libs/x86/touch",
    "x86_64": "https://raw.githubusercontent.com/BobbleKeyboard/android_touch/master/libs/x86_64/touch",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch bundled MuMu runtime assets into the repo.")
    parser.add_argument("--force", action="store_true", help="Re-download files even when they already exist.")
    parser.add_argument("--check", action="store_true", help="Only verify that the expected files exist.")
    args = parser.parse_args()

    expected_files = [SCRCPY_SERVER_PATH, *[ASSET_ROOT / "android_touch" / abi / "touch" for abi in ANDROID_TOUCH_URLS]]
    if args.check:
        missing = [str(path) for path in expected_files if not path.is_file()]
        if missing:
            print("Missing MuMu runtime assets:", file=sys.stderr)
            for path in missing:
                print(f"  {path}", file=sys.stderr)
            return 1
        print("MuMu runtime assets are present.")
        return 0

    _download_file(SCRCPY_SERVER_URL, SCRCPY_SERVER_PATH, force=args.force)
    for abi, url in ANDROID_TOUCH_URLS.items():
        _download_file(url, ASSET_ROOT / "android_touch" / abi / "touch", force=args.force)
    print("MuMu runtime assets are ready.")
    return 0


def _download_file(url: str, path: Path, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not force:
        print(f"Skip existing: {path}")
        return
    print(f"Downloading: {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()
    path.write_bytes(payload)
    print(f"Saved: {path} ({len(payload)} bytes, sha256={hashlib.sha256(payload).hexdigest()[:12]})")


if __name__ == "__main__":
    raise SystemExit(main())
