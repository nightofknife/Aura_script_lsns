"""Select source CI suites from Git changes and trusted main workflow history."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.release.detect_release_scope import git_changed_files


GROUPS_FILE = Path(".github/ci-test-groups.json")
SUITES = ("tests/contracts", "tests/unit", "tests/smoke")
BASIC_SMOKE = {
    "tests/smoke/test_gui_smoke.py",
    "tests/smoke/test_import_smoke.py",
    "tests/smoke/test_package_discovery_smoke.py",
    "tests/smoke/test_runner_smoke.py",
    "tests/smoke/test_unicode_path_smoke.py",
}
TASK_CONTRACTS = [
    "tests/contracts/test_task_hierarchy_catalog.py",
    "tests/contracts/test_resonance_pc_legacy_entry_retirement.py",
]
ACTION_CONTRACT = "tests/contracts/test_async_plan_action_safety.py"
RELEASE_ASSEMBLY = "tests/unit/test_release_plan_assembly.py"
TEMPLATE_CONTRACTS = "tests/unit/test_functional_template*_release.py"
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass
class Selection:
    mode: str
    reason: str
    base: str = ""
    groups: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    main_run: int | None = None


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def is_ancestor(base: str, head: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        capture_output=True,
    ).returncode == 0


def main_runs(timeout: float = 10, **filters: str) -> list[dict]:
    repository = os.environ["GITHUB_REPOSITORY"]
    query = urlencode({"branch": "main", "event": "push", "per_page": "100", **filters})
    url = f"{os.environ['GITHUB_API_URL']}/repos/{repository}/actions/workflows/release.yml/runs?{query}"
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
    })
    with urlopen(request, timeout=timeout) as response:
        runs = json.load(response)["workflow_runs"]
    return [
        run for run in runs
        if run["event"] == "push"
        and run["head_branch"] == "main"
        and (run.get("head_repository") or {}).get("full_name") == repository
        and COMMIT_SHA.fullmatch(run["head_sha"])
        and type(run["id"]) is int
    ]


def successful_run_for_commit(head: str) -> int | None:
    deadline = time.monotonic() + 180
    while (remaining := deadline - time.monotonic()) > 0:
        runs = [
            run for run in main_runs(timeout=min(10, remaining), head_sha=head)
            if run["head_sha"] == head
        ]
        for run in runs:
            if run["status"] == "completed" and run["conclusion"] == "success":
                return run["id"]
        if all(run["status"] == "completed" for run in runs):
            return None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(10, remaining))
    return None


def latest_successful_ancestor(head: str) -> tuple[str, int] | None:
    for run in main_runs(status="success"):
        base = run["head_sha"]
        if (
            base != head
            and run["status"] == "completed"
            and run["conclusion"] == "success"
            and is_ancestor(base, head)
        ):
            return base, run["id"]
    return None


def patterns(value: object) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(pattern, str) or not pattern.strip() for pattern in value
    ):
        raise ValueError("expected a nonempty list of path patterns")
    return value


def matching_tests(inventory: set[str], test_patterns: list[str]) -> set[str]:
    selected = set()
    for pattern in test_patterns:
        matches = {path for path in inventory if fnmatchcase(path, pattern)}
        if not matches:
            raise ValueError(f"test pattern matches no tracked tests: {pattern!r}")
        selected.update(matches)
    return selected


def select_changed_tests(base: str, head: str, main_run: int | None) -> Selection:
    changed = git_changed_files(Path.cwd(), base, head)
    config = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    documentation = patterns(config["documentation"])
    inventory = {
        path for path in git("ls-files", "-z", "--", *SUITES).decode("utf-8").split("\0")
        if Path(path).name.startswith("test_") and path.endswith(".py")
    }
    if not BASIC_SMOKE <= inventory:
        raise ValueError("one or more required source smoke files are missing")
    groups = config["groups"]
    if not isinstance(groups, dict) or not groups:
        raise ValueError("expected at least one feature test group")
    group_paths = {}
    group_tests = {}
    for name, group in groups.items():
        group_paths[name] = patterns(group["paths"])
        group_tests[name] = matching_tests(inventory, patterns(group["tests"]))

    if not changed:
        return Selection("skip", "No file changes from the baseline.", base, main_run=main_run)
    relevant = [
        path for path in changed
        if not (
            path.startswith("docs/")
            and path.endswith(".md")
            and any(fnmatchcase(path, pattern) for pattern in documentation)
        )
    ]
    if not relevant:
        return Selection("skip", "Only allowlisted documentation changed.", base, main_run=main_run)

    selected = BASIC_SMOKE.copy()
    selected_groups = set()
    for path in relevant:
        hits = {
            name for name, source_patterns in group_paths.items()
            if path in group_tests[name] or any(fnmatchcase(path, pattern) for pattern in source_patterns)
        }
        if not hits:
            return Selection("full", f"No incremental mapping for {path!r}.", base, main_run=main_run)
        selected_groups.update(hits)
        for name in hits:
            selected.update(group_tests[name])
        if fnmatchcase(path, "plans/*/tasks/*.yaml"):
            selected.update(matching_tests(inventory, TASK_CONTRACTS))
        if fnmatchcase(path, "plans/*/src/actions/*.py"):
            selected.update(matching_tests(inventory, [ACTION_CONTRACT]))
        if fnmatchcase(path, "plans/*/templates/*"):
            selected.update(matching_tests(inventory, [RELEASE_ASSEMBLY, TEMPLATE_CONTRACTS]))

    return Selection(
        "incremental", "Selected tests from the changed paths.", base,
        tuple(sorted(selected_groups)), tuple(sorted(selected)), main_run,
    )


def select(head: str) -> Selection:
    event = os.environ["GITHUB_EVENT_NAME"]
    release_kind = os.environ["CI_RELEASE_KIND"]
    if os.environ["CI_TEST_SCOPE"] == "full":
        return Selection("full", "Full source CI requested.")
    if release_kind == "formal":
        return Selection("full", "Formal release requires full source CI.")

    if event == "pull_request":
        base = os.environ["CI_PR_BASE"]
        if not COMMIT_SHA.fullmatch(base) or git("rev-parse", "HEAD^1").decode().strip() != base:
            return Selection("full", "The PR base does not match the checked-out merge commit.")
        return select_changed_tests(base, head, None)

    if release_kind == "experimental":
        run_id = successful_run_for_commit(head)
        if run_id is not None:
            return Selection("reuse", "Source CI already passed for this main commit.", head, main_run=run_id)
    baseline = latest_successful_ancestor(head)
    if baseline is None:
        return Selection("full", "No successful main CI ancestor was found in recent history.")
    base, run_id = baseline
    return select_changed_tests(base, head, run_id)


def main() -> None:
    head = git("rev-parse", "HEAD").decode().strip()
    if head != os.environ["CI_SOURCE_COMMIT"]:
        raise RuntimeError("Source CI checkout differs from the resolved release commit.")
    try:
        selection = select(head)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError, AttributeError) as error:
        selection = Selection("full", f"Could not resolve incremental source CI ({type(error).__name__}): {error}")

    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        print(f"mode={selection.mode}", file=output)
        print(f"tests={json.dumps(selection.tests, separators=(',', ':'))}", file=output)

    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        print("### Source CI selection\n", file=summary)
        print(f"- Mode: `{selection.mode}`", file=summary)
        print(f"- Reason: {escape(selection.reason)}", file=summary)
        print(f"- Head: `{head}`", file=summary)
        if selection.base:
            print(f"- Baseline: `{selection.base}`", file=summary)
        if selection.groups:
            print(f"- Groups: {escape(', '.join(selection.groups))}", file=summary)
            print(f"- Selected test files: {len(selection.tests)}", file=summary)
        if selection.main_run is not None:
            repo_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            print(f"- [Main CI run]({repo_url}/actions/runs/{selection.main_run})", file=summary)
        print(file=summary)
    print(f"Source CI mode: {selection.mode}. {selection.reason}")


if __name__ == "__main__":
    main()
