from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import yaml


# Development plans are discovered locally but never distributed to players.
RUNTIME_PLANS = frozenset({"aura_base", "resonance_pc"})

ROOT_FILES = {"manifest.yaml", "config.yaml", "requirements.txt"}
ROOT_SUFFIXES = {".py"}
CONTENT_DIRS = {"src", "tasks", "templates", "assets"}
DATA_DIRS = {"meta", "input_profiles"}
# Runtime assets outside the general metadata directories. Keep this explicit:
# training checkpoints, model experiments and cache files are not release data.
DATA_FILES = {
    "data/deep_dive_events/healing.json",
    "data/deep_dive_vortex/catalog.json",
    "data/four_views_geometry.json",
    "data/models/deep_dive_entities.onnx",
    "data/models/deep_dive_entities.json",
}
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
RETIRED_PACKAGE_PATHS = (("assets", "mumu"), ("src", "platform", "mumu"))


def _runtime_task_definitions(source: Path) -> dict:
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected task definitions mapping: {source}")
    return {
        name: definition for name, definition in payload.items()
        if not (isinstance(definition, dict)
                and definition.get("meta", {}).get("role") == "developer")
    }


def _is_allowed_package_file(relative: Path) -> bool:
    parts = relative.parts
    lowered = tuple(part.lower() for part in parts)
    if any(lowered[:len(prefix)] == prefix for prefix in RETIRED_PACKAGE_PATHS):
        return False
    if any(part in EXCLUDED_DIRS for part in lowered[:-1]):
        return False
    name = relative.name.lower()
    if name.startswith(".env") or name in FORBIDDEN_CREDENTIAL_NAMES or relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if len(parts) == 1:
        return name in ROOT_FILES or relative.suffix.lower() in ROOT_SUFFIXES
    if lowered[0] in CONTENT_DIRS:
        return True
    if "/".join(lowered) in DATA_FILES:
        return True
    return len(parts) >= 3 and lowered[0] == "data" and lowered[1] in DATA_DIRS


def collect_plan_files(plans_root: Path) -> list[tuple[Path, Path]]:
    if not plans_root.is_dir():
        raise FileNotFoundError(f"Plans directory not found: {plans_root}")
    selected: list[tuple[Path, Path]] = []
    for root_file in sorted(plans_root.glob("*.py")):
        selected.append((root_file, Path("plans") / root_file.name))
    package_dirs = sorted(
        path for path in plans_root.iterdir() if path.is_dir() and path.name in RUNTIME_PLANS and (path / "manifest.yaml").is_file()
    )
    if not package_dirs:
        raise RuntimeError(f"No plan packages with manifest.yaml found under {plans_root}")
    for package_dir in package_dirs:
        for source in sorted(path for path in package_dir.rglob("*") if path.is_file()):
            relative = source.relative_to(package_dir)
            if _is_allowed_package_file(relative):
                if relative.parts[0] == "tasks" and relative.suffix in {".yaml", ".yml"}:
                    if not _runtime_task_definitions(source):
                        continue
                selected.append((source, Path("plans") / package_dir.name / relative))
    included = {destination.parent.name for _, destination in selected if destination.name == "manifest.yaml"}
    expected = set(RUNTIME_PLANS)
    if included != expected:
        raise RuntimeError(f"Release Plan tree is missing manifests for: {', '.join(sorted(expected - included))}")
    return selected


def validate_selected_files(files: list[tuple[Path, Path]]) -> None:
    forbidden_fragments = ("/data/cache/", "/data/state/", "/__pycache__/", "/logs/")
    for _, relative in files:
        if len(relative.parts) > 2 and relative.parts[1] not in RUNTIME_PLANS:
            raise RuntimeError(f"Non-runtime Plan selected for full release: {relative}")
        package_parts = tuple(part.lower() for part in relative.parts[2:])
        if any(package_parts[:len(prefix)] == prefix for prefix in RETIRED_PACKAGE_PATHS):
            raise RuntimeError(f"Retired platform selected for full release: {relative}")
        normalized = f"/{relative.as_posix().lower()}"
        if any(fragment in normalized for fragment in forbidden_fragments):
            raise RuntimeError(f"Forbidden runtime data selected for full release: {relative}")


def stage_plan_tree(files: list[tuple[Path, Path]], destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source, relative in files:
        if not relative.parts or relative.parts[0] != "plans":
            raise RuntimeError(f"Unexpected Plan staging path: {relative}")
        target = destination / Path(*relative.parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True)
        if len(relative.parts) > 3 and relative.parts[2] == "tasks" and relative.suffix in {".yaml", ".yml"}:
            payload = _runtime_task_definitions(source)
            target.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        else:
            shutil.copy2(source, target)
    # Generated manifests contain aliases for every local task, including
    # development wrappers. Keep exports aligned with the staged definitions.
    for manifest_path in destination.glob("*/manifest.yaml"):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("exports"), dict):
            continue
        exports = manifest["exports"]
        if not isinstance(exports.get("tasks"), list):
            continue
        retained = []
        for task in exports["tasks"]:
            source = task.get("source", "")
            task_source = manifest_path.parent / source
            if not task_source.is_file() or task.get("role") == "developer":
                continue
            definitions = _runtime_task_definitions(task_source)
            task_id = str(task.get("id", ""))
            if task_id in definitions or task_id.rsplit("/", 1)[-1] in definitions:
                retained.append(task)
        exports["tasks"] = retained
        manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble the filtered external Plan tree for a full release.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    files = collect_plan_files(args.repo_root.resolve() / "plans")
    validate_selected_files(files)
    stage_plan_tree(files, args.destination.resolve())
    total_bytes = sum(source.stat().st_size for source, _ in files)
    print(json.dumps({"files": len(files), "bytes": total_bytes}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
