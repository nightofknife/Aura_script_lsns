from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile
from hashlib import sha256

from scripts.release.prune_release_payload import classify_runtime_file, iter_files, path_is_file, prune_files, reset_release_runtime_data
from scripts.release.release_contract import file_sha256, load_contract, render_contract_value


def _normalized_entry(name: str) -> str:
    return name.replace("\\", "/").lstrip("./")


def validate_archive(archive_path: Path, *, expected_root: str, contract: dict) -> list[str]:
    archive_path = archive_path.resolve()
    if archive_path.stat().st_size > int(contract["archive"]["max_asset_bytes"]):
        raise ValueError(f"Archive exceeds the configured asset limit: {archive_path}")

    seen: set[str] = set()
    files: list[str] = []
    max_length = int(contract["archive"]["max_entry_length"])
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            name = _normalized_entry(entry.filename)
            pure = PurePosixPath(name)
            if not name or pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
                raise ValueError(f"Unsafe archive entry: {entry.filename!r}")
            key = name.rstrip("/").casefold()
            if key in seen:
                raise ValueError(f"Case-insensitive duplicate archive entry: {name}")
            seen.add(key)
            if len(name) > max_length:
                raise ValueError(f"Archive entry exceeds {max_length} characters: {name}")
            if pure.parts[0] != expected_root:
                raise ValueError(f"Archive entry is outside expected root {expected_root!r}: {name}")
            if not entry.is_dir():
                files.append(name)
    if not files:
        raise ValueError(f"Archive contains no files: {archive_path}")
    return files


def validate_archive_matches_tree(archive_path: Path, release_root: Path) -> None:
    release_root = release_root.resolve()
    tree = {path.relative_to(release_root).as_posix(): file_sha256(path) for path in iter_files(release_root)}
    archived: dict[str, str] = {}
    prefix = release_root.name + "/"
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            name = _normalized_entry(entry.filename)
            if entry.is_dir():
                continue
            relative = name[len(prefix) :] if name.startswith(prefix) else name
            digest = sha256()
            with archive.open(entry) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            archived[relative] = digest.hexdigest()
    if tree != archived:
        changed = sorted(path for path in set(tree) | set(archived) if tree.get(path) != archived.get(path))
        raise ValueError("Archive contents do not exactly match the validated release tree: " + ", ".join(changed[:20]))


def _read_build_info(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"Unsupported build info schema in {path}")
    return payload


def _assert_empty_logs(release_root: Path) -> None:
    logs = release_root / "logs"
    if logs.is_dir() and any(path_is_file(path) for path in logs.rglob("*")):
        raise ValueError(f"Release contains generated log files: {logs}")


def validate_full_release(release_root: Path, *, profile: str, label: str, contract: dict) -> dict:
    if profile not in {"cpu", "gpu"}:
        raise ValueError(f"Full release validation does not support profile {profile!r}")
    release_root = release_root.resolve()
    expected_name = render_contract_value(contract["profiles"][profile]["release_directory"], label=label)
    if release_root.name != expected_name:
        raise ValueError(f"Release root must be named {expected_name!r}, got {release_root.name!r}")

    for relative in contract["full_release"]["required_paths"]:
        candidate = release_root / PurePosixPath(relative)
        if not candidate.exists():
            raise ValueError(f"Release is missing required path: {relative}")
    for relative in contract["full_release"]["forbidden_paths"]:
        candidate = release_root / PurePosixPath(relative)
        if candidate.exists():
            raise ValueError(f"Release contains forbidden path: {relative}")

    runtime = release_root / "runtime"
    prune_files(runtime, classify_runtime_file, check_only=True)
    reset_release_runtime_data(release_root, check_only=True)
    _assert_empty_logs(release_root)

    for path in iter_files(release_root):
        lowered_parts = tuple(part.lower() for part in path.relative_to(release_root).parts)
        if "__pycache__" in lowered_parts or path.suffix.lower() in {".pyc", ".pyo"}:
            raise ValueError(f"Release contains Python cache data: {path}")
        if path.name.lower() == ".env" or path.name.lower().startswith(".env."):
            raise ValueError(f"Release contains environment credentials: {path}")

    config = (release_root / "config.yaml").read_text(encoding="utf-8-sig")
    expected_provider = contract["profiles"][profile]["execution_provider"]
    if not re.search(rf"(?m)^\s*execution_provider:\s*{re.escape(expected_provider)}\s*$", config):
        raise ValueError(f"Release config does not select OCR provider {expected_provider!r}")

    internal = runtime / "_internal"
    cuda_provider = internal / "onnxruntime" / "capi" / "onnxruntime_providers_cuda.dll"
    cpu_dist = list(internal.glob("onnxruntime-*.dist-info"))
    gpu_dist = list(internal.glob("onnxruntime_gpu-*.dist-info"))
    if profile == "cpu":
        if not cpu_dist or gpu_dist or cuda_provider.exists():
            raise ValueError("CPU release ONNX Runtime distribution/provider contents are invalid.")
    else:
        if cpu_dist or not gpu_dist or not cuda_provider.is_file():
            raise ValueError("GPU release ONNX Runtime distribution/provider contents are invalid.")

    info = _read_build_info(release_root / "BUILD-INFO.json")
    if info.get("profile") != profile or info.get("release_label") != label:
        raise ValueError("BUILD-INFO.json does not match the selected profile and release label.")
    if not info.get("contract_sha256"):
        raise ValueError("BUILD-INFO.json is missing the release contract fingerprint.")
    if info.get("execution_provider") != expected_provider:
        raise ValueError("BUILD-INFO.json records the wrong execution provider.")
    if info.get("onnxruntime_distribution") != contract["profiles"][profile]["onnxruntime_distribution"]:
        raise ValueError("BUILD-INFO.json records the wrong ONNX Runtime distribution.")
    if not str(info.get("python_version", "")).startswith(contract["python"]["major_minor"] + "."):
        raise ValueError("BUILD-INFO.json records an unsupported Python version.")
    return info


def validate_overlay_tree(overlay_root: Path, *, label: str, contract: dict) -> dict:
    overlay_root = overlay_root.resolve()
    profile = contract["profiles"]["overlay"]
    expected_name = render_contract_value(profile["release_directory"], label=label)
    if overlay_root.name != expected_name:
        raise ValueError(f"Overlay root must be named {expected_name!r}, got {overlay_root.name!r}")

    allowed_root_file = overlay_root / "NVIDIA-RUNTIME-OVERLAY.txt"
    nvidia_root = overlay_root / "runtime" / "_internal" / "nvidia"
    info_path = nvidia_root / "AURA-OVERLAY-INFO.json"
    if not allowed_root_file.is_file() or not info_path.is_file():
        raise ValueError("Overlay is missing its notice or machine-readable build information.")

    for path in iter_files(overlay_root):
        relative = path.relative_to(overlay_root).as_posix()
        if path != allowed_root_file and nvidia_root not in path.parents:
            raise ValueError(f"Overlay contains a file outside the NVIDIA runtime payload: {relative}")
        parts = tuple(part.lower() for part in path.parts)
        if "include" in parts or path.suffix.lower() in {".h", ".hpp", ".lib", ".a", ".exp", ".pdb", ".pyc", ".pyo"}:
            raise ValueError(f"Overlay contains a development or cache file: {relative}")

    for relative in profile["required_dlls"]:
        if not (overlay_root / PurePosixPath(relative)).is_file():
            raise ValueError(f"Overlay is missing required DLL: {relative}")
    for pattern in profile["required_globs"]:
        if not list(overlay_root.glob(pattern)):
            raise ValueError(f"Overlay does not match required DLL pattern: {pattern}")

    info = _read_build_info(info_path)
    if info.get("profile") != "overlay" or info.get("release_label") != label:
        raise ValueError("Overlay build information does not match its release label.")
    if info.get("cuda_major") != profile["cuda_major"] or info.get("cudnn_major") != profile["cudnn_major"]:
        raise ValueError("Overlay build information does not match the CUDA/cuDNN release contract.")
    return info


def _run_checked(args: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 180) -> str:
    completed = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode:
        raise ValueError(f"Packaged smoke command failed ({completed.returncode}): {' '.join(args)}\n{output}")
    return output


def run_runtime_smoke(release_root: Path) -> None:
    release_root = release_root.resolve()
    env = os.environ.copy()
    env.pop("AURA_BASE_PATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    run_script = str(release_root / "run.ps1")
    games = _run_checked(["pwsh", "-NoProfile", "-File", run_script, "games", "--all"], cwd=release_root, env=env)
    if '"game_name": "resonance"' not in games or '"game_name": "resonance_pc"' not in games:
        raise ValueError("Packaged CLI did not discover both Resonance plans.")
    _run_checked(["pwsh", "-NoProfile", "-File", run_script, "tasks", "resonance"], cwd=release_root, env=env)
    _run_checked(["pwsh", "-NoProfile", "-File", run_script, "tasks", "resonance_pc"], cwd=release_root, env=env)
    _run_checked(
        ["pwsh", "-NoProfile", "-File", run_script, "doctor", "--ocr", "--ocr-provider", "cpu"],
        cwd=release_root,
        env=env,
    )
    _run_checked([str(release_root / "runtime" / "AuraResonanceRuntime.exe"), "--self-check"], cwd=release_root, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate one Aura release profile and its archive.")
    parser.add_argument("--profile", choices=("cpu", "gpu", "overlay"), required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--runtime-smoke", action="store_true")
    args = parser.parse_args()

    contract = load_contract(args.contract)
    if args.profile == "overlay":
        validate_overlay_tree(args.release_root, label=args.label, contract=contract)
    else:
        validate_full_release(args.release_root, profile=args.profile, label=args.label, contract=contract)
        if args.runtime_smoke:
            run_runtime_smoke(args.release_root)
    if args.archive:
        validate_archive(args.archive, expected_root=args.release_root.name, contract=contract)
        validate_archive_matches_tree(args.archive, args.release_root)
    print(f"Validated Aura release profile: {args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
