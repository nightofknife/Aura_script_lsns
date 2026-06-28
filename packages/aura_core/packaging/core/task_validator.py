# -*- coding: utf-8 -*-
"""Task definition validation for runtime task YAML loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from packages.aura_core.config.validator import validate_task_definition
from packages.aura_core.types import TaskReference


class TaskValidationError(ValueError):
    """Typed task validation error with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TaskDefinitionValidator:
    """Validate runtime task definitions outside TaskLoader caching concerns."""

    _REMOVED_STEP_FIELDS = {"goto", "label"}

    def __init__(self, *, plan_name: str, enable_schema_validation: bool, strict_validation: bool):
        self.plan_name = plan_name
        self.enable_schema_validation = bool(enable_schema_validation)
        self.strict_validation = bool(strict_validation)

    def validate_file(self, task_file_data: Dict[str, Any], file_path: Path) -> None:
        self._validate_removed_step_fields(task_file_data, file_path)
        self._validate_schema(task_file_data, file_path)

    def _validate_schema(self, task_file_data: Dict[str, Any], file_path: Path) -> None:
        if not self.enable_schema_validation or not task_file_data:
            return
        is_valid, error = validate_task_definition(task_file_data)
        if is_valid:
            return
        raise TaskValidationError(
            "schema_validation_failed",
            f"Schema validation failed for '{file_path.name}': {error}",
        )

    def _iter_task_definitions(self, task_file_data: Dict[str, Any], file_path: Path) -> Iterable[Tuple[str, Dict[str, Any]]]:
        if not isinstance(task_file_data, dict):
            return

        root_steps = task_file_data.get("steps")
        if isinstance(root_steps, dict):
            yield file_path.stem, task_file_data

        for task_name, task_def in task_file_data.items():
            if isinstance(task_def, dict) and isinstance(task_def.get("steps"), dict):
                yield str(task_name), task_def

    def _validate_removed_step_fields(self, task_file_data: Dict[str, Any], file_path: Path) -> None:
        for task_name, task_def in self._iter_task_definitions(task_file_data, file_path):
            steps = task_def.get("steps") if isinstance(task_def, dict) else None
            if not isinstance(steps, dict):
                continue
            for step_id, step_def in steps.items():
                if not isinstance(step_def, dict):
                    continue
                illegal = sorted(self._REMOVED_STEP_FIELDS.intersection(step_def.keys()))
                if illegal:
                    fields = ", ".join(illegal)
                    raise TaskValidationError(
                        "deprecated_syntax",
                        f"Task file '{file_path.name}' uses removed step field(s) [{fields}] "
                        f"at '{task_name}.steps.{step_id}'. "
                        "Please remove 'goto' and replace step-level 'label' with 'step_note'."
                    )
                if "step_note" in step_def and not isinstance(step_def.get("step_note"), str):
                    raise TaskValidationError(
                        "task_validation_failed",
                        f"Task file '{file_path.name}' has invalid step_note type at "
                        f"'{task_name}.steps.{step_id}'. step_note must be a string."
                    )
                if "when" in step_def and not isinstance(step_def.get("when"), str):
                    raise TaskValidationError(
                        "task_validation_failed",
                        f"Task file '{file_path.name}' has invalid when type at "
                        f"'{task_name}.steps.{step_id}'. when must be a string."
                    )
                self._validate_depends_on_syntax(
                    step_def.get("depends_on"),
                    file_path=file_path,
                    task_name=task_name,
                    step_id=str(step_id),
                    field_path="depends_on",
                )

                action_name = step_def.get("action")
                if action_name == "run_task":
                    raise TaskValidationError(
                        "deprecated_syntax",
                        f"Task file '{file_path.name}' uses removed action alias 'run_task' "
                        f"at '{task_name}.steps.{step_id}'. Please use 'aura.run_task'."
                    )

                if action_name == "aura.run_task":
                    params = step_def.get("params", {})
                    if not isinstance(params, dict):
                        raise TaskValidationError(
                            "task_validation_failed",
                            f"Task file '{file_path.name}' has invalid params type at "
                            f"'{task_name}.steps.{step_id}'. aura.run_task params must be an object."
                        )

                    if "task_name" in params:
                        raise TaskValidationError(
                            "deprecated_syntax",
                            f"Task file '{file_path.name}' uses removed parameter 'task_name' at "
                            f"'{task_name}.steps.{step_id}'. Please use 'task_ref'."
                        )

                    task_ref = params.get("task_ref")
                    if not isinstance(task_ref, str) or not task_ref.strip():
                        raise TaskValidationError(
                            "task_validation_failed",
                            f"Task file '{file_path.name}' is missing required string parameter "
                            f"'task_ref' at '{task_name}.steps.{step_id}'."
                        )

                    try:
                        parsed_ref = TaskReference.from_string(
                            task_ref.strip(),
                            default_package=self.plan_name,
                        )
                    except Exception as exc:
                        raise TaskValidationError(
                            "task_validation_failed",
                            f"Task file '{file_path.name}' has invalid task_ref at "
                            f"'{task_name}.steps.{step_id}': {exc}"
                        ) from exc

                    if not parsed_ref.task_path.startswith("tasks:"):
                        raise TaskValidationError(
                            "task_validation_failed",
                            f"Task file '{file_path.name}' has invalid task_ref at "
                            f"'{task_name}.steps.{step_id}': task path must start with 'tasks:'."
                        )

    def _validate_depends_on_syntax(
        self,
        spec: Any,
        *,
        file_path: Path,
        task_name: str,
        step_id: str,
        field_path: str,
    ) -> None:
        location = f"{task_name}.steps.{step_id}.{field_path}"

        if spec is None:
            return

        if isinstance(spec, str):
            if spec.strip().startswith("when:"):
                raise TaskValidationError(
                    "deprecated_syntax",
                    f"Task file '{file_path.name}' uses removed inline dependency condition "
                    f"at '{location}'. Please move this condition to step-level 'when'."
                )
            return

        if isinstance(spec, list):
            raise TaskValidationError(
                "deprecated_syntax",
                f"Task file '{file_path.name}' uses removed list dependency shorthand at "
                f"'{location}'. Please use '{{ all: [...] }}' instead."
            )

        if isinstance(spec, dict):
            legacy_keys = {"and", "or", "not"}
            legacy_hit = sorted(legacy_keys.intersection(spec.keys()))
            if legacy_hit:
                raise TaskValidationError(
                    "deprecated_syntax",
                    f"Task file '{file_path.name}' uses removed dependency operator(s) "
                    f"{legacy_hit} at '{location}'. Please use 'all/any/none'."
                )

            logical_keys = {"all", "any", "none"}
            logical_hit = logical_keys.intersection(spec.keys())
            if logical_hit:
                if len(logical_hit) != 1 or len(spec) != 1:
                    raise TaskValidationError(
                        "task_validation_failed",
                        f"Task file '{file_path.name}' has invalid logical dependency object at "
                        f"'{location}'. It must contain exactly one of 'all', 'any', 'none'."
                )
                op = next(iter(logical_hit))
                payload = spec[op]
                if isinstance(payload, list):
                    for idx, item in enumerate(payload):
                        self._validate_depends_on_syntax(
                            item,
                            file_path=file_path,
                            task_name=task_name,
                            step_id=step_id,
                            field_path=f"{field_path}.{op}[{idx}]",
                        )
                    return
                self._validate_depends_on_syntax(
                    payload,
                    file_path=file_path,
                    task_name=task_name,
                    step_id=step_id,
                    field_path=f"{field_path}.{op}",
                )
                return

            if len(spec) != 1:
                raise TaskValidationError(
                    "task_validation_failed",
                    f"Task file '{file_path.name}' has invalid status dependency object at "
                    f"'{location}'. Status-query form must contain exactly one node key."
                )
            return

        raise TaskValidationError(
            "task_validation_failed",
            f"Task file '{file_path.name}' has unsupported depends_on type at '{location}': "
            f"{type(spec).__name__}."
        )
