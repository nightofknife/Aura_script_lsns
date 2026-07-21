from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Iterable


CORE_PREFIXES = (
    ".github/workflows/",
    "packages/",
    "packaging/",
    "requirements/",
    "scripts/release/",
)
CORE_FILES = {
    "cli.py",
    "scripts/build_preflight.ps1",
    "scripts/build_release.ps1",
    "scripts/fetch_mumu_runtime_assets.py",
    "scripts/setup_python_runtime.ps1",
}


def normalize_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")


def classify_paths(paths: Iterable[str], forced_scope: str = "auto") -> dict[str, object]:
    normalized = sorted({normalize_path(path) for path in paths if normalize_path(path)})
    plan_changed = any(path.startswith("plans/") for path in normalized)
    core_changed = any(path in CORE_FILES or path.startswith(CORE_PREFIXES) for path in normalized)

    if forced_scope != "auto":
        scope = forced_scope
    elif core_changed:
        scope = "full"
    elif plan_changed:
        scope = "plan"
    else:
        scope = "none"

    return {
        "scope": scope,
        "plan_changed": plan_changed,
        "core_changed": core_changed,
        "changed_files": normalized,
    }


def git_changed_files(repo_root: Path, base_sha: str, head_sha: str) -> list[str]:
    base = str(base_sha or "").strip()
    head = str(head_sha or "HEAD").strip() or "HEAD"
    if not base or set(base) == {"0"}:
        command = ["git", "show", "--pretty=", "--name-only", head]
    else:
        command = ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", base, head]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def write_github_outputs(path: Path, result: dict[str, object]) -> None:
    lines = [
        f"scope={result['scope']}",
        f"plan_changed={str(result['plan_changed']).lower()}",
        f"core_changed={str(result['core_changed']).lower()}",
        f"changed_files_json={json.dumps(result['changed_files'], ensure_ascii=False, separators=(',', ':'))}",
    ]
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify Aura release-impacting changes.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--force", choices=("auto", "full", "plan", "none"), default="auto")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    paths = git_changed_files(repo_root, args.base, args.head)
    result = classify_paths(paths, args.force)
    if args.github_output:
        write_github_outputs(args.github_output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
