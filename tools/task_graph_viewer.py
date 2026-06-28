from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.aura_core.engine.graph_builder import GraphBuilder
from packages.aura_core.observability.logging.core_logger import logger as core_logger
from packages.aura_core.packaging.core.task_loader import TaskLoader
from packages.aura_core.packaging.core.task_validator import TaskDefinitionValidator
from packages.aura_core.types import TaskReference


class _StepState:
    PENDING = "PENDING"


class _DummyEngine:
    StepState = _StepState

    def __init__(self) -> None:
        self.nodes: dict[str, Any] = {}
        self.dependencies: dict[str, Any] = {}
        self.reverse_dependencies: dict[str, set[str]] = {}
        self.step_states: dict[str, Any] = {}
        self.node_metadata: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class LoadedTask:
    plan_name: str | None
    display_name: str
    source_path: Path
    task_key: str
    task_ref: str | None
    task_data: dict[str, Any]


@contextlib.contextmanager
def suppress_framework_console_logs():
    logger_obj = getattr(core_logger, "logger", None)
    if logger_obj is None:
        yield
        return

    console_handler = None
    previous_level = None
    for handler in logger_obj.handlers:
        if getattr(handler, "name", None) == "console":
            console_handler = handler
            previous_level = handler.level
            handler.setLevel(logging.CRITICAL + 1)
            break
    try:
        yield
    finally:
        if console_handler is not None and previous_level is not None:
            console_handler.setLevel(previous_level)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Aura task structure and render step dependency graphs."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--task-ref",
        help="Canonical task_ref, used together with --plan. Example: tasks:single_sleep.yaml:single_sleep",
    )
    source_group.add_argument(
        "--task",
        help="Loader-style task path, used together with --plan. Example: single_sleep or combat/open_map/task_name",
    )
    source_group.add_argument(
        "--path",
        type=Path,
        help="Direct YAML file path. Use --task-key when the file defines multiple tasks.",
    )
    parser.add_argument("--plan", help="Plan name under plans/<plan>. Required for --task-ref or --task.")
    parser.add_argument("--task-key", help="Task key used with --path for multi-task YAML files.")
    parser.add_argument(
        "--format",
        choices=("text", "json", "mermaid"),
        default="text",
        help="Output format. Defaults to human-readable text.",
    )
    return parser


def load_task_from_plan(plan_name: str, *, task_ref: str | None = None, task: str | None = None) -> LoadedTask:
    if not plan_name.strip():
        raise ValueError("--plan must not be empty")

    plan_path = REPO_ROOT / "plans" / plan_name
    if not plan_path.is_dir():
        raise FileNotFoundError(f"Plan not found: {plan_path}")

    with suppress_framework_console_logs():
        loader = TaskLoader(plan_name=plan_name, plan_path=plan_path)

        if task_ref:
            parsed = TaskReference.from_string(task_ref, default_package=plan_name)
            loader_path = parsed.as_loader_path()
            display_name = f"{plan_name}/{task_ref}"
            source_path = plan_path / parsed.as_file_path()
            if not source_path.is_file():
                raise FileNotFoundError(f"Task file not found: {source_path}")
            task_key = parsed.infer_task_key()
            canonical_ref = task_ref
        else:
            loader_path = str(task or "").strip()
            if not loader_path:
                raise ValueError("Either task_ref or task must be provided")
            display_name = f"{plan_name}/{loader_path}"
            source_path, task_key = resolve_loader_task_source(plan_path, loader_path)
            canonical_ref = infer_plan_task_ref(plan_path, source_path, task_key)

        task_data = loader.get_task_data(loader_path)
    if not isinstance(task_data, dict) or not isinstance(task_data.get("steps"), dict):
        error = loader.find_task_load_error(loader_path)
        if error:
            raise ValueError(error["message"])
        raise FileNotFoundError(f"Task definition not found for '{loader_path}' in plan '{plan_name}'")

    return LoadedTask(
        plan_name=plan_name,
        display_name=display_name,
        source_path=source_path,
        task_key=task_key,
        task_ref=canonical_ref,
        task_data=task_data,
    )


def resolve_loader_task_source(plan_path: Path, loader_path: str) -> tuple[Path, str]:
    parts = [part for part in loader_path.split("/") if part]
    if not parts:
        raise ValueError("Task path must not be empty")

    task_dir = plan_path / "tasks"
    task_key = parts[-1]
    direct_path = task_dir.joinpath(*parts).with_suffix(".yaml")
    if direct_path.is_file():
        return direct_path, task_key

    file_parts = parts[:-1] if len(parts) > 1 else parts
    file_path = task_dir.joinpath(*file_parts).with_suffix(".yaml")
    if file_path.is_file():
        return file_path, task_key

    raise FileNotFoundError(f"Task file not found for loader path '{loader_path}' under {task_dir}")


def infer_plan_task_ref(plan_path: Path, source_path: Path, task_key: str) -> str | None:
    try:
        relative = source_path.resolve().relative_to((plan_path / "tasks").resolve())
    except ValueError:
        return None

    canonical_ref = f"tasks:{relative.as_posix().replace('/', ':')}"
    if task_key != source_path.stem:
        canonical_ref = f"{canonical_ref}:{task_key}"
    return canonical_ref


def load_task_from_path(file_path: Path, *, task_key: str | None = None, plan_name: str | None = None) -> LoadedTask:
    resolved_path = file_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Task file not found: {resolved_path}")

    with open(resolved_path, "r", encoding="utf-8") as handle:
        task_file_data = yaml.safe_load(handle) or {}
    if not isinstance(task_file_data, dict):
        raise ValueError(f"Task file must be a YAML object: {resolved_path}")

    with suppress_framework_console_logs():
        validator = TaskDefinitionValidator(
            plan_name=plan_name or "adhoc",
            enable_schema_validation=True,
            strict_validation=False,
        )
        validator.validate_file(task_file_data, resolved_path)

    selected_key, task_data = select_task_definition(task_file_data, resolved_path, task_key=task_key)
    display_name = str(resolved_path)

    inferred_ref = None
    if plan_name:
        inferred_ref = infer_plan_task_ref(REPO_ROOT / "plans" / plan_name, resolved_path, selected_key)

    return LoadedTask(
        plan_name=plan_name,
        display_name=display_name,
        source_path=resolved_path,
        task_key=selected_key,
        task_ref=inferred_ref,
        task_data=task_data,
    )


def select_task_definition(
    task_file_data: dict[str, Any],
    file_path: Path,
    *,
    task_key: str | None,
) -> tuple[str, dict[str, Any]]:
    if isinstance(task_file_data.get("steps"), dict):
        return file_path.stem, task_file_data

    candidates = {
        key: value
        for key, value in task_file_data.items()
        if isinstance(value, dict) and isinstance(value.get("steps"), dict)
    }
    if not candidates:
        raise ValueError(f"No task definition with steps found in: {file_path}")

    if task_key:
        selected = candidates.get(task_key)
        if selected is None:
            raise ValueError(f"Task key '{task_key}' not found in: {file_path}")
        return task_key, selected

    if len(candidates) == 1:
        selected_key = next(iter(candidates))
        return selected_key, candidates[selected_key]

    if file_path.stem in candidates:
        return file_path.stem, candidates[file_path.stem]

    raise ValueError(
        f"Task file '{file_path}' defines multiple tasks. Please specify --task-key."
    )


def analyze_loaded_task(loaded: LoadedTask) -> dict[str, Any]:
    task_data = loaded.task_data
    steps = task_data.get("steps") or {}
    if not isinstance(steps, dict):
        raise ValueError("Task steps must be a mapping")

    graph_builder = GraphBuilder(_DummyEngine())
    graph_valid = True
    graph_error: str | None = None
    try:
        graph_builder.build_graph(steps)
    except Exception as exc:
        graph_valid = False
        graph_error = str(exc)

    edges: list[dict[str, str]] = []
    reverse_dependencies: dict[str, set[str]] = {step_id: set() for step_id in steps}
    step_summaries: list[dict[str, Any]] = []

    for step_id, step_data in steps.items():
        if not isinstance(step_data, dict):
            step_summaries.append(
                {
                    "id": step_id,
                    "action": None,
                    "dependency_error": f"Step definition must be an object, got {type(step_data).__name__}",
                    "resolved_dependencies": [],
                }
            )
            continue

        dependency_error = None
        try:
            resolved_dependencies = sorted(
                graph_builder.get_all_deps_from_struct(step_data.get("depends_on"))
            )
        except Exception as exc:
            resolved_dependencies = []
            dependency_error = str(exc)

        for dependency in resolved_dependencies:
            reverse_dependencies.setdefault(dependency, set()).add(step_id)
            edges.append({"from": dependency, "to": step_id})

        params = step_data.get("params")
        subtask_ref = None
        if step_data.get("action") == "aura.run_task" and isinstance(params, dict):
            task_ref_value = params.get("task_ref")
            if isinstance(task_ref_value, str) and task_ref_value.strip():
                subtask_ref = task_ref_value.strip()

        step_summaries.append(
            {
                "id": step_id,
                "action": step_data.get("action"),
                "depends_on": step_data.get("depends_on"),
                "resolved_dependencies": resolved_dependencies,
                "dependency_error": dependency_error,
                "when": step_data.get("when"),
                "loop": step_data.get("loop"),
                "step_note": step_data.get("step_note"),
                "subtask_ref": subtask_ref,
            }
        )

    roots = sorted(step["id"] for step in step_summaries if not step["resolved_dependencies"])
    leaves = sorted(step_id for step_id, children in reverse_dependencies.items() if not children)
    mermaid = render_mermaid(step_summaries, edges)
    meta = task_data.get("meta") if isinstance(task_data.get("meta"), dict) else {}

    return {
        "task": {
            "plan_name": loaded.plan_name,
            "display_name": loaded.display_name,
            "source_path": str(loaded.source_path),
            "task_key": loaded.task_key,
            "task_ref": loaded.task_ref,
            "title": meta.get("title"),
            "description": meta.get("description"),
        },
        "summary": {
            "step_count": len(step_summaries),
            "edge_count": len(edges),
            "root_steps": roots,
            "leaf_steps": leaves,
            "subtask_calls": [step["subtask_ref"] for step in step_summaries if step["subtask_ref"]],
            "graph_valid": graph_valid,
            "graph_error": graph_error,
        },
        "steps": step_summaries,
        "edges": edges,
        "mermaid": mermaid,
    }


def render_mermaid(steps: Sequence[dict[str, Any]], edges: Sequence[dict[str, str]]) -> str:
    alias_map = {
        step["id"]: f"step_{index + 1}_{sanitize_mermaid_id(step['id'])}"
        for index, step in enumerate(steps)
    }
    lines = ["flowchart TD"]
    for step in steps:
        action = step.get("action") or "unknown"
        label = escape_mermaid_label(f"{step['id']}\\n{action}")
        lines.append(f"    {alias_map[step['id']]}[\"{label}\"]")
    for edge in edges:
        if edge["from"] in alias_map and edge["to"] in alias_map:
            lines.append(f"    {alias_map[edge['from']]} --> {alias_map[edge['to']]}")
    return "\n".join(lines)


def sanitize_mermaid_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)


def escape_mermaid_label(value: str) -> str:
    return value.replace("\"", "'")


def format_text_report(report: dict[str, Any]) -> str:
    task = report["task"]
    summary = report["summary"]

    lines = [
        f"Task: {task['display_name']}",
        f"Source: {task['source_path']}",
        f"Task key: {task['task_key']}",
        f"Task ref: {task['task_ref'] or '-'}",
        f"Title: {task['title'] or '-'}",
        f"Description: {task['description'] or '-'}",
        (
            f"Graph: valid ({summary['step_count']} steps, {summary['edge_count']} edges)"
            if summary["graph_valid"]
            else f"Graph: invalid ({summary['graph_error']})"
        ),
        f"Roots: {', '.join(summary['root_steps']) if summary['root_steps'] else '-'}",
        f"Leaves: {', '.join(summary['leaf_steps']) if summary['leaf_steps'] else '-'}",
    ]

    if summary["subtask_calls"]:
        lines.append(f"Subtasks: {', '.join(summary['subtask_calls'])}")

    lines.append("")
    lines.append("Steps:")
    for step in report["steps"]:
        lines.append(f"- {step['id']}: action={step.get('action') or '-'}")
        lines.append(
            f"  deps={', '.join(step['resolved_dependencies']) if step['resolved_dependencies'] else '-'}"
        )
        if step.get("dependency_error"):
            lines.append(f"  dep_error={step['dependency_error']}")
        if step.get("when"):
            lines.append(f"  when={step['when']}")
        if step.get("loop") is not None:
            lines.append(f"  loop={json.dumps(step['loop'], ensure_ascii=False, sort_keys=True)}")
        if step.get("step_note"):
            lines.append(f"  step_note={step['step_note']}")
        if step.get("subtask_ref"):
            lines.append(f"  subtask_ref={step['subtask_ref']}")

    return "\n".join(lines)


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.task_ref or args.task:
            if not args.plan:
                parser.error("--plan is required with --task-ref or --task")
            loaded = load_task_from_plan(args.plan, task_ref=args.task_ref, task=args.task)
        else:
            loaded = load_task_from_path(args.path, task_key=args.task_key, plan_name=args.plan)
        report = analyze_loaded_task(loaded)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.format == "mermaid":
        print(report["mermaid"])
    else:
        print(format_text_report(report))
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
