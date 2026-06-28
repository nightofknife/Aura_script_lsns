from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared import (
    build_overlay_config,
    normalize_payload,
    plan_path,
    plan_scope,
    suppress_framework_console_logs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a plan states_map.yaml, validate referenced tasks, and render the state graph."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--plan", help="Plan name under plans/<plan>.")
    source_group.add_argument("--path", type=Path, help="Direct states_map.yaml path.")
    parser.add_argument("--format", choices=("text", "json", "mermaid"), default="text", help="Output format.")
    return parser


def inspect_state_map(*, plan_name: str | None, state_map_path: Path) -> dict[str, Any]:
    from packages.aura_core.context.state.planner import StateMap
    from packages.aura_core.packaging.core.task_loader import TaskLoader
    from packages.aura_core.types import TaskReference

    if not state_map_path.is_file():
        raise FileNotFoundError(f"State map not found: {state_map_path}")

    with open(state_map_path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"State map must be a YAML object: {state_map_path}")

    with suppress_framework_console_logs():
        state_map = StateMap(data)

    states: dict[str, Any] = dict(state_map.states or {})
    transitions: list[dict[str, Any]] = list(state_map.transitions or [])

    loader = None
    all_tasks: dict[str, Any] = {}
    if plan_name:
        with suppress_framework_console_logs():
            loader = TaskLoader(plan_name=plan_name, plan_path=plan_path(plan_name))
            all_tasks = loader.get_all_task_definitions()

    inbound = defaultdict(int)
    outbound = defaultdict(int)
    edges: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    for transition in transitions:
        from_state = str(transition.get("from") or "").strip()
        to_state = str(transition.get("to") or "").strip()
        transition_task = transition.get("transition_task")
        cost = transition.get("cost", 1)
        edges.append({"from": from_state, "to": to_state, "task": transition_task, "cost": cost})

        if from_state not in states:
            findings.append(_finding("error", "state_transition_from_missing", f"Transition references unknown from-state '{from_state}'."))
        if to_state not in states:
            findings.append(_finding("error", "state_transition_to_missing", f"Transition references unknown to-state '{to_state}'."))
        if from_state in states:
            outbound[from_state] += 1
        if to_state in states:
            inbound[to_state] += 1
        if from_state and to_state and from_state == to_state:
            findings.append(_finding("warn", "state_self_cycle", f"State '{from_state}' has a self-cycle transition.", transition))

        if plan_name and transition_task and not _task_exists(loader, plan_name, str(transition_task)):
            findings.append(
                _finding(
                    "error",
                    "state_transition_task_missing",
                    f"Transition task '{transition_task}' cannot be resolved inside plan '{plan_name}'.",
                    transition,
                )
            )

    for state_name, state_data in states.items():
        if not isinstance(state_data, dict):
            findings.append(_finding("error", "state_invalid_payload", f"State '{state_name}' must be an object."))
            continue
        check_task = state_data.get("check_task")
        if not check_task:
            findings.append(_finding("warn", "state_check_task_missing", f"State '{state_name}' does not define check_task."))
        elif plan_name and not _task_exists(loader, plan_name, str(check_task)):
            findings.append(
                _finding(
                    "error",
                    "state_check_task_missing_target",
                    f"check_task '{check_task}' for state '{state_name}' cannot be resolved inside plan '{plan_name}'.",
                )
            )

        if inbound[state_name] == 0:
            findings.append(_finding("info", "state_no_inbound", f"State '{state_name}' has no inbound transitions."))
        if outbound[state_name] == 0:
            findings.append(_finding("info", "state_no_outbound", f"State '{state_name}' has no outbound transitions."))

    reachable = _reachable_states(states, edges)
    for state_name in sorted(states):
        if state_name not in reachable:
            findings.append(_finding("warn", "state_unreachable", f"State '{state_name}' is unreachable from graph roots."))

    requires_initial_state_tasks: list[dict[str, Any]] = []
    for task_id, task_def in all_tasks.items():
        if not isinstance(task_def, dict):
            continue
        meta = task_def.get("meta") if isinstance(task_def.get("meta"), dict) else {}
        required_state = meta.get("requires_initial_state")
        if required_state:
            requires_initial_state_tasks.append({"task_id": task_id, "required_state": required_state})
            if required_state not in states:
                findings.append(
                    _finding(
                        "error",
                        "requires_initial_state_unknown",
                        f"Task '{task_id}' requires unknown initial state '{required_state}'.",
                    )
                )

    report = {
        "plan_name": plan_name,
        "state_map_path": str(state_map_path),
        "summary": {
            "state_count": len(states),
            "transition_count": len(transitions),
            "root_states": sorted(state for state in states if inbound[state] == 0),
            "leaf_states": sorted(state for state in states if outbound[state] == 0),
            "requires_initial_state_tasks": requires_initial_state_tasks,
            "findings": {
                "errors": sum(1 for item in findings if item["severity"] == "error"),
                "warnings": sum(1 for item in findings if item["severity"] == "warn"),
                "infos": sum(1 for item in findings if item["severity"] == "info"),
            },
        },
        "states": states,
        "transitions": transitions,
        "findings": findings,
        "mermaid": render_mermaid(states, edges),
    }
    return normalize_payload(report)


def _task_exists(loader, plan_name: str, task_ref: str) -> bool:
    from packages.aura_core.types import TaskReference

    try:
        reference = TaskReference.from_string(task_ref, default_package=plan_name)
    except Exception:
        return False
    if loader is None:
        return False
    task_data = loader.get_task_data(reference.as_loader_path())
    return isinstance(task_data, dict) and isinstance(task_data.get("steps"), dict)


def _reachable_states(states: dict[str, Any], edges: list[dict[str, Any]]) -> set[str]:
    roots = {state for state in states if all(edge["to"] != state for edge in edges)}
    if not roots:
        roots = set(states)
    adjacency = defaultdict(list)
    for edge in edges:
        adjacency[edge["from"]].append(edge["to"])
    visited: set[str] = set()
    queue = deque(sorted(roots))
    while queue:
        state = queue.popleft()
        if state in visited or state not in states:
            continue
        visited.add(state)
        for neighbor in adjacency.get(state, []):
            if neighbor not in visited:
                queue.append(neighbor)
    return visited


def _finding(severity: str, code: str, message: str, detail: Any | None = None) -> dict[str, Any]:
    payload = {"severity": severity, "code": code, "message": message}
    if detail is not None:
        payload["detail"] = detail
    return payload


def render_mermaid(states: dict[str, Any], edges: list[dict[str, Any]]) -> str:
    lines = ["flowchart TD"]
    for state_name in states:
        lines.append(f"    {sanitize_id(state_name)}[\"{state_name}\"]")
    for edge in edges:
        from_id = sanitize_id(edge["from"])
        to_id = sanitize_id(edge["to"])
        label = edge.get("task") or ""
        if label:
            safe_label = str(label).replace('"', "'")
            lines.append(f"    {from_id} -->|\"{safe_label}\"| {to_id}")
        else:
            lines.append(f"    {from_id} --> {to_id}")
    return "\n".join(lines)


def sanitize_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(value))
    return cleaned or "state"


def render_text(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        f"Plan: {report.get('plan_name') or '-'}",
        f"State map: {report.get('state_map_path')}",
        f"States: {summary.get('state_count')}  Transitions: {summary.get('transition_count')}",
        f"Roots: {', '.join(summary.get('root_states') or []) or '-'}",
        f"Leaves: {', '.join(summary.get('leaf_states') or []) or '-'}",
    ]

    requires_tasks = summary.get("requires_initial_state_tasks") or []
    if requires_tasks:
        lines.append("Tasks requiring initial state:")
        for item in requires_tasks:
            lines.append(f"- {item.get('task_id')}: {item.get('required_state')}")

    findings = report.get("findings") or []
    if findings:
        lines.append("")
        lines.append("Findings:")
        for item in findings:
            lines.append(f"- [{item.get('severity')}] {item.get('code')}: {item.get('message')}")
    else:
        lines.append("")
        lines.append("Findings: none")

    return "\n".join(lines)


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.plan:
            report = inspect_state_map(plan_name=args.plan, state_map_path=plan_path(args.plan) / "states_map.yaml")
        else:
            report = inspect_state_map(plan_name=None, state_map_path=args.path)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.format == "mermaid":
        print(report["mermaid"])
    else:
        print(render_text(report))
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
