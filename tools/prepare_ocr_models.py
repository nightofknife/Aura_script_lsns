"""Assemble the default OCR bundle from pinned official ONNX artifacts.

This build-time downloader requires only Aura's existing YAML/metadata libraries.
It does not import Paddle, ONNX, or ONNX Runtime, and does not execute models.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plans.aura_base.src.services.ocr_contract import OcrModelFiles, write_ocr_metadata  # noqa: E402
from tools.export_paddleocr_onnx import _load_model_config, build_ocr_metadata  # noqa: E402


MODEL_FILENAMES = {
    "det": "det.onnx",
    "rec": "rec.onnx",
    "textline_orientation": "textline_orientation.onnx",
}


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _download(opener, url: str, destination: Path, expected_hash: str, *, offline: bool) -> None:
    if urlparse(url).scheme != "https":
        raise ValueError("Official OCR model artifacts require HTTPS URLs.")
    digest = str(expected_hash).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"Missing or invalid SHA256 for {destination.name}.")
    if destination.is_file() and _sha256(destination) == digest:
        return
    if offline:
        raise ValueError(f"OCR artifact is missing or has a different SHA256 in offline mode: {destination}.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".download")
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": "Aura-OCR-Model-Downloader"})
            with opener.open(request, timeout=60) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            if _sha256(partial) != digest:
                raise ValueError(f"Official OCR artifact SHA256 mismatch: {destination.name}.")
            partial.replace(destination)
            return
        except (OSError, URLError, ValueError):
            partial.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(min(2 ** attempt, 4))


def prepare_ocr_models(
    *, manifest_path: Path, destination_root: Path, proxy: str | None = None, offline: bool = False,
) -> Path:
    source = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if source.get("schema_version") != 1:
        raise ValueError("Unsupported OCR source manifest schema version.")
    if (source.get("family"), source.get("variant")) != ("ppocrv6", "small"):
        raise ValueError("The official OCR downloader expects the PP-OCRv6 small model family.")
    name = str(source.get("name") or "")
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("OCR bundle name must be a single directory name.")

    bundle = destination_root.resolve() / name
    bundle.mkdir(parents=True, exist_ok=True)
    opener = build_opener(ProxyHandler({"https": proxy, "http": proxy})) if proxy else build_opener()
    configs = {}
    for key, filename in MODEL_FILENAMES.items():
        artifact = source["files"][key]
        config_dir = bundle / ".sources" / key
        _download(
            opener, artifact["config_url"], config_dir / "inference.yml", artifact["config_sha256"],
            offline=offline,
        )
        config = _load_model_config(config_dir)
        if config.get("Global", {}).get("model_name") != artifact["model_name"]:
            raise ValueError(f"Unexpected official OCR model configuration for {key}.")
        configs[key] = config
        _download(opener, artifact["model_url"], bundle / filename, artifact["model_sha256"], offline=offline)

    metadata = build_ocr_metadata(
        det_config=configs["det"],
        rec_config=configs["rec"],
        textline_config=configs["textline_orientation"],
        models=OcrModelFiles(**MODEL_FILENAMES),
    )
    write_ocr_metadata(bundle / "ocr.meta.json", metadata)
    (bundle / "sources.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "packaging/ocr/ppocrv6_small.json")
    parser.add_argument("--destination-root", type=Path, default=REPO_ROOT / "models/ocr")
    parser.add_argument("--proxy", default=None, help="Optional HTTP(S) proxy for artifact downloads.")
    parser.add_argument("--offline", action="store_true", help="Require all pinned artifacts to exist locally.")
    args = parser.parse_args()
    bundle = prepare_ocr_models(
        manifest_path=args.manifest,
        destination_root=args.destination_root,
        proxy=args.proxy,
        offline=args.offline,
    )
    print(json.dumps({"model_dir": str(bundle), "model": bundle.name, "source": "official_onnx"}))


if __name__ == "__main__":
    main()
