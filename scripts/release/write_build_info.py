from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import platform
from pathlib import Path

from scripts.release.release_contract import file_sha256, load_contract, parse_hashed_lock, tree_fingerprint


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def build_information(
    *,
    profile: str,
    label: str,
    source_commit: str,
    source_dirty: bool,
    contract_path: Path,
    lock_path: Path,
    ocr_root: Path | None,
    mumu_lock: Path | None,
) -> dict:
    contract = load_contract(contract_path)
    profile_contract = contract["profiles"][profile]
    lock_packages = parse_hashed_lock(lock_path)
    provider = profile_contract.get("execution_provider")
    result = {
        "schema_version": 1,
        "release_label": label,
        "profile": profile,
        "platform": contract["platform"],
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "contract_sha256": file_sha256(contract_path),
        "requirements_lock": lock_path.name,
        "requirements_lock_sha256": file_sha256(lock_path),
        "python_version": platform.python_version(),
        "dependencies": lock_packages,
        "pyinstaller_version": _installed_version("pyinstaller"),
        "pyside6_version": _installed_version("PySide6"),
        "onnxruntime_version": _installed_version("onnxruntime"),
        "onnxruntime_gpu_version": _installed_version("onnxruntime-gpu"),
        "execution_provider": provider,
        "built_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if profile == "overlay":
        result.update(
            {
                "target_profile": profile_contract["base_profile"],
                "target_release_directory": profile_contract["release_directory"].format(label=label),
                "cuda_major": profile_contract["cuda_major"],
                "cudnn_major": profile_contract["cudnn_major"],
            }
        )
    else:
        result.update(
            {
                "release_directory": profile_contract["release_directory"].format(label=label),
                "onnxruntime_distribution": profile_contract["onnxruntime_distribution"],
                "ocr_fingerprint": tree_fingerprint(ocr_root) if ocr_root and ocr_root.is_dir() else None,
                "mumu_lock_sha256": file_sha256(mumu_lock) if mumu_lock and mumu_lock.is_file() else None,
            }
        )
    return result


def write_information(root: Path, information: dict) -> Path:
    if information["profile"] == "overlay":
        destination = root / "runtime" / "_internal" / "nvidia" / "AURA-OVERLAY-INFO.json"
    else:
        destination = root / "BUILD-INFO.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(information, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if information["profile"] != "overlay":
        text_lines = [
            f"release_name={information['release_directory']}",
            f"release_label={information['release_label']}",
            f"release_profile={information['profile']}",
            f"source_commit={information['source_commit']}",
            f"source_dirty={str(information['source_dirty']).lower()}",
            f"contract_sha256={information['contract_sha256']}",
            f"python_version={information['python_version']}",
            f"pyinstaller_version={information['pyinstaller_version']}",
            f"pyside6_version={information['pyside6_version']}",
            f"onnxruntime_distribution={information['onnxruntime_distribution']}",
            f"execution_provider={information['execution_provider']}",
            "ocr_backend=onnxruntime",
            "paddle_stack=false",
            "base_path_mode=release_root",
            "entrypoint=run.ps1",
            "gui=true",
            "gui_entrypoint=AuraResonanceGui.exe",
            "gui_runtime=runtime\\AuraResonanceRuntime.exe",
        ]
        (root / "BUILD-INFO.txt").write_text("\n".join(text_lines) + "\n", encoding="utf-8")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Write reproducible Aura release build information.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", choices=("cpu", "gpu", "overlay"), required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-dirty", choices=("true", "false"), required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--ocr-root", type=Path)
    parser.add_argument("--mumu-lock", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    information = build_information(
        profile=args.profile,
        label=args.label,
        source_commit=args.source_commit,
        source_dirty=args.source_dirty == "true",
        contract_path=args.contract.resolve(),
        lock_path=args.lock.resolve(),
        ocr_root=args.ocr_root.resolve() if args.ocr_root else None,
        mumu_lock=args.mumu_lock.resolve() if args.mumu_lock else None,
    )
    path = write_information(root, information)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
