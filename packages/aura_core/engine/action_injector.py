# -*- coding: utf-8 -*-
"""Action execution adapter for rendering, injection and invocation."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any, Dict

try:
    from pydantic import BaseModel, ValidationError

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    BaseModel = object  # type: ignore
    ValidationError = Exception  # type: ignore

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.observability.logging.core_logger import current_cid
from packages.aura_core.observability.runtime_trace import activity
from packages.aura_core.scheduler.operation_scope import OperationScope, current_operation_scope

from ..api import ACTION_REGISTRY, ActionDefinition
from ..config.template import TemplateRenderer
from ..context.execution import ExecutionContext
from ..types import TaskRefResolver
from .action_resolver import ActionResolver
from .execution_facade import _await_scoped
from ..utils.middleware import middleware_manager

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine


class ActionInjector:
    """Resolve one action call inside an execution engine context."""

    def __init__(
        self,
        context: ExecutionContext,
        engine: "ExecutionEngine",
        renderer: TemplateRenderer | None,
        services: Dict[str, Any],
        current_package=None,
        service_resolver=None,
    ):
        self.context = context
        self.engine = engine
        self.renderer = renderer
        self.services = services or {}
        self.task_services = (
            (context.data.get("task_services") or {})
            if hasattr(context, "data") and isinstance(context.data, dict)
            else {}
        )
        self.current_package = current_package
        self.service_resolver = service_resolver
        self.action_resolver = ActionResolver(current_package=current_package)

    async def execute(self, action_name: str, raw_params: Dict[str, Any]) -> Any:
        if action_name == "run_task":
            raise ValueError(
                "Action 'run_task' has been removed. Please use 'aura.run_task' with parameter 'task_ref'."
            )
        if action_name == "aura.run_task":
            return await self._execute_run_task(raw_params)

        if self.renderer is None:
            raise ValueError("YAML action execution requires a TemplateRenderer; use execute_typed for Python values")
        render_scope = await self.renderer.get_render_scope()
        rendered_params = await self.renderer.render(raw_params, scope=render_scope)
        return await self.execute_typed(action_name, rendered_params)

    def _operation_scope(self, scope: OperationScope | None) -> OperationScope:
        active = current_operation_scope()
        if scope is not None and active is not None:
            if scope._stop is not active._stop:
                raise ValueError("Explicit execution scope must share the current operation's stop state")
            if active.deadline is not None and (scope.deadline is None or scope.deadline > active.deadline):
                raise ValueError("Explicit execution scope cannot extend the current operation's deadline")
            if active.target is not None and scope.target is not active.target:
                raise ValueError("Explicit execution scope cannot change the current operation's target")
        inherited = scope if scope is not None else active
        cid = self.context.data.get("cid") or current_cid()
        if inherited is None:
            return OperationScope(cid=cid)
        if cid and cid != inherited.cid:
            # A child task has its own trace CID but must still observe its
            # parent's cancellation flag as well as the shared stop token.
            return OperationScope(parent=inherited, cid=cid, cancel_check=lambda: inherited.cancelled)
        return inherited

    async def execute_typed(self, action_name: str, values: Dict[str, Any], *,
                            scope: OperationScope | None = None) -> Any:
        """Execute already evaluated values without invoking the template renderer."""
        if action_name == "run_task":
            raise ValueError("Action 'run_task' has been removed. Please use 'aura.run_task'.")
        if not isinstance(action_name, str) or not action_name:
            raise ValueError("action_ref must be a nonempty string")
        if not isinstance(values, dict):
            raise TypeError("Typed action values must be a dictionary")
        operation = self._operation_scope(scope)
        with operation.activate():
            try:
                if action_name == "aura.run_task":
                    result = await self._run_task_values(dict(values), scope=operation)
                else:
                    result = await self._execute_typed_action(action_name, dict(values), operation)
                operation.checkpoint()
                return result
            except asyncio.CancelledError as exc:
                raise operation.cancellation_error(exc)

    async def _execute_typed_action(self, action_name, values, scope):

        resolved_fqid = self.action_resolver.resolve(action_name)
        action_def = ACTION_REGISTRY.get(resolved_fqid)
        if not action_def:
            raise ValueError(f"Action '{action_name}' (resolved: '{resolved_fqid}') not found.")

        async def invoke(action_def, context, params):
            # A synchronous middleware may call next from a thread with no
            # ContextVars. Rebind the captured scope at the actual call boundary.
            with scope.activate():
                return await self._invoke_action(action_def, context, params, scope=scope)

        return await _await_scoped(middleware_manager.process(
            action_def=action_def,
            context=self.context,
            params=values,
            final_handler=invoke,
        ), scope)

    async def _invoke_action(
        self,
        action_def: ActionDefinition,
        _context: ExecutionContext,
        rendered_params: Dict[str, Any],
        *, scope: OperationScope | None = None,
    ) -> Any:
        call_args = self._prepare_action_arguments(action_def, rendered_params)
        if action_def.is_async:
            with activity("async_action", action_def.name):
                return await action_def.func(**call_args)

        operation = self._operation_scope(scope)

        def invoke():
            with activity("sync_action", action_def.name):
                return action_def.func(**call_args)

        return await operation.run_async(invoke)

    async def _execute_run_task(self, raw_params: Dict[str, Any]) -> Any:
        if self.renderer is None:
            raise ValueError("YAML task execution requires a TemplateRenderer; use run_task_typed for Python values")
        render_scope = await self.renderer.get_render_scope()
        rendered_params = await self.renderer.render(raw_params, scope=render_scope)
        return await self.execute_typed("aura.run_task", rendered_params)

    async def run_task_typed(self, task_ref: str, inputs: Dict[str, Any] | None = None, *,
                             scope: OperationScope | None = None,
                             result_assertions: Dict[str, Any] | None = None) -> Any:
        return await self.execute_typed("aura.run_task", {
            "task_ref": task_ref,
            "inputs": {} if inputs is None else inputs,
            "result_assertions": result_assertions,
        }, scope=scope)

    async def _run_task_values(self, rendered_params: Dict[str, Any], *, scope: OperationScope) -> Any:
        """Single task contract used by YAML and context-bound Python callers."""
        logger.info("Executing sub-task via aura.run_task")

        if "task_name" in rendered_params:
            raise ValueError("aura.run_task no longer accepts 'task_name'. Please use 'task_ref'.")

        task_ref = rendered_params.get("task_ref")
        if not task_ref:
            raise ValueError("aura.run_task action requires a 'task_ref' parameter.")
        if not isinstance(task_ref, str):
            raise ValueError(f"task_ref must be a string, got {type(task_ref).__name__}")
        if ".." in task_ref:
            raise ValueError(f"Security: task_ref contains path traversal sequence '..' - {task_ref}")
        if task_ref.startswith("/") or task_ref.startswith("\\"):
            raise ValueError(f"Security: task_ref cannot be an absolute path - {task_ref}")

        current_package = self.engine.orchestrator.plan_name
        resolved = TaskRefResolver.resolve(
            task_ref,
            default_package=current_package,
            enforce_package=current_package,
            allow_cross_package=False,
        )
        task_file_path = resolved.task_file_path
        task_key = resolved.task_key

        logger.info(
            "Parsed task_ref: ref='%s', file='%s', key='%s', target_plan='%s'",
            task_ref,
            task_file_path,
            task_key,
            resolved.reference.package,
        )

        sub_task_inputs = rendered_params.get("inputs", {})
        if not isinstance(sub_task_inputs, dict):
            raise TypeError("aura.run_task 'inputs' parameter must be a dictionary.")
        result_assertions = rendered_params.get("result_assertions")
        if result_assertions is None:
            result_assertions = {}
        if not isinstance(result_assertions, dict) or any(
            not isinstance(path, str) or not all(path.split(".")) for path in result_assertions
        ):
            raise ValueError("aura.run_task 'result_assertions' must map nonempty dotted field paths to expected values.")

        orchestrator = self.engine.orchestrator
        parent_cid = self.context.data.get("cid")
        logger.debug(
            "Executing sub-task file='%s', key='%s' with parent_cid='%s'",
            task_file_path,
            task_key,
            parent_cid,
        )

        scope.checkpoint()
        tfr = await _await_scoped(orchestrator.execute_task(
            task_file_path=task_file_path,
            task_key=task_key,
            inputs=sub_task_inputs,
            parent_cid=parent_cid,
        ), scope)

        if tfr.get('status') == 'CANCELLED':
            from packages.aura_core.observability.failure_evidence import attach_evidence, evidence_from
            error = asyncio.CancelledError()
            attach_evidence(error, evidence_from(tfr) + evidence_from(tfr.get("error")))
            raise error
        if tfr.get("status") != "SUCCESS":
            error_info = tfr.get("error", {"message": "Unknown error in sub-task."})
            from packages.aura_core.observability.failure_evidence import attach_evidence, evidence_from
            error = Exception(f"Sub-task '{task_ref}' failed. Reason: {error_info}")
            attach_evidence(error, evidence_from(tfr) + evidence_from(error_info))
            raise error

        # Validate this iteration before a sequential loop can dispatch another
        # child. Side-effect-only tasks can explicitly declare `returns: true`.
        output = tfr.get("user_data")
        problem = None
        if output is None or (isinstance(output, dict) and not output):
            problem = "did not declare a nonempty business result"
        for path, expected in result_assertions.items():
            value = output
            missing = object()
            for field in path.split("."):
                value = value.get(field, missing) if isinstance(value, dict) else missing
            if value is missing or type(value) is not type(expected) or value != expected:
                problem = f"business result assertion failed for '{path}'"
                break
        if problem:
            from packages.aura_core.observability.failure_evidence import attach_evidence, evidence_from
            error = ValueError(f"Sub-task '{task_ref}' {problem}.")
            attach_evidence(error, evidence_from(tfr) + evidence_from(output))
            raise error
        return output

    def _prepare_action_arguments(self, action_def: ActionDefinition, rendered_params: Dict[str, Any]) -> Dict[str, Any]:
        sig = action_def.signature
        call_args: Dict[str, Any] = {}
        consumed_param_names = set()
        accepts_var_keyword = False

        pydantic_param_name = None
        pydantic_model_class = None
        for name, param_spec in sig.parameters.items():
            if inspect.isclass(param_spec.annotation) and issubclass(param_spec.annotation, BaseModel):
                pydantic_param_name = name
                pydantic_model_class = param_spec.annotation
                break

        if pydantic_param_name and pydantic_model_class:
            try:
                call_args[pydantic_param_name] = pydantic_model_class(**rendered_params)
                rendered_params = {}
            except ValidationError as exc:
                error_msg = f"Action '{action_def.name}' parameter validation failed: {exc}"
                logger.error(error_msg)
                raise ValueError(error_msg) from exc

        for param_name, param_spec in sig.parameters.items():
            if param_name in call_args:
                continue
            if param_spec.kind == inspect.Parameter.VAR_POSITIONAL:
                continue
            if param_spec.kind == inspect.Parameter.VAR_KEYWORD:
                accepts_var_keyword = True
                continue

            if param_name in action_def.service_deps:
                service_fqid = action_def.service_deps[param_name]
                if service_fqid in self.task_services:
                    call_args[param_name] = self.task_services[service_fqid]
                elif service_fqid in self.services:
                    call_args[param_name] = self.services[service_fqid]
                elif self.service_resolver:
                    service_instance = self.service_resolver(service_fqid)
                    self.services[service_fqid] = service_instance
                    call_args[param_name] = service_instance
                else:
                    raise ValueError(
                        f"Service dependency '{service_fqid}' for action '{action_def.name}' is not available in execution scope."
                    )
                continue

            if param_name == "context" or param_spec.annotation is ExecutionContext:
                call_args[param_name] = self.context
                continue

            if param_name == "engine":
                call_args[param_name] = self.engine
                continue

            if param_name in rendered_params:
                call_args[param_name] = rendered_params[param_name]
                consumed_param_names.add(param_name)
                continue

            if param_spec.default is not inspect.Parameter.empty:
                continue

            raise ValueError(f"Action '{action_def.name}' missing required parameter '{param_name}'")

        if accepts_var_keyword:
            for key, value in rendered_params.items():
                if key in consumed_param_names or key in call_args:
                    continue
                call_args[key] = value

        return call_args
