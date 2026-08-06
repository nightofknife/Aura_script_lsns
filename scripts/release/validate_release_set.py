from __future__ import annotations

import argparse
from fnmatch import fnmatch
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from scripts.release.prune_release_payload import iter_files
from scripts.release.release_contract import file_sha256, load_contract, parse_hashed_lock, render_contract_value
from scripts.release.validate_release import validate_archive, validate_full_release, validate_overlay_tree


def _inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in iter_files(root)
    }


def _matches_any(path: str, patterns: list[str]) -> bool:
    lowered = path.lower()
    return any(fnmatch(lowered, pattern.lower()) for pattern in patterns)


def _compare_exact_tree(cpu_root: Path, gpu_root: Path, relative: str) -> None:
    cpu = cpu_root / relative
    gpu = gpu_root / relative
    if cpu.is_dir() and gpu.is_dir():
        cpu_inventory = _inventory(cpu)
        gpu_inventory = _inventory(gpu)
        if cpu_inventory != gpu_inventory:
            raise ValueError(f"CPU and GPU releases differ under required common tree: {relative}")
        return
    if cpu.is_file() and gpu.is_file() and file_sha256(cpu) == file_sha256(gpu):
        return
    raise ValueError(f"CPU and GPU releases do not share identical required payload: {relative}")


def _normalized_config(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    return re.sub(r"(?m)^(\s*execution_provider:)\s*\S+\s*$", r"\1 <profile>", text)


def _equivalent_generated_metadata(relative: str, cpu_root: Path, gpu_root: Path) -> bool:
    lowered = relative.lower()
    if not re.fullmatch(r"runtime/_internal/[^/]+\.dist-info/record", lowered):
        return False
    cpu_lines = (cpu_root / relative).read_text(encoding="utf-8").splitlines()
    gpu_lines = (gpu_root / relative).read_text(encoding="utf-8").splitlines()
    normalize = lambda lines: [line for line in lines if not line.startswith("../../Scripts/")]
    return normalize(cpu_lines) == normalize(gpu_lines)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract(archive: Path, destination: Path, *, expected_root: str, contract: dict) -> Path:
    validate_archive(archive, expected_root=expected_root, contract=contract)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)
    root = destination / expected_root
    if not root.is_dir():
        raise ValueError(f"Archive did not extract to its declared root: {expected_root}")
    return root


def validate_release_set(
    *,
    cpu_archive: Path,
    gpu_archive: Path,
    overlay_archive: Path,
    label: str,
    contract_path: Path,
    allow_dirty: bool = False,
    work_root: Path | None = None,
) -> None:
    contract = load_contract(contract_path)
    supplied_archives = {"cpu": cpu_archive, "gpu": gpu_archive, "overlay": overlay_archive}
    for profile_name, archive in supplied_archives.items():
        expected_archive = render_contract_value(contract["artifacts"][profile_name], label=label)
        if archive.name != expected_archive:
            raise ValueError(f"{profile_name} archive must be named {expected_archive!r}, got {archive.name!r}")
    expected_names = {
        profile: render_contract_value(contract["profiles"][profile]["release_directory"], label=label)
        for profile in ("cpu", "gpu", "overlay")
    }

    temporary = None
    if work_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="aura-release-set-")
        work_root = Path(temporary.name)
    else:
        work_root = work_root.resolve()
        if work_root.exists():
            shutil.rmtree(work_root)
        work_root.mkdir(parents=True)

    try:
        cpu_root = _extract(cpu_archive, work_root / "cpu", expected_root=expected_names["cpu"], contract=contract)
        gpu_root = _extract(gpu_archive, work_root / "gpu", expected_root=expected_names["gpu"], contract=contract)
        overlay_root = _extract(
            overlay_archive,
            work_root / "overlay",
            expected_root=expected_names["overlay"],
            contract=contract,
        )

        cpu_info = validate_full_release(cpu_root, profile="cpu", label=label, contract=contract)
        gpu_info = validate_full_release(gpu_root, profile="gpu", label=label, contract=contract)
        overlay_info = validate_overlay_tree(overlay_root, label=label, contract=contract)
        infos = (cpu_info, gpu_info, overlay_info)

        for field in ("release_label", "source_commit", "contract_sha256"):
            values = {str(info.get(field)) for info in infos}
            if len(values) != 1:
                raise ValueError(f"Release artifacts disagree on {field}: {sorted(values)}")
        if next(iter({info["release_label"] for info in infos})) != label:
            raise ValueError("Release artifact label does not match the requested set label.")
        if any(bool(info.get("source_dirty")) for info in infos) and not allow_dirty:
            raise ValueError("Release set was built from a dirty source tree.")
        if cpu_info["contract_sha256"] != file_sha256(contract_path):
            raise ValueError("Release set was not built from the supplied release contract.")
        repo_root = contract_path.resolve().parent.parent
        for profile_name, info in (("cpu", cpu_info), ("gpu", gpu_info), ("overlay", overlay_info)):
            lock_path = repo_root / contract["profiles"][profile_name]["requirements_lock"]
            if info.get("requirements_lock_sha256") != file_sha256(lock_path):
                raise ValueError(f"{profile_name} artifact does not match its committed dependency lock.")
            if info.get("dependencies") != parse_hashed_lock(lock_path):
                raise ValueError(f"{profile_name} artifact dependency inventory does not match its lock.")
        if cpu_info.get("ocr_fingerprint") != gpu_info.get("ocr_fingerprint") or not cpu_info.get("ocr_fingerprint"):
            raise ValueError("CPU and GPU artifacts do not share the same OCR model fingerprint.")
        if cpu_info.get("mumu_lock_sha256") != gpu_info.get("mumu_lock_sha256") or not cpu_info.get("mumu_lock_sha256"):
            raise ValueError("CPU and GPU artifacts do not share the same MuMu asset lock fingerprint.")

        for relative in contract["comparison"]["exact_roots"]:
            _compare_exact_tree(cpu_root, gpu_root, relative)
        for relative in contract["comparison"]["exact_files"]:
            _compare_exact_tree(cpu_root, gpu_root, relative)
        if _normalized_config(cpu_root / "config.yaml") != _normalized_config(gpu_root / "config.yaml"):
            raise ValueError("CPU and GPU configs differ outside execution_provider.")

        cpu_dependencies = dict(cpu_info["dependencies"])
        gpu_dependencies = dict(gpu_info["dependencies"])
        cpu_dependencies.pop("onnxruntime", None)
        gpu_dependencies.pop("onnxruntime-gpu", None)
        if cpu_dependencies != gpu_dependencies:
            raise ValueError("CPU and GPU common dependency locks are inconsistent.")

        cpu_inventory = _inventory(cpu_root)
        gpu_inventory = _inventory(gpu_root)
        allowed = list(contract["comparison"]["allowed_cpu_gpu_differences"])
        invalid_differences = []
        for relative in sorted(set(cpu_inventory) | set(gpu_inventory)):
            if cpu_inventory.get(relative) == gpu_inventory.get(relative):
                continue
            if relative in cpu_inventory and relative in gpu_inventory and _equivalent_generated_metadata(
                relative, cpu_root, gpu_root
            ):
                continue
            if not _matches_any(relative, allowed):
                invalid_differences.append(relative)
        if invalid_differences:
            raise ValueError(
                "CPU and GPU payloads differ outside the release allowlist: "
                + ", ".join(invalid_differences[:20])
            )

        if overlay_info.get("target_profile") != "gpu" or overlay_info.get("target_release_directory") != gpu_root.name:
            raise ValueError("NVIDIA overlay does not target the selected GPU release.")
        overlay_inventory = _inventory(overlay_root)
        collisions = sorted(set(overlay_inventory) & set(gpu_inventory))
        if collisions:
            raise ValueError("NVIDIA overlay would overwrite GPU package files: " + ", ".join(collisions[:20]))

        merged_root = work_root / "merged" / gpu_root.name
        shutil.copytree(gpu_root, merged_root)
        for source in iter_files(overlay_root):
            destination = merged_root / source.relative_to(overlay_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if not (merged_root / "runtime" / "_internal" / "nvidia").is_dir():
            raise ValueError("Merged GPU package is missing the NVIDIA runtime directory.")
    finally:
        if temporary is not None:
            temporary.cleanup()


def write_checksums(paths: list[Path], destination: Path) -> None:
    lines = [f"{file_sha256(path)}  {path.name}" for path in sorted(paths, key=lambda item: item.name)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the CPU, GPU, and NVIDIA overlay release as one set.")
    parser.add_argument("--cpu", type=Path, required=True)
    parser.add_argument("--gpu", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--checksums", type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()

    archives = [args.cpu.resolve(), args.gpu.resolve(), args.overlay.resolve()]
    validate_release_set(
        cpu_archive=archives[0],
        gpu_archive=archives[1],
        overlay_archive=archives[2],
        label=args.label,
        contract_path=args.contract.resolve(),
        allow_dirty=args.allow_dirty,
        work_root=args.work_root,
    )
    if args.checksums:
        write_checksums(archives, args.checksums.resolve())
    print("Validated Aura CPU/GPU/overlay release set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
