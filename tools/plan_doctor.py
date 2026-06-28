from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared import normalize_payload, suppress_framework_console_logs


INPUT_ACTION_REFS = {
    "input.available_profiles",
    "input.list_actions",
    "input.resolve_action",
    "input.press_action",
    "input.tap_action",
    "input.hold_action",
    "input.release_action",
    "plans/aura_base/input.available_profiles",
    "plans/aura_base/input.list_actions",
    "plans/aura_base/input.resolve_action",
    "plans/aura_base/input.press_action",
    "plans/aura_base/input.tap_action",
    "plans/aura_base/input.hold_action",
    "plans/aura_base/input.release_action",
}


def parse_yaml_file(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def normalize_manifest_for_check(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    normalized = dict(data)
    metadata = dict(normalized.get("metadata", {}) or {})
    metadata.pop("generated_at", None)
    normalized["metadata"] = metadata
    return normalized


def iter_task_definitions(task_file_data: Any, file_path: Path) -> Iterable[tuple[str, dict[str, Any]]]:
    if not isinstance(task_file_data, dict):
        return
    if isinstance(task_file_data.get("steps"), dict):
        yield file_path.stem, task_file_data
    for task_name, task_def in task_file_data.items():
        if isinstance(task_def, dict) and isinstance(task_def.get("steps"), dict):
            yield str(task_name), task_def


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    path: str
    hint: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class ComplianceChecker:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._load_repo_symbols()
        self.manifest_index = self._build_manifest_index()
        self.action_export_index = self._build_action_export_index()

    def _load_repo_symbols(self) -> None:
        from packages.aura_core.packaging.core.task_loader import TaskLoader
        from packages.aura_core.packaging.core.task_validator import TaskDefinitionValidator, TaskValidationError
        from packages.aura_core.packaging.manifest.generator import ManifestGenerator
        from packages.aura_core.packaging.manifest.parser import ManifestParser
        from packages.aura_core.packaging.manifest.scanner import ExportScanner, ManifestScanError
        from packages.aura_core.types.task_ref_resolver import TaskRefResolver
        from plans.aura_base.src.platform.runtime_config import supported_capture_backends, supported_input_backends

        self.TaskLoader = TaskLoader
        self.TaskDefinitionValidator = TaskDefinitionValidator
        self.TaskValidationError = TaskValidationError
        self.ManifestGenerator = ManifestGenerator
        self.ManifestParser = ManifestParser
        self.ExportScanner = ExportScanner
        self.ManifestScanError = ManifestScanError
        self.TaskRefResolver = TaskRefResolver
        self.supported_capture_backends = supported_capture_backends
        self.supported_input_backends = supported_input_backends

    def _build_manifest_index(self) -> dict[str, Any]:
        manifests: dict[str, Any] = {}
        for base_dir in (self.repo_root / "packages", self.repo_root / "plans", self.repo_root / "games"):
            if not base_dir.is_dir():
                continue
            for manifest_path in list(base_dir.rglob("manifest.yaml")) + list(base_dir.rglob("game.yaml")):
                try:
                    manifest = self.ManifestParser.parse(manifest_path)
                except Exception:
                    continue
                manifests[manifest.package.canonical_id] = manifest
        return manifests

    def _build_action_export_index(self) -> dict[str, set[str]]:
        index: dict[str, set[str]] = {}
        for package_id, manifest in self.manifest_index.items():
            for action in manifest.exports.actions:
                index.setdefault(action.name, set()).add(package_id)
        return index

    def check(self, plan_names: list[str]) -> dict[str, Any]:
        targets: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        checked_files = 0
        for plan_name in plan_names:
            target = self.check_plan(plan_name)
            targets.append(
                {
                    "plan": target["plan_name"],
                    "plan_path": target["plan_path"],
                    "summary": target["summary"],
                }
            )
            findings.extend(target["findings"])
            checked_files += int(target["summary"]["checked_files"])
        summary = {
            "errors": sum(1 for item in findings if item["severity"] == "error"),
            "warnings": sum(1 for item in findings if item["severity"] == "warn"),
            "infos": sum(1 for item in findings if item["severity"] == "info"),
            "checked_files": checked_files,
        }
        return normalize_payload({"summary": summary, "targets": targets, "findings": findings})

    def check_plan(self, plan_name: str) -> dict[str, Any]:
        plan_dir = self.repo_root / "plans" / plan_name
        findings: list[Finding] = []
        checked_files: set[str] = set()

        if not plan_dir.is_dir():
            findings.append(self._finding("error", "plan_not_found", f"Plan directory does not exist: {plan_name}", plan_dir, "Create the package under plans/<game> first."))
            return normalize_payload(
                {
                    "plan_name": plan_name,
                    "plan_path": str(plan_dir),
                    "summary": {"errors": 1, "warnings": 0, "infos": 0, "checked_files": 0},
                    "findings": [item.to_dict() for item in findings],
                }
            )

        manifest_path = plan_dir / "manifest.yaml"
        config_path = plan_dir / "config.yaml"
        states_map_path = plan_dir / "states_map.yaml"
        tasks_dir = plan_dir / "tasks"
        actions_dir = plan_dir / "src" / "actions"
        services_dir = plan_dir / "src" / "services"
        actions_init = actions_dir / "__init__.py"
        services_init = services_dir / "__init__.py"
        input_profiles_dir = plan_dir / "data" / "input_profiles"

        checked_files.update(str(path) for path in plan_dir.rglob("*.py"))
        if manifest_path.is_file():
            checked_files.add(str(manifest_path))
        if config_path.is_file():
            checked_files.add(str(config_path))
        if states_map_path.is_file():
            checked_files.add(str(states_map_path))
        if tasks_dir.is_dir():
            checked_files.update(str(path) for path in tasks_dir.rglob("*.yaml"))
        if input_profiles_dir.is_dir():
            checked_files.update(str(path) for path in input_profiles_dir.rglob("*.yaml"))

        findings.extend(self._check_structure(plan_dir, manifest_path, tasks_dir, actions_init, services_init))

        manifest = None
        raw_manifest_data: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                raw_manifest_data = parse_yaml_file(manifest_path)
            except Exception as exc:
                findings.append(self._finding("error", "manifest_parse_failed", f"Failed to parse manifest.yaml: {exc}", manifest_path, self._hint("manifest_parse_failed")))
            try:
                manifest = self.ManifestParser.parse(manifest_path)
            except Exception as exc:
                findings.append(self._finding("error", "manifest_invalid", f"Manifest parse failed: {exc}", manifest_path, self._hint("manifest_invalid")))
            if manifest is not None:
                for error in self.ManifestParser.validate(manifest):
                    findings.append(self._finding("error", "manifest_invalid", error, manifest_path, self._hint("manifest_invalid")))
                findings.extend(self._check_manifest_sync(plan_dir, manifest_path, raw_manifest_data))
                findings.extend(self._check_manifest_exports(plan_dir, manifest_path, manifest, raw_manifest_data))

        with suppress_framework_console_logs():
            task_loader = self.TaskLoader(plan_name, plan_dir, manifest=manifest)
            validator = self.TaskDefinitionValidator(
                plan_name=plan_name,
                enable_schema_validation=True,
                strict_validation=True,
            )

        required_states: set[str] = set()
        task_actions: list[tuple[str, str, Path, dict[str, Any]]] = []
        literal_input_action_names: set[str] = set()

        if tasks_dir.is_dir():
            for task_file in sorted(tasks_dir.rglob("*.yaml")):
                try:
                    task_file_data = parse_yaml_file(task_file)
                except Exception as exc:
                    findings.append(self._finding("error", "task_yaml_parse_failed", f"Failed to parse task YAML: {exc}", task_file, self._hint("task_yaml_parse_failed")))
                    continue

                try:
                    validator.validate_file(task_file_data, task_file)
                except self.TaskValidationError as exc:
                    findings.append(self._finding("error", exc.code, str(exc), task_file, self._hint(exc.code)))

                for task_name, task_def in iter_task_definitions(task_file_data, task_file):
                    if not isinstance(task_def, dict):
                        continue
                    required_state = (task_def.get("meta", {}) or {}).get("requires_initial_state")
                    if isinstance(required_state, str) and required_state.strip():
                        required_states.add(required_state.strip())

                    steps = task_def.get("steps", {})
                    if not isinstance(steps, dict):
                        continue
                    for step_id, step_def in steps.items():
                        if not isinstance(step_def, dict):
                            continue
                        location = f"{task_file.name}:{task_name}.steps.{step_id}"
                        loop_cfg = step_def.get("loop")
                        if loop_cfg is not None:
                            findings.extend(self._check_loop_config(task_file, location, loop_cfg))
                        action_name = step_def.get("action")
                        if isinstance(action_name, str) and action_name.strip():
                            resolved_action_name = action_name.strip()
                            task_actions.append((location, resolved_action_name, task_file, step_def))
                            if resolved_action_name == "run_task":
                                findings.append(self._finding("error", "deprecated_syntax", f"{location} uses removed action alias 'run_task'. Use 'aura.run_task'.", task_file, self._hint("deprecated_syntax")))
                            if resolved_action_name in {"aura.run_task", "plans/aura_base/aura.run_task"}:
                                params = step_def.get("params", {}) or {}
                                if "task_name" in params:
                                    findings.append(self._finding("error", "deprecated_syntax", f"{location} uses removed parameter 'task_name'. Use 'task_ref' with canonical .yaml syntax.", task_file, self._hint("deprecated_syntax")))
                                task_ref = params.get("task_ref")
                                if isinstance(task_ref, str) and task_ref.strip():
                                    findings.extend(self._check_task_ref_exists(plan_name, task_loader, task_ref.strip(), task_file, "task_validation_failed"))
                            if resolved_action_name in INPUT_ACTION_REFS:
                                params = step_def.get("params", {}) or {}
                                action_param = params.get("action_name")
                                if isinstance(action_param, str) and "{{" not in action_param and "{%" not in action_param:
                                    literal_input_action_names.add(action_param.strip())

        with suppress_framework_console_logs():
            for error in task_loader.get_task_load_errors():
                findings.append(
                    self._finding(
                        "error",
                        f"task_{error.get('error_code')}",
                        error.get("message") or "Task load error.",
                        tasks_dir / str(error.get("source_file") or ""),
                        self._hint(error.get("error_code") or "task_validation_failed"),
                    )
                )

        findings.extend(self._check_action_references(task_actions, manifest))
        findings.extend(self._check_state_planning(plan_name, task_loader, required_states, states_map_path))
        findings.extend(
            self._check_runtime_and_input(
                config_path=config_path,
                input_profiles_dir=input_profiles_dir,
                task_actions=task_actions,
                literal_input_action_names=literal_input_action_names,
            )
        )

        summary = {
            "errors": sum(1 for item in findings if item.severity == "error"),
            "warnings": sum(1 for item in findings if item.severity == "warn"),
            "infos": sum(1 for item in findings if item.severity == "info"),
            "checked_files": len(checked_files),
        }
        return normalize_payload(
            {
                "plan_name": plan_name,
                "plan_path": str(plan_dir),
                "summary": summary,
                "findings": [item.to_dict() for item in findings],
            }
        )

    def _check_structure(
        self,
        plan_dir: Path,
        manifest_path: Path,
        tasks_dir: Path,
        actions_init: Path,
        services_init: Path,
    ) -> list[Finding]:
        findings: list[Finding] = []
        if not manifest_path.is_file():
            findings.append(self._finding("error", "manifest_missing", "Plan package is missing manifest.yaml.", manifest_path, self._hint("manifest_missing")))
        if not tasks_dir.is_dir():
            findings.append(self._finding("error", "tasks_dir_missing", "Plan package is missing tasks/.", tasks_dir, self._hint("tasks_dir_missing")))
        if not actions_init.is_file():
            findings.append(self._finding("error", "actions_init_missing", "Plan package is missing src/actions/__init__.py.", actions_init, self._hint("actions_init_missing")))
        if not services_init.is_file():
            findings.append(self._finding("error", "services_init_missing", "Plan package is missing src/services/__init__.py.", services_init, self._hint("services_init_missing")))
        for pycache_dir in sorted(plan_dir.rglob("__pycache__")):
            findings.append(self._finding("warn", "pycache_present", "Plan package contains __pycache__ directory.", pycache_dir, self._hint("pycache_present")))
        for pyc_file in sorted(plan_dir.rglob("*.pyc")):
            findings.append(self._finding("warn", "pyc_present", "Plan package contains compiled Python bytecode.", pyc_file, self._hint("pycache_present")))
        return findings

    def _check_manifest_sync(self, plan_dir: Path, manifest_path: Path, raw_manifest_data: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        try:
            expected = self.ManifestGenerator(plan_dir).generate(preserve_manual_edits=True)
            current = raw_manifest_data or parse_yaml_file(manifest_path)
            if normalize_manifest_for_check(current) != normalize_manifest_for_check(expected):
                findings.append(self._finding("error", "manifest_out_of_sync", "manifest.yaml is out of sync with scanned package exports.", manifest_path, self._hint("manifest_out_of_sync")))
        except Exception as exc:
            findings.append(self._finding("error", "manifest_scan_failed", f"Failed to compare manifest against scanned exports: {exc}", manifest_path, self._hint("manifest_out_of_sync")))
        return findings

    def _check_manifest_exports(self, plan_dir: Path, manifest_path: Path, manifest: Any, raw_manifest_data: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        try:
            scanner = self.ExportScanner(plan_dir, raw_manifest_data or {"package": {"name": f"@plans/{plan_dir.name}"}})
            scanned_actions = scanner.scan_actions()
            scanned_services = scanner.scan_services()
        except self.ManifestScanError as exc:
            findings.append(self._finding("error", "manifest_scan_failed", str(exc), plan_dir / "src", self._hint("manifest_out_of_sync")))
            return findings

        exported_actions = {action.name for action in manifest.exports.actions}
        exported_services = {service.name for service in manifest.exports.services}
        for action_name in sorted({item["name"] for item in scanned_actions} - exported_actions):
            findings.append(self._finding("error", "action_export_missing", f"Action '{action_name}' is defined in source but missing from manifest exports.", manifest_path, self._hint("action_export_missing")))
        for service_name in sorted({item["name"] for item in scanned_services} - exported_services):
            findings.append(self._finding("error", "service_export_missing", f"Service '{service_name}' is defined in source but missing from manifest exports.", manifest_path, self._hint("service_export_missing")))
        for action in manifest.exports.actions:
            findings.extend(self._check_export_symbol(action.module, action.function_name, manifest_path, "action", action.name))
        for service in manifest.exports.services:
            findings.extend(self._check_export_symbol(service.module, service.class_name, manifest_path, "service", service.name))
        return findings

    def _check_export_symbol(self, module_name: str, symbol_name: str, manifest_path: Path, export_kind: str, export_name: str) -> list[Finding]:
        findings: list[Finding] = []
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            findings.append(self._finding("error", "export_module_import_failed", f"Failed to import {export_kind} module '{module_name}' for '{export_name}': {exc}", manifest_path, self._hint("export_module_import_failed")))
            return findings
        if not hasattr(module, symbol_name):
            findings.append(self._finding("error", "export_symbol_missing", f"Manifest exports {export_kind} '{export_name}' from '{module_name}.{symbol_name}', but the symbol does not exist.", manifest_path, self._hint("export_symbol_missing")))
        return findings

    def _check_loop_config(self, task_file: Path, location: str, loop_cfg: Any) -> list[Finding]:
        findings: list[Finding] = []
        if not isinstance(loop_cfg, dict):
            findings.append(self._finding("error", "loop_invalid", "loop must be an object with one supported mode.", task_file, self._hint("loop_invalid")))
            return findings
        supported_modes = {"for_each", "times", "while"}
        auxiliary_keys = {"parallelism", "max_iterations"}
        mode_keys = supported_modes.intersection(loop_cfg.keys())
        if len(mode_keys) != 1:
            findings.append(self._finding("error", "loop_invalid", f"{location} must define exactly one loop mode from {sorted(supported_modes)}.", task_file, self._hint("loop_invalid")))
        unexpected_keys = sorted(set(loop_cfg.keys()) - supported_modes - auxiliary_keys)
        if unexpected_keys:
            findings.append(self._finding("warn", "loop_unexpected_keys", f"{location} loop contains unexpected keys: {unexpected_keys}.", task_file, self._hint("loop_invalid")))
        return findings

    def _check_action_references(self, task_actions: list[tuple[str, str, Path, dict[str, Any]]], manifest: Any) -> list[Finding]:
        findings: list[Finding] = []
        current_package_id = manifest.package.canonical_id if manifest is not None else None
        dependency_ids = {name.lstrip("@") for name in manifest.dependencies.keys()} if manifest is not None else set()
        current_actions = {action.name for action in manifest.exports.actions} if manifest is not None else set()

        for location, action_name, task_file, _step_def in task_actions:
            if action_name == "run_task":
                continue
            if "/" not in action_name:
                if action_name in current_actions:
                    continue
                exported_by = self.action_export_index.get(action_name, set())
                if not exported_by:
                    findings.append(self._finding("error", "local_action_missing", f"{location} references action '{action_name}', but it is not exported by any discovered package.", task_file, self._hint("local_action_missing")))
                    continue
                non_core_matches = sorted(package_id for package_id in exported_by if package_id not in {current_package_id, "plans/aura_base"})
                if non_core_matches and not any(package_id in dependency_ids for package_id in non_core_matches):
                    findings.append(self._finding("warn", "external_dependency_missing", f"{location} references action '{action_name}' exported by {non_core_matches}, but those packages are not declared as manifest dependencies.", task_file, self._hint("external_dependency_missing")))
                continue

            parts = action_name.split("/")
            if len(parts) != 3:
                findings.append(self._finding("error", "action_ref_invalid", f"{location} uses invalid external action ref '{action_name}'. Expected 'author/package/action'.", task_file, self._hint("action_ref_invalid")))
                continue

            package_id = f"{parts[0]}/{parts[1]}"
            action_leaf = parts[2]
            if current_package_id and package_id != current_package_id and package_id not in dependency_ids:
                findings.append(self._finding("error", "external_dependency_missing", f"{location} references external package '{package_id}' without declaring it in manifest dependencies.", task_file, self._hint("external_dependency_missing")))
                continue
            target_manifest = self.manifest_index.get(package_id)
            if target_manifest is None:
                findings.append(self._finding("error", "external_package_missing", f"{location} references external action '{action_name}', but package '{package_id}' was not found.", task_file, self._hint("external_package_missing")))
                continue
            exported_actions = {action.name for action in target_manifest.exports.actions}
            if action_leaf not in exported_actions:
                findings.append(self._finding("error", "external_action_missing", f"{location} references action '{action_name}', but '{action_leaf}' is not exported by package '{package_id}'.", task_file, self._hint("external_action_missing")))
        return findings

    def _check_state_planning(self, plan_name: str, task_loader: Any, required_states: set[str], states_map_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        if required_states and not states_map_path.is_file():
            findings.append(self._finding("error", "states_map_missing", "At least one task requires an initial state, but states_map.yaml is missing.", states_map_path, self._hint("states_map_missing")))
            return findings
        if not states_map_path.is_file():
            return findings

        try:
            state_map_data = parse_yaml_file(states_map_path)
        except Exception as exc:
            findings.append(self._finding("error", "states_map_invalid", f"Failed to parse states_map.yaml: {exc}", states_map_path, self._hint("states_map_invalid")))
            return findings

        states = state_map_data.get("states")
        transitions = state_map_data.get("transitions")
        if not isinstance(states, dict) or not isinstance(transitions, list):
            findings.append(self._finding("error", "states_map_invalid", "states_map.yaml must contain 'states' (object) and 'transitions' (list).", states_map_path, self._hint("states_map_invalid")))
            return findings

        state_names = set(states.keys())
        indegree = {name: 0 for name in state_names}
        outdegree = {name: 0 for name in state_names}

        for required_state in sorted(required_states):
            if required_state not in state_names:
                findings.append(self._finding("error", "required_state_missing", f"Task requires initial state '{required_state}', but that state is not declared in states_map.yaml.", states_map_path, self._hint("required_state_missing")))

        for state_name, state_def in states.items():
            if not isinstance(state_def, dict):
                findings.append(self._finding("error", "states_map_invalid", f"State '{state_name}' must map to an object.", states_map_path, self._hint("states_map_invalid")))
                continue
            check_task = state_def.get("check_task")
            if not isinstance(check_task, str) or not check_task.strip():
                findings.append(self._finding("error", "state_check_task_missing", f"State '{state_name}' is missing check_task.", states_map_path, self._hint("state_check_task_missing")))
                continue
            findings.extend(self._check_task_ref_exists(plan_name, task_loader, check_task, states_map_path, "state_check_task_missing"))

        for index, transition in enumerate(transitions):
            if not isinstance(transition, dict):
                findings.append(self._finding("error", "states_map_invalid", f"Transition #{index} must be an object.", states_map_path, self._hint("states_map_invalid")))
                continue
            from_state = transition.get("from")
            to_state = transition.get("to")
            task_ref = transition.get("transition_task")
            if from_state not in state_names:
                findings.append(self._finding("error", "transition_state_missing", f"Transition #{index} references unknown source state '{from_state}'.", states_map_path, self._hint("states_map_invalid")))
            if to_state not in state_names:
                findings.append(self._finding("error", "transition_state_missing", f"Transition #{index} references unknown target state '{to_state}'.", states_map_path, self._hint("states_map_invalid")))
            if isinstance(from_state, str) and from_state in outdegree:
                outdegree[from_state] += 1
            if isinstance(to_state, str) and to_state in indegree:
                indegree[to_state] += 1
            if from_state == to_state and from_state in state_names:
                findings.append(self._finding("warn", "state_self_loop", f"State '{from_state}' has a self-loop transition.", states_map_path, self._hint("state_self_loop")))
            if not isinstance(task_ref, str) or not task_ref.strip():
                findings.append(self._finding("error", "transition_task_missing", f"Transition #{index} is missing transition_task.", states_map_path, self._hint("transition_task_missing")))
            else:
                findings.extend(self._check_task_ref_exists(plan_name, task_loader, task_ref, states_map_path, "transition_task_missing"))

        for state_name in sorted(state_names):
            if indegree[state_name] == 0:
                findings.append(self._finding("info", "state_no_incoming_edges", f"State '{state_name}' has no incoming transitions.", states_map_path, self._hint("state_no_edges")))
            if outdegree[state_name] == 0:
                findings.append(self._finding("info", "state_no_outgoing_edges", f"State '{state_name}' has no outgoing transitions.", states_map_path, self._hint("state_no_edges")))
        if len(state_names) > 1:
            for required_state in sorted(required_states):
                if required_state in indegree and indegree[required_state] == 0:
                    findings.append(self._finding("warn", "required_state_unreachable", f"Required state '{required_state}' has no incoming transitions, so state planning cannot reach it from a different current state.", states_map_path, self._hint("required_state_unreachable")))
        return findings

    def _check_task_ref_exists(self, plan_name: str, task_loader: Any, task_ref: str, path: Path, code: str) -> list[Finding]:
        findings: list[Finding] = []
        try:
            resolved = self.TaskRefResolver.resolve(task_ref.strip(), default_package=plan_name, enforce_package=plan_name)
        except Exception as exc:
            findings.append(self._finding("error", code, f"Invalid task_ref '{task_ref}': {exc}", path, self._hint(code)))
            return findings
        task_data = task_loader.get_task_data(resolved.loader_path)
        if task_data is None:
            findings.append(self._finding("error", code, f"Task reference '{task_ref}' does not resolve to an existing task in plan '{plan_name}'.", path, self._hint(code)))
        return findings

    def _check_runtime_and_input(
        self,
        *,
        config_path: Path,
        input_profiles_dir: Path,
        task_actions: list[tuple[str, str, Path, dict[str, Any]]],
        literal_input_action_names: set[str],
    ) -> list[Finding]:
        findings: list[Finding] = []
        config_data: dict[str, Any] = {}
        if config_path.is_file():
            try:
                loaded = parse_yaml_file(config_path)
                if isinstance(loaded, dict):
                    config_data = loaded
            except Exception as exc:
                findings.append(self._finding("error", "config_parse_failed", f"Failed to parse config.yaml: {exc}", config_path, self._hint("runtime_backend_invalid")))
                return findings

        runtime_cfg = config_data.get("runtime", {}) if isinstance(config_data.get("runtime"), dict) else {}
        top_input_cfg = config_data.get("input", {}) if isinstance(config_data.get("input"), dict) else {}
        runtime_provider = str(runtime_cfg.get("provider") or "").strip().lower()
        runtime_family = str(runtime_cfg.get("family") or "").strip().lower()
        if not runtime_provider and runtime_family == "windows_desktop":
            runtime_provider = "windows"
        elif not runtime_provider and runtime_family == "android_emulator":
            runtime_provider = "mumu"
        elif not runtime_provider and (
            "window_spec" in runtime_cfg or "capture" in runtime_cfg or "input" in runtime_cfg
        ):
            runtime_provider = "windows"

        if runtime_provider:
            supported_capture = set(self.supported_capture_backends(runtime_provider))
            supported_input = set(self.supported_input_backends(runtime_provider))
            capture_cfg = runtime_cfg.get("capture", {}) if isinstance(runtime_cfg.get("capture"), dict) else {}
            input_cfg = runtime_cfg.get("input", {}) if isinstance(runtime_cfg.get("input"), dict) else {}

            backend = str(capture_cfg.get("backend") or "").strip().lower()
            if backend and backend not in supported_capture:
                findings.append(self._finding("error", "runtime_backend_invalid", f"Unsupported {runtime_provider} capture backend '{backend}'.", config_path, self._hint("runtime_backend_invalid")))
            input_backend = str(input_cfg.get("backend") or "").strip().lower()
            if input_backend and input_backend not in supported_input:
                findings.append(self._finding("error", "runtime_backend_invalid", f"Unsupported {runtime_provider} input backend '{input_backend}'.", config_path, self._hint("runtime_backend_invalid")))

            candidates = capture_cfg.get("candidates", [])
            if isinstance(candidates, list):
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_backend = str(candidate.get("backend") or "").strip().lower()
                    if candidate_backend and candidate_backend not in supported_capture:
                        findings.append(self._finding("error", "runtime_backend_invalid", f"Unsupported {runtime_provider} capture candidate backend '{candidate_backend}'.", config_path, self._hint("runtime_backend_invalid")))

            target_cfg = runtime_cfg.get("target", {}) if isinstance(runtime_cfg.get("target"), dict) else {}
            window_spec_cfg = runtime_cfg.get("window_spec", {}) if isinstance(runtime_cfg.get("window_spec"), dict) else {}
            window_spec_mode = str(window_spec_cfg.get("mode") or "off").strip().lower()
            if runtime_provider == "windows" and window_spec_mode != "off":
                selector_values = (
                    target_cfg.get("mode"),
                    target_cfg.get("hwnd"),
                    target_cfg.get("process_name"),
                    target_cfg.get("pid"),
                    target_cfg.get("title"),
                    target_cfg.get("title_regex"),
                    target_cfg.get("class_name"),
                    target_cfg.get("class_regex"),
                    target_cfg.get("exe_path_contains"),
                )
                if not any(value not in (None, "", []) for value in selector_values):
                    findings.append(self._finding("warn", "window_target_selector_missing", "runtime.window_spec is enabled, but runtime.target does not define enough selector fields to bind a window.", config_path, self._hint("window_target_selector_missing")))

        uses_input_mapping = any(action_name in INPUT_ACTION_REFS for _, action_name, _, _ in task_actions) or bool(top_input_cfg)
        if uses_input_mapping:
            if not input_profiles_dir.is_dir():
                findings.append(self._finding("error", "input_profiles_missing", "Input mapping is used, but data/input_profiles/ is missing.", input_profiles_dir, self._hint("input_profiles_missing")))
                return findings

            configured_profile = str(top_input_cfg.get("profile") or "default_pc").strip() or "default_pc"
            configured_profile_path = input_profiles_dir / f"{configured_profile}.yaml"
            default_profile_path = input_profiles_dir / "default_pc.yaml"
            if not configured_profile_path.is_file():
                findings.append(self._finding("warn", "input_profile_missing", f"Configured input profile '{configured_profile}' was not found.", configured_profile_path, self._hint("input_profile_missing")))
            if not default_profile_path.is_file():
                findings.append(self._finding("warn", "input_profile_missing", "default_pc.yaml is missing under data/input_profiles/.", default_profile_path, self._hint("input_profile_missing")))

            defined_actions: dict[str, Any] = {}
            if isinstance(top_input_cfg.get("actions"), dict):
                defined_actions.update(top_input_cfg.get("actions"))
            for profile_path in input_profiles_dir.glob("*.yaml"):
                try:
                    profile_data = parse_yaml_file(profile_path)
                except Exception:
                    continue
                if isinstance(profile_data, dict) and isinstance(profile_data.get("actions"), dict):
                    if profile_path.name == f"{configured_profile}.yaml":
                        defined_actions.update(profile_data["actions"])
            for action_name in sorted(name for name in literal_input_action_names if name):
                if action_name not in defined_actions:
                    findings.append(self._finding("warn", "input_action_not_defined", f"Literal input action '{action_name}' is referenced by task YAML but not defined in the active profile or config input.actions.", input_profiles_dir, self._hint("input_action_not_defined")))
        return findings

    def _finding(self, severity: str, code: str, message: str, path: Path, hint: str) -> Finding:
        return Finding(
            severity=severity,
            code=code,
            message=message,
            path=str(path),
            hint=hint,
            remediation=self._remediation_template(code),
        )

    def _hint(self, code: str) -> str:
        hint_map = {
            "plan_not_found": "Create the package under plans/<game> first.",
            "manifest_missing": "Create the package manifest first. See docs/package-development/actions-and-services.md.",
            "manifest_parse_failed": "Fix manifest YAML syntax first, then rerun the checker.",
            "manifest_invalid": "Validate manifest structure and exported module paths. See docs/package-development/actions-and-services.md.",
            "manifest_out_of_sync": "Sync the package manifest after adding or removing exports.",
            "manifest_scan_failed": "Check src/actions and src/services for unsupported imports or malformed decorators.",
            "action_export_missing": "Export new actions through manifest.yaml. See docs/package-development/actions-and-services.md.",
            "service_export_missing": "Export new services through manifest.yaml. See docs/package-development/actions-and-services.md.",
            "export_module_import_failed": "Check the module path and package layout under src/actions or src/services.",
            "export_symbol_missing": "Make sure the exported function/class name matches the source symbol.",
            "tasks_dir_missing": "Create tasks/ and place task YAML files there.",
            "actions_init_missing": "Add src/actions/__init__.py to make the action package explicit.",
            "services_init_missing": "Add src/services/__init__.py to make the service package explicit.",
            "pycache_present": "Remove transient Python cache artifacts before finalizing the package.",
            "task_yaml_parse_failed": "Fix YAML syntax first, then rerun the checker.",
            "deprecated_syntax": "Use current task syntax. See docs/package-development/task-references-and-dependencies.md.",
            "schema_validation_failed": "Review task schema and dependency syntax in docs/package-development/task-references-and-dependencies.md.",
            "task_validation_failed": "Review task schema and canonical task_ref usage.",
            "loop_invalid": "Only for_each, times, and while are supported loop modes.",
            "local_action_missing": "Either export the action locally or use a valid shared action name/FQID.",
            "action_ref_invalid": "Use 'author/package/action' for explicit external actions, or a valid shared action name.",
            "external_dependency_missing": "Declare the external package in manifest dependencies before relying on its private exports.",
            "external_package_missing": "Make sure the target package exists and has a valid manifest.",
            "external_action_missing": "Check the target package manifest exports or choose a valid action name.",
            "states_map_missing": "Add states_map.yaml when tasks declare requires_initial_state. See docs/runtime-operations/state-planning.md.",
            "states_map_invalid": "Fix states_map.yaml shape and task refs. See docs/runtime-operations/state-planning.md.",
            "state_check_task_missing": "Each state should define a resolvable check_task in the same plan.",
            "transition_task_missing": "Each transition should define a resolvable transition_task in the same plan.",
            "required_state_missing": "Keep meta.requires_initial_state aligned with states_map.yaml state names.",
            "state_self_loop": "Review whether the self-loop is intentional and safe for replanning.",
            "state_no_edges": "Review graph topology and document any intentionally isolated states.",
            "required_state_unreachable": "Add incoming transitions so state planning can reach the target from other states.",
            "runtime_backend_invalid": "Use backend names from plans/aura_base/src/platform/runtime_config.py.",
            "window_target_selector_missing": "When using window-spec enforcement, define enough runtime.target selector fields to bind the window.",
            "input_profiles_missing": "Create data/input_profiles/ and add the active profile file.",
            "input_profile_missing": "Keep input.profile aligned with an existing YAML under data/input_profiles/.",
            "input_action_not_defined": "Define the action in the active profile or top-level input.actions.",
        }
        return hint_map.get(code, "Review the relevant repo docs and representative examples before landing the change.")

    def _remediation_template(self, code: str) -> str:
        templates = {
            "manifest_missing": (
                "package:\n"
                "  name: '@plans/<plan_name>'\n"
                "  version: '0.1.0'\n"
                "  description: ''\n"
                "  license: MIT\n"
                "dependencies: {}\n"
                "exports:\n"
                "  actions: []\n"
                "  services: []\n"
                "  tasks: []\n"
            ),
            "tasks_dir_missing": "Create directory: plans/<plan_name>/tasks/\nAdd task YAML files under that directory.",
            "actions_init_missing": "Create file: plans/<plan_name>/src/actions/__init__.py\nContent can be empty.",
            "services_init_missing": "Create file: plans/<plan_name>/src/services/__init__.py\nContent can be empty.",
            "action_export_missing": (
                "exports:\n"
                "  actions:\n"
                "    - name: <action_name>\n"
                "      module: plans.<plan_name>.src.actions.<module_name>\n"
                "      function: <function_name>\n"
                "      public: true\n"
                "      read_only: false\n"
            ),
            "service_export_missing": (
                "exports:\n"
                "  services:\n"
                "    - name: <service_alias>\n"
                "      module: plans.<plan_name>.src.services.<module_name>\n"
                "      class: <ClassName>\n"
                "      public: true\n"
                "      singleton: true\n"
            ),
            "manifest_out_of_sync": (
                "Rescan exports and sync manifest.\n"
                "At minimum, compare src/actions + src/services against manifest.yaml and add/remove stale export entries."
            ),
            "deprecated_syntax": (
                "steps:\n"
                "  call_subtask:\n"
                "    action: aura.run_task\n"
                "    params:\n"
                "      task_ref: tasks:subdir:task.yaml[:task_key]\n"
                "  gated_step:\n"
                "    when: \"{{ condition }}\"\n"
                "    step_note: Human readable note\n"
            ),
            "task_validation_failed": (
                "Use canonical task_ref values only:\n"
                "  tasks:subdir:file.yaml\n"
                "  tasks:subdir:file.yaml:task_key\n"
            ),
            "loop_invalid": (
                "loop:\n"
                "  for_each: \"{{ items }}\"\n"
                "# or\n"
                "loop:\n"
                "  times: 3\n"
                "# or\n"
                "loop:\n"
                "  while: \"{{ condition }}\"\n"
            ),
            "action_ref_invalid": "External action refs should look like: plans/aura_base/input.tap_action",
            "states_map_missing": (
                "states:\n"
                "  world:\n"
                "    check_task: \"tasks:checks:world.yaml\"\n"
                "transitions:\n"
                "  - from: loading\n"
                "    to: world\n"
                "    cost: 1\n"
                "    transition_task: \"tasks:transitions:to_world.yaml\"\n"
            ),
            "states_map_invalid": (
                "states:\n"
                "  idle:\n"
                "    check_task: \"tasks:checks:idle.yaml\"\n"
                "transitions:\n"
                "  - from: idle\n"
                "    to: ready\n"
                "    transition_task: \"tasks:transitions:to_ready.yaml\"\n"
            ),
            "state_check_task_missing": "For each state add: check_task: \"tasks:checks:<state>.yaml\"",
            "transition_task_missing": "For each transition add: transition_task: \"tasks:transitions:<name>.yaml\"",
            "required_state_missing": "Keep task meta.requires_initial_state aligned with states_map.yaml state names.",
            "runtime_backend_invalid": (
                "Windows capture backends: wgc, dxgi, gdi, printwindow\n"
                "Windows input backends: sendinput, window_message"
            ),
            "window_target_selector_missing": (
                "runtime:\n"
                "  target:\n"
                "    mode: title\n"
                "    title: \"Game Window\"\n"
            ),
            "input_profiles_missing": (
                "Create directory: plans/<plan_name>/data/input_profiles/\n"
                "Add file: default_pc.yaml\n"
                "actions:\n"
                "  confirm:\n"
                "    type: key\n"
                "    key: enter\n"
            ),
            "input_profile_missing": (
                "input:\n"
                "  profile: default_pc\n"
                "# and ensure plans/<plan_name>/data/input_profiles/default_pc.yaml exists"
            ),
            "input_action_not_defined": (
                "actions:\n"
                "  <action_name>:\n"
                "    type: key\n"
                "    key: <key_name>\n"
            ),
        }
        return templates.get(code, "Fix the reported path/config and rerun plan_doctor to confirm the finding is cleared.")


def inspect_plan(plan_name: str) -> dict[str, Any]:
    checker = ComplianceChecker(REPO_ROOT)
    with suppress_framework_console_logs():
        return checker.check_plan(plan_name)


def inspect_many(plan_names: list[str]) -> dict[str, Any]:
    checker = ComplianceChecker(REPO_ROOT)
    with suppress_framework_console_logs():
        return checker.check(plan_names)


def discover_plan_names() -> list[str]:
    plans_dir = REPO_ROOT / "plans"
    if not plans_dir.is_dir():
        return []
    names = []
    for child in sorted(plans_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("__"):
            continue
        if child.name == "aura_base":
            continue
        names.append(child.name)
    return names


def render_text(report: dict[str, Any]) -> str:
    if "targets" in report:
        summary = report.get("summary", {})
        lines = [
            "Compliance summary: "
            f"errors={summary.get('errors')} warnings={summary.get('warnings')} "
            f"infos={summary.get('infos')} checked_files={summary.get('checked_files')}",
        ]
        for target in report.get("targets", []):
            target_summary = target.get("summary", {})
            lines.append(
                f"- {target.get('plan')}: "
                f"errors={target_summary.get('errors')} warnings={target_summary.get('warnings')} "
                f"infos={target_summary.get('infos')} checked_files={target_summary.get('checked_files')}"
            )
        findings = report.get("findings", [])
    else:
        summary = report.get("summary", {})
        lines = [
            f"Plan: {report.get('plan_name')}",
            f"Path: {report.get('plan_path')}",
            f"Summary: errors={summary.get('errors')} warnings={summary.get('warnings')} infos={summary.get('infos')} checked_files={summary.get('checked_files')}",
        ]
        findings = report.get("findings", [])

    if findings:
        lines.append("")
        lines.append("Findings:")
        for item in findings:
            lines.append(f"- [{item.get('severity')}] {item.get('code')}: {item.get('message')}")
            lines.append(f"  path: {item.get('path') or '-'}")
            lines.append(f"  hint: {item.get('hint') or '-'}")
            lines.append("  remediation:")
            for row in str(item.get("remediation") or "-").splitlines():
                lines.append(f"    {row}")
    else:
        lines.append("")
        lines.append("Findings: none")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a repo-local Aura game plan package.")
    parser.add_argument("--plan", help="Plan name under plans/.")
    parser.add_argument("--all", action="store_true", help="Check all plans under plans/.")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout.")
    parser.add_argument("--json-out", help="Write JSON results to a file.")
    parser.add_argument("--fail-on", choices=("warn", "error"), default="error", help="Failure threshold for exit code.")
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.plan and not args.all:
        parser.error("Either --plan or --all is required.")
    if args.plan and args.all:
        parser.error("Use either --plan or --all, not both.")

    plan_names = [args.plan] if args.plan else discover_plan_names()
    try:
        result = inspect_plan(args.plan) if args.plan else inspect_many(plan_names)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))

    if args.json_out:
        output_path = Path(args.json_out).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.fail_on == "warn":
        warnings = result.get("summary", {}).get("warnings", 0)
        errors = result.get("summary", {}).get("errors", 0)
        return 1 if errors or warnings else 0
    return 1 if result.get("summary", {}).get("errors", 0) else 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
