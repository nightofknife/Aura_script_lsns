# -*- coding: utf-8 -*-
"""Aura orchestration runtime for one plan."""

import asyncio
import os
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import aiofiles
    import aioshutil
    ASYNC_FILE_OPS_AVAILABLE = True
except ImportError:
    ASYNC_FILE_OPS_AVAILABLE = False
    aiofiles = None  # type: ignore
    aioshutil = None  # type: ignore

import yaml

from packages.aura_core.config.template import TemplateRenderer
from packages.aura_core.context.plan import PlanContext, current_plan_name
from packages.aura_core.context.persistence.strategy import NoPersistence, StateStorePersistence
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.context.state.planner import StatePlanner
from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger

from ..context.execution import ExecutionContext
from ..engine.action_injector import ActionInjector
from ..engine.execution_engine import ExecutionEngine
from ..packaging.core.task_loader import TaskLoader
from .validation import InputValidator


class Orchestrator:
    """Manage task execution inside one plan."""

    def __init__(
        self,
        base_dir: str,
        plan_name: str,
        pause_event: asyncio.Event,
        state_planner: Optional[StatePlanner] = None,
        loaded_package=None,
        runtime_services: Optional[Dict[str, Any]] = None,
        service_resolver=None,
    ):
        self.plan_name = plan_name
        loaded_plan_path = getattr(loaded_package, "path", None)
        if loaded_plan_path is not None:
            self.current_plan_path = Path(loaded_plan_path)
        else:
            self.current_plan_path = Path(base_dir) / 'plans' / plan_name
        self.pause_event = pause_event

        manifest = loaded_package.manifest if loaded_package and hasattr(loaded_package, 'manifest') else loaded_package
        self.task_loader = TaskLoader(self.plan_name, self.current_plan_path, manifest)

        self.state_planner = state_planner
        self.services = dict(runtime_services or {})
        self._service_resolver = service_resolver
        self.event_bus: EventBus = self.resolve_service('event_bus')
        self.state_store: StateStoreService = self.resolve_service('state_store')
        self.config_service = self.resolve_service('config')
        self.loaded_package = loaded_package
        self._input_validator = InputValidator(None)

        self.plan_context: Optional[PlanContext] = None
        self._plan_context_initialized = False

    def resolve_service(self, service_id: str) -> Any:
        if service_id in self.services:
            return self.services[service_id]
        if callable(self._service_resolver):
            service = self._service_resolver(service_id)
            self.services[service_id] = service
            return service
        raise RuntimeError(
            f"Service '{service_id}' is not available in orchestrator scope for plan '{self.plan_name}'."
        )

    async def _ensure_plan_context(self):
        if self._plan_context_initialized:
            return

        config_path = self.current_plan_path / 'config.yaml'
        plan_config = {}
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as handle:
                    plan_config = yaml.safe_load(handle) or {}
            except Exception as exc:
                logger.error(f"Failed to load plan config '{config_path}': {exc}")

        persistence_config = self.config_service.get('plan_context.persistence', {})
        if isinstance(persistence_config, dict):
            strategy_type = persistence_config.get('type', 'none')
            if strategy_type == 'file':
                storage_path = persistence_config.get('path', f'./plan_states/{self.plan_name}_state.json')
                persistence_strategy = StateStorePersistence(storage_path)
            else:
                persistence_strategy = NoPersistence()
        else:
            persistence_strategy = NoPersistence()

        self.plan_context = PlanContext(
            plan_name=self.plan_name,
            config_data=plan_config,
            persistence_strategy=persistence_strategy,
        )
        await self.plan_context.initialize()
        self._plan_context_initialized = True
        logger.info(f"PlanContext initialized for plan '{self.plan_name}'")

    async def _load_task_file(self, task_file_path: str) -> Dict[str, Any]:
        file_path_for_loading = task_file_path
        if not file_path_for_loading.endswith('.yaml'):
            file_path_for_loading = file_path_for_loading + '.yaml'

        full_path = self.current_plan_path / file_path_for_loading
        try:
            full_path = full_path.resolve()
            if not str(full_path).startswith(str(self.current_plan_path.resolve())):
                raise ValueError(f"Security: Task file path escapes plan directory: {task_file_path}")
        except Exception as exc:
            raise ValueError(f"Invalid task file path: {task_file_path}") from exc

        if not full_path.exists():
            error_msg = f"Task file not found: {task_file_path} (resolved to {full_path})"
            parent_dir = full_path.parent
            if parent_dir.exists() and parent_dir.is_dir():
                possible_file = parent_dir / (full_path.stem + '.yaml')
                if possible_file.exists():
                    error_msg += (
                        "\n\nPossible task reference format issue detected."
                        f"\n   Found file: {possible_file.relative_to(self.current_plan_path)}"
                        "\n   This looks like a multi-task file. Use:"
                        "\n   - tasks:test_state_transitions.yaml:task_name"
                        "\n   - not tasks:test_state_transitions:task_name"
                    )
            raise ValueError(error_msg)

        try:
            if ASYNC_FILE_OPS_AVAILABLE and aiofiles is not None:
                async with aiofiles.open(full_path, 'r', encoding='utf-8') as handle:
                    content = await handle.read()
            else:
                content = await asyncio.to_thread(full_path.read_text, encoding='utf-8')
            task_file_data = yaml.safe_load(content)

            if not isinstance(task_file_data, dict):
                raise ValueError(f"Task file must contain a dictionary, got {type(task_file_data)}")

            logger.debug(f"Loaded task file '{task_file_path}' with {len(task_file_data)} task(s)")
            return task_file_data
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse YAML file '{task_file_path}': {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Failed to load task file '{task_file_path}': {exc}") from exc

    @staticmethod
    def _build_event_task_name(
        task_file_path: Optional[str],
        task_key: Optional[str],
    ) -> str:
        path = (task_file_path or '').strip().replace('\\', '/')
        if not path:
            return 'tasks:unknown.yaml'

        if path.startswith('tasks/'):
            canonical = path.replace('/', ':')
        elif path.startswith('tasks:'):
            canonical = path
        else:
            canonical = f"tasks:{path.replace('/', ':')}"

        if not canonical.endswith('.yaml'):
            canonical = f"{canonical}.yaml"
        if task_key:
            return f"{canonical}:{task_key}"
        return canonical

    def _validate_and_normalize_task_inputs(
        self,
        *,
        task_data: Dict[str, Any],
        full_task_id: str,
        inputs: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        raw_inputs = inputs or {}
        if not isinstance(raw_inputs, dict):
            raise ValueError('Inputs must be an object/dict.')
        inputs_meta = (task_data.get('meta', {}) or {}).get('inputs', [])
        ok, validated_or_error = self._input_validator.validate_inputs_against_meta(inputs_meta, raw_inputs)
        if not ok:
            raise ValueError(f"Task '{full_task_id}' inputs invalid: {validated_or_error}")
        return validated_or_error if isinstance(validated_or_error, dict) else {}

    async def _render_task_returns(
        self,
        *,
        final_context: ExecutionContext,
        returns_template: Any,
        full_task_id: str,
    ) -> Any:
        renderer = TemplateRenderer(final_context, self.state_store)
        try:
            return await renderer.render(returns_template)
        except Exception as exc:
            raise ValueError(
                f"Failed to render returns for task '{full_task_id}': "
                f"{type(exc).__name__}: {exc}. returns={returns_template!r}"
            ) from exc

    async def execute_task(
        self,
        *,
        task_file_path: str,
        task_key: Optional[str] = None,
        triggering_event: Optional[Event] = None,
        inputs: Optional[Dict[str, Any]] = None,
        cid: Optional[str] = None,
        parent_cid: Optional[str] = None,
        trace_id: Optional[str] = None,
        trace_label: Optional[str] = None,
        source: Optional[str] = None,
        planning_depth: int = 0,
    ) -> Dict[str, Any]:
        token = current_plan_name.set(self.plan_name)
        logger.debug(f"Configuration context set to: '{self.plan_name}'")

        await self._ensure_plan_context()

        task_start_time = time.time()
        cid = cid or str(uuid.uuid4())
        if source is None:
            source = 'internal'

        if not trace_id:
            time_part = time.strftime('%y%m%d-%H%M%S', time.localtime(task_start_time))
            suffix = cid[-4:] if cid else '0000'
            task_display_name = task_file_path.replace('.yaml', '').replace('/', ':')
            if task_key:
                task_display_name = f"{task_display_name}:{task_key}"
            trace_id = f"{self.plan_name}/{task_display_name}@{time_part}-{suffix}"

        if not trace_label:
            task_display_name = task_file_path.replace('.yaml', '').replace('/', ':')
            if task_key:
                task_display_name = f"{task_display_name}:{task_key}"
            trace_label = f"{self.plan_name}/{task_display_name}"

        event_task_name = self._build_event_task_name(task_file_path, task_key)

        await self.event_bus.publish(Event(
            name='task.started',
            payload={
                'cid': cid,
                'parent_cid': parent_cid,
                'trace_id': trace_id,
                'trace_label': trace_label,
                'source': source,
                'plan_name': self.plan_name,
                'task_name': event_task_name,
                'task_file_path': task_file_path,
                'task_key': task_key,
                'start_time': task_start_time,
                'inputs': inputs or {},
            },
        ))

        final_status = 'UNKNOWN'
        user_data = None
        framework_data = None
        error_details = None

        try:
            task_file_data = await self._load_task_file(task_file_path)
            if not task_file_data:
                raise ValueError(f"Task file not found: {task_file_path}")

            def has_yaml_suffix(path: str) -> bool:
                return path.endswith('.yaml')

            explicit_task_key = task_key
            if task_key:
                task_data = task_file_data.get(task_key)
                if not task_data:
                    raise ValueError(f"Task '{task_key}' not found in file '{task_file_path}'")
                resolved_task_key = task_key
            elif has_yaml_suffix(task_file_path):
                if isinstance(task_file_data.get('steps'), (list, dict)):
                    task_data = task_file_data
                    resolved_task_key = None
                else:
                    filename = task_file_path.replace('.yaml', '').split('/')[-1]
                    task_data = task_file_data.get(filename)
                    if not task_data:
                        raise ValueError(f"Task '{filename}' not found in file '{task_file_path}'")
                    resolved_task_key = filename
            else:
                if not task_file_data:
                    raise ValueError(f"Task file '{task_file_path}' is empty")
                resolved_task_key = list(task_file_data.keys())[0]
                task_data = task_file_data[resolved_task_key]
                logger.debug(f"No .yaml suffix, using first task: '{resolved_task_key}'")

            normalized_path = task_file_path.replace('.yaml', '')
            if normalized_path.startswith('tasks/'):
                normalized_path = normalized_path[6:]
            if explicit_task_key:
                full_task_id = f"{self.plan_name}/{normalized_path}/{resolved_task_key}"
            elif resolved_task_key and resolved_task_key != normalized_path.split('/')[-1]:
                full_task_id = f"{self.plan_name}/{normalized_path}/{resolved_task_key}"
            else:
                full_task_id = f"{self.plan_name}/{normalized_path}"

            validated_inputs = self._validate_and_normalize_task_inputs(
                task_data=task_data,
                full_task_id=full_task_id,
                inputs=inputs,
            )

            root_context = ExecutionContext(
                inputs=validated_inputs,
                cid=cid,
                plan_context=self.plan_context,
            )

            async def step_event_callback(event_name: str, payload: Dict):
                payload['cid'] = cid
                payload['parent_cid'] = parent_cid
                payload['trace_id'] = trace_id
                payload['trace_label'] = trace_label
                payload['source'] = source
                payload['plan_name'] = self.plan_name
                payload['task_file_path'] = task_file_path
                payload['task_key'] = task_key
                await self.event_bus.publish(Event(name=event_name, payload=payload))

            engine = ExecutionEngine(
                orchestrator=self,
                pause_event=self.pause_event,
                event_callback=step_event_callback,
            )
            final_context = await engine.run(task_data, full_task_id, root_context)
            framework_data = final_context.data

            is_failed = False
            for node_result in framework_data.get('nodes', {}).values():
                run_state = node_result.get('run_state', {})
                if run_state.get('status') == 'FAILED':
                    final_status = 'FAILED'
                    error_details = run_state.get('error', {'message': 'A node failed.'})
                    if 'node_id' in node_result.get('run_state', {}):
                        error_details['node_id'] = node_result['run_state']['node_id']
                    is_failed = True
                    break

            if not is_failed:
                final_status = 'SUCCESS'

            if final_status == 'SUCCESS':
                returns_template = task_data.get('returns')
                if returns_template is not None:
                    user_data = await self._render_task_returns(
                        final_context=final_context,
                        returns_template=returns_template,
                        full_task_id=full_task_id,
                    )
                else:
                    user_data = True

        except asyncio.CancelledError:
            final_status = 'CANCELLED'
            error_details = {'message': 'Task cancellation requested.', 'type': 'CancelledError'}
            logger.warning(
                "Task execution cancelled at orchestrator level for '%s:%s'.",
                task_file_path,
                task_key,
            )
            raise
        except Exception as exc:
            final_status = 'ERROR'
            error_details = {'message': str(exc), 'type': type(exc).__name__}
            if getattr(self, 'debug_mode', False):
                error_details['traceback'] = traceback.format_exc()
            logger.critical(
                f"Task execution failed at orchestrator level for '{task_file_path}:{task_key}': {exc}",
                exc_info=True,
            )

        finally:
            tfr_object = {
                'status': final_status,
                'user_data': user_data,
                'framework_data': framework_data,
                'error': error_details,
            }
            await self.event_bus.publish(Event(
                name='task.finished',
                payload={
                    'cid': cid,
                    'parent_cid': parent_cid,
                    'trace_id': trace_id,
                    'trace_label': trace_label,
                    'source': source,
                    'plan_name': self.plan_name,
                    'task_name': event_task_name,
                    'end_time': time.time(),
                    'duration': time.time() - task_start_time,
                    'final_status': final_status,
                    'final_result': tfr_object,
                },
            ))
            current_plan_name.reset(token)
            logger.debug(f"Configuration context reset (was: '{self.plan_name}')")

        return tfr_object

    async def perform_condition_check(self, condition_data: dict) -> bool:
        """执行一个条件检查。

        这通常用于评估中断规则的 `condition` 块。

        Args:
            condition_data (dict): 包含 `action` 和 `params` 的条件定义。

        Returns:
            bool: 条件检查的结果，True 或 False。
        """
        action_name = condition_data.get('action')
        if not action_name: return False

        try:
            temp_context = ExecutionContext()
            renderer = TemplateRenderer(temp_context, self.state_store)
            injector = ActionInjector(
                temp_context,
                self,
                renderer,
                self.services,
                current_package=self.loaded_package,
                service_resolver=self.resolve_service,
            )

            params = condition_data.get('params', {})
            rendered_params = await renderer.render(params)
            result = await injector.execute(action_name, rendered_params)

            return bool(result)
        except Exception as e:
            logger.error(f"条件检查 '{action_name}' 失败: {e}", exc_info=False)
            return False

    def load_task_data(self, full_task_id: str) -> Optional[Dict]:
        """加载指定任务的定义数据。

        Args:
            full_task_id (str): 任务的完全限定ID (e.g., "my_plan/my_task")。

        Returns:
            包含任务定义的字典，如果找不到则返回 None。
        """
        try:
            plan_name, task_name_in_plan = full_task_id.split('/', 1)
            if plan_name == self.plan_name:
                return self.task_loader.get_task_data(task_name_in_plan)
            logger.error(f"Orchestrator for '{self.plan_name}' cannot load task for other plan: '{full_task_id}'")
        except ValueError:
            logger.error(f"Invalid full_task_id format for loading: '{full_task_id}'")
        return None

    @property
    def task_definitions(self) -> Dict[str, Any]:
        """获取此 Plan 中所有已加载的任务定义。"""
        return self.task_loader.get_all_task_definitions()

    def _resolve_and_validate_path(self, relative_path: str) -> Path:
        """(私有) 将相对路径解析为绝对路径，并进行安全检查以防止路径穿越。"""
        safe_relative_path = os.path.normpath(relative_path)
        if safe_relative_path.startswith(('..', '/')):
            raise ValueError(f"不安全的路径: '{relative_path}'。禁止访问父目录或绝对路径。")

        full_path = self.current_plan_path.joinpath(safe_relative_path).resolve()

        if not str(full_path).startswith(str(self.current_plan_path.resolve())):
            raise ValueError(f"路径穿越攻击被阻止: '{relative_path}' 解析到了 Plan 目录之外。")

        return full_path

    async def get_file_content(self, relative_path: str) -> str:
        """异步、安全地读取 Plan 目录内的一个文件内容（文本模式）。"""
        file_path = self._resolve_and_validate_path(relative_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"文件未找到: '{relative_path}'")

        if ASYNC_FILE_OPS_AVAILABLE and aiofiles is not None:
            async with aiofiles.open(file_path, mode='r', encoding='utf-8') as f:
                return await f.read()
        return await asyncio.to_thread(file_path.read_text, encoding='utf-8')

    async def get_file_content_bytes(self, relative_path: str) -> bytes:
        """异步、安全地读取 Plan 目录内的一个文件内容（二进制模式）。"""
        file_path = self._resolve_and_validate_path(relative_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"文件未找到: '{relative_path}'")

        if ASYNC_FILE_OPS_AVAILABLE and aiofiles is not None:
            async with aiofiles.open(file_path, mode='rb') as f:
                return await f.read()
        return await asyncio.to_thread(file_path.read_bytes)

    async def save_file_content(self, relative_path: str, content: Any) -> None:
        """异步、安全地向 Plan 目录内的一个文件写入内容。

        如果内容是字典或列表，则自动保存为 YAML 格式；否则保存为文本。
        如果文件是字节流，则以二进制模式写入。
        """
        file_path = self._resolve_and_validate_path(relative_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, (dict, list)):
            loop = asyncio.get_running_loop()
            serialized_content = await loop.run_in_executor(
                None,
                lambda: yaml.dump(content, allow_unicode=True, sort_keys=False, indent=2)
            )
            is_binary = False
        elif isinstance(content, bytes):
            serialized_content = content
            is_binary = True
        else:
            serialized_content = str(content)
            is_binary = False

        if ASYNC_FILE_OPS_AVAILABLE and aiofiles is not None:
            if is_binary:
                async with aiofiles.open(file_path, mode='wb') as f:
                    await f.write(serialized_content)
            else:
                async with aiofiles.open(file_path, mode='w', encoding='utf-8') as f:
                    await f.write(serialized_content)
            return

        if is_binary:
            await asyncio.to_thread(file_path.write_bytes, serialized_content)
        else:
            await asyncio.to_thread(file_path.write_text, serialized_content, encoding='utf-8')

    async def create_directory(self, relative_path: str) -> None:
        """异步、安全地在 Plan 目录内创建一个新目录。"""
        dir_path = self._resolve_and_validate_path(relative_path)
        dir_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"目录已创建: {dir_path}")

    async def create_file(self, relative_path: str, content: str = "") -> None:
        """异步、安全地在 Plan 目录内创建一个新文件。"""
        file_path = self._resolve_and_validate_path(relative_path)
        await self.save_file_content(relative_path, content)
        logger.info(f"文件已创建: {file_path}")

    async def rename_path(self, old_relative_path: str, new_relative_path: str) -> None:
        """异步、安全地在 Plan 目录内重命名一个文件或目录。"""
        old_path = self._resolve_and_validate_path(old_relative_path)
        new_path = self._resolve_and_validate_path(new_relative_path)

        if not old_path.exists():
            raise FileNotFoundError(f"源路径不存在: '{old_relative_path}'")
        if new_path.exists():
            raise FileExistsError(f"目标路径已存在: '{new_relative_path}'")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.rename, old_path, new_path)
        logger.info(f"路径已重命名: 从 '{old_relative_path}' 到 '{new_relative_path}'")

    async def delete_path(self, relative_path: str) -> None:
        """异步、安全地删除 Plan 目录内的一个文件或目录（如果是目录则递归删除）。"""
        path_to_delete = self._resolve_and_validate_path(relative_path)

        if not path_to_delete.exists():
            logger.warning(f"尝试删除不存在的路径: '{relative_path}'，操作被跳过。")
            return

        if path_to_delete.is_file():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, os.remove, path_to_delete)
            logger.info(f"文件已删除: {path_to_delete}")
        elif path_to_delete.is_dir():
            await aioshutil.rmtree(path_to_delete)
            logger.info(f"目录已递归删除: {path_to_delete}")
