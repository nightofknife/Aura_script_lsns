from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path


ROOT_FILES = {"manifest.yaml", "config.yaml", "requirements.txt"}
ROOT_SUFFIXES = {".py"}
CONTENT_DIRS = {"src", "tasks", "templates", "assets"}
DATA_DIRS = {"meta", "input_profiles"}
EXCLUDED_DIRS = {
    "__pycache__",
    ".pytest_cache",
    "cache",
    "state",
    "logs",
    "artifacts",
    "screenshots",
    "tmp",
    "temp",
    ".runtime",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".sqlite3", ".tmp"}
FORBIDDEN_CREDENTIAL_NAMES = {"auth.json", "credentials.json", "secrets.json", "tokens.json"}


def _is_allowed_package_file(relative: Path) -> bool:
    parts = relative.parts
    lowered = tuple(part.lower() for part in parts)
    if any(part in EXCLUDED_DIRS for part in lowered[:-1]):
        return False
    name = relative.name.lower()
    if (
        name.startswith(".env")
        or name in FORBIDDEN_CREDENTIAL_NAMES
        or relative.suffix.lower() in EXCLUDED_SUFFIXES
    ):
        return False
    if len(parts) == 1:
        return name in ROOT_FILES or relative.suffix.lower() in ROOT_SUFFIXES
    if lowered[0] in CONTENT_DIRS:
        return True
    return len(parts) >= 3 and lowered[0] == "data" and lowered[1] in DATA_DIRS


def collect_plan_files(plans_root: Path) -> list[tuple[Path, Path]]:
    if not plans_root.is_dir():
        raise FileNotFoundError(f"Plans directory not found: {plans_root}")

    selected: list[tuple[Path, Path]] = []
    for root_file in sorted(plans_root.glob("*.py")):
        selected.append((root_file, Path("plans") / root_file.name))

    package_dirs = sorted(
        path for path in plans_root.iterdir() if path.is_dir() and (path / "manifest.yaml").is_file()
    )
    if not package_dirs:
        raise RuntimeError(f"No plan packages with manifest.yaml found under {plans_root}")

    for package_dir in package_dirs:
        for source in sorted(path for path in package_dir.rglob("*") if path.is_file()):
            relative = source.relative_to(package_dir)
            if _is_allowed_package_file(relative):
                selected.append((source, Path("plans") / package_dir.name / relative))

    included_manifests = {destination.parent.name for _, destination in selected if destination.name == "manifest.yaml"}
    expected_manifests = {path.name for path in package_dirs}
    if included_manifests != expected_manifests:
        missing = sorted(expected_manifests - included_manifests)
        raise RuntimeError(f"Plan package is missing manifests for: {', '.join(missing)}")
    return selected


def stage_plan_tree(files: list[tuple[Path, Path]], destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source, relative in files:
        if not relative.parts or relative.parts[0] != "plans":
            raise RuntimeError(f"Unexpected Plan staging path: {relative}")
        target = destination / Path(*relative.parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def create_archive(files: list[tuple[Path, Path]], archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, relative in files:
            archive.write(source, relative.as_posix())


def validate_selected_files(files: list[tuple[Path, Path]]) -> None:
    forbidden_fragments = ("/data/cache/", "/data/state/", "/__pycache__/", "/logs/")
    for _, relative in files:
        normalized = f"/{relative.as_posix().lower()}"
        if any(fragment in normalized for fragment in forbidden_fragments):
            raise RuntimeError(f"Forbidden runtime data selected for Plan package: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the filtered external Aura plans tree.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.destination is None and args.archive is None:
        parser.error("at least one of --destination or --archive is required")

    repo_root = args.repo_root.resolve()
    files = collect_plan_files(repo_root / "plans")
    validate_selected_files(files)
    if args.destination:
        stage_plan_tree(files, args.destination.resolve())
    if args.archive:
        create_archive(files, args.archive.resolve())

    total_bytes = sum(source.stat().st_size for source, _ in files)
    print(json.dumps({"files": len(files), "bytes": total_bytes}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
