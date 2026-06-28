# -*- coding: utf-8 -*-
"""Action execution adapter for rendering, injection and invocation."""

from __future__ import annotations

import asyncio
import contextvars
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
from packages.aura_core.scheduler.cancellation import request_task_cancel
from packages.aura_core.config.loader import get_config_value

from ..api import ACTION_REGISTRY, ActionDefinition
from ..config.template import TemplateRenderer
from ..context.execution import ExecutionContext
from ..types import TaskRefResolver
from .action_resolver import ActionResolver
from ..utils.middleware import middleware_manager

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine


class ActionInjector:
    """Resolve one action call inside an execution engine context."""

    def __init__(
        self,
        context: ExecutionContext,
        engine: "ExecutionEngine",
        renderer: TemplateRenderer,
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

        resolved_fqid = self.action_resolver.resolve(action_name)
        action_def = ACTION_REGISTRY.get(resolved_fqid)
        if not action_def:
            raise ValueError(f"Action '{action_name}' (resolved: '{resolved_fqid}') not found.")

        render_scope = await self.renderer.get_render_scope()
        rendered_params = await self.renderer.render(raw_params, scope=render_scope)
        return await middleware_manager.process(
            action_def=action_def,
            context=self.context,
            params=rendered_params,
            final_handler=self._invoke_action,
        )

    async def _invoke_action(
        self,
        action_def: ActionDefinition,
        _context: ExecutionContext,
        rendered_params: Dict[str, Any],
    ) -> Any:
        call_args = self._prepare_action_arguments(action_def, rendered_params)
        if action_def.is_async:
            return await action_def.func(**call_args)

        loop = asyncio.get_running_loop()
        context_snapshot = contextvars.copy_context()
        future = loop.run_in_executor(
            None,
            lambda: context_snapshot.run(action_def.func, **call_args),
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cid = current_cid()
            request_task_cancel(cid)
            grace_sec = max(float(get_config_value("execution.sync_action_cancel_grace_sec", 2.0) or 2.0), 0.0)
            if grace_sec > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=grace_sec)
                    logger.info(
                        "Sync action '%s' finished cleanup after cancellation request (cid=%s).",
                        action_def.name,
                        cid,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Sync action '%s' did not finish within %.1fs after cancellation request (cid=%s).",
                        action_def.name,
                        grace_sec,
                        cid,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Sync action '%s' ended with %s during cancellation cleanup (cid=%s).",
                        action_def.name,
                        type(exc).__name__,
                        cid,
                    )
            raise

    async def _execute_run_task(self, raw_params: Dict[str, Any]) -> Any:
        logger.info("Executing sub-task via aura.run_task")

        render_scope = await self.renderer.get_render_scope()
        rendered_params = await self.renderer.render(raw_params, scope=render_scope)

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

        orchestrator = self.engine.orchestrator
        parent_cid = self.context.data.get("cid")
        logger.debug(
            "Executing sub-task file='%s', key='%s' with parent_cid='%s'",
            task_file_path,
            task_key,
            parent_cid,
        )

        tfr = await orchestrator.execute_task(
            task_file_path=task_file_path,
            task_key=task_key,
            inputs=sub_task_inputs,
            parent_cid=parent_cid,
        )

        if tfr.get("status") in ("FAILED", "ERROR"):
            error_info = tfr.get("error", {"message": "Unknown error in sub-task."})
            raise Exception(f"Sub-task '{task_ref}' failed. Reason: {error_info}")

        return tfr.get("framework_data")

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
