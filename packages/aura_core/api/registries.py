# -*- coding: utf-8 -*-
"""Aura 框架的注册表类。

此模块包含用于管理 Actions、Services 和 Hooks 的注册表类。
"""
import asyncio
import inspect
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set

from .definitions import ActionDefinition, ServiceDefinition, HookResult
from packages.aura_core.observability.logging.core_logger import logger


class ActionRegistry:
    """管理所有 Action 定义的注册表。

    这是一个线程安全的类,用于注册、查询和移除 Action 定义。

    ✅ 修复：支持FQID索引,防止不同包的同名Action冲突。
    """
    def __init__(self):
        """初始化 ActionRegistry。"""
        self._actions_by_fqid: Dict[str, ActionDefinition] = {}  # FQID -> ActionDefinition
        self._actions_by_name: Dict[str, ActionDefinition] = {}  # 简单名称 -> ActionDefinition(向后兼容)
        self._lock = threading.RLock()  # ✅ 添加线程锁

    def clear(self):
        """清空注册表中的所有 Action 定义。"""
        with self._lock:
            self._actions_by_fqid.clear()
            self._actions_by_name.clear()

    def register(self, action_def: ActionDefinition):
        """注册一个新的 Action 定义。

        ✅ 修复：使用FQID作为主键,禁止包内同名Action。

        Args:
            action_def: 要注册的 Action 定义对象。

        Raises:
            RuntimeError: 如果FQID已存在(同一包内定义了同名Action)。
        """
        with self._lock:
            # ✅ 检查FQID是否已存在(严格禁止包内同名)
            if action_def.fqid in self._actions_by_fqid:
                existing = self._actions_by_fqid[action_def.fqid]
                raise RuntimeError(
                    f"Action FQID冲突！'{action_def.fqid}' 已被注册。\n"
                    f"同一个包不能定义多个同名Action。\n"
                    f"已存在: {existing.func.__module__}.{existing.func.__name__}\n"
                    f"尝试注册: {action_def.func.__module__}.{action_def.func.__name__}"
                )

            # ✅ 注册到FQID索引
            self._actions_by_fqid[action_def.fqid] = action_def

            # ✅ 注册到简单名称索引(向后兼容,但会警告冲突)
            if action_def.name in self._actions_by_name:
                existing_fqid = self._actions_by_name[action_def.name].fqid
                logger.warning(
                    f"Action简单名称冲突！'{action_def.name}' 已被 '{existing_fqid}' 注册。\n"
                    f"新注册的 '{action_def.fqid}' 将覆盖简单名称索引。\n"
                    f"建议：调用外部包的Action时请使用完整FQID: '{action_def.fqid}'"
                )

            self._actions_by_name[action_def.name] = action_def

            logger.debug(
                f"已注册Action: '{action_def.fqid}' (公开: {action_def.public}, 异步: {action_def.is_async})"
            )

    def get_all_action_definitions(self) -> List[ActionDefinition]:
        """获取所有已注册的 Action 定义列表,按 FQID 排序。

        Returns:
            一个包含所有 ActionDefinition 对象的列表。
        """
        with self._lock:
            return sorted(list(self._actions_by_fqid.values()), key=lambda a: a.fqid)

    def remove_actions_by_plugin(self, plugin_id: str):
        """移除所有属于特定插件的 Action 定义。

        在插件卸载或重载时使用。

        Args:
            plugin_id (str): 插件的规范ID (canonical_id)。
        """
        with self._lock:
            # ✅ 遍历FQID索引
            fqids_to_remove = []
            for fqid, action_def in self._actions_by_fqid.items():
                # ✅ 统一使用新系统获取plugin_id
                current_plugin_id = action_def.plugin.package.canonical_id

                if current_plugin_id == plugin_id:
                    fqids_to_remove.append(fqid)

            if fqids_to_remove:
                logger.info(f"正在为插件 '{plugin_id}' 移除 {len(fqids_to_remove)} 个 Action...")
                for fqid in fqids_to_remove:
                    action_def = self._actions_by_fqid.pop(fqid)
                    # 同时从简单名称索引中移除
                    if self._actions_by_name.get(action_def.name) == action_def:
                        del self._actions_by_name[action_def.name]

    def __len__(self) -> int:
        """返回已注册的 Action 数量。"""
        with self._lock:
            return len(self._actions_by_fqid)

    def get(self, name_or_fqid: str) -> Optional[ActionDefinition]:
        """根据名称或FQID获取一个 Action 定义。

        ✅ 修复：优先使用FQID查找,支持完整的命名空间隔离。

        Args:
            name_or_fqid: Action 的简单名称或完整FQID。

        Returns:
            对应的 ActionDefinition 对象,如果不存在则返回 None。
        """
        with self._lock:
            # ✅ 优先查找FQID(包含'/'表示是FQID)
            if '/' in name_or_fqid:
                return self._actions_by_fqid.get(name_or_fqid)

            # 再查找简单名称(向后兼容)
            return self._actions_by_name.get(name_or_fqid)


class ServiceRegistry:
    """Registry for service definitions and instances.

    Override policy (strict):
    1. Alias conflict without explicit `replace` fails fast.
    2. `replace` is allowed only for non-core targets.
    3. Core services can never be replaced.
    4. Replace target must exist, be unique, and contract-compatible.
    """

    def __init__(self):
        self._fqid_map: Dict[str, ServiceDefinition] = {}
        self._active_alias_map: Dict[str, str] = {}
        self._instances: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._registering: Set[str] = set()

    def is_registered(self, fqid: str) -> bool:
        with self._lock:
            return fqid in self._fqid_map

    def clear(self):
        with self._lock:
            self._fqid_map.clear()
            self._active_alias_map.clear()
            self._instances.clear()
            self._registering.clear()
            logger.info("Service registry cleared.")

    def register(self, definition: ServiceDefinition):
        with self._lock:
            if definition.fqid in self._registering:
                raise RuntimeError(f"Service '{definition.fqid}' is being registered concurrently.")
            if definition.fqid in self._fqid_map:
                raise RuntimeError(f"Service fqid conflict: '{definition.fqid}' is already registered.")

            if definition.domain not in {"core", "package"}:
                raise RuntimeError(
                    f"Invalid service domain '{definition.domain}' for '{definition.fqid}'. "
                    "Expected 'core' or 'package'."
                )
            if definition.plugin is None:
                definition.domain = "core"

            self._registering.add(definition.fqid)
            try:
                existing_active_fqid = self._active_alias_map.get(definition.alias)
                replace_target = self._resolve_replace_target(definition.replace)

                if existing_active_fqid and not definition.replace:
                    raise RuntimeError(
                        f"Service alias conflict for '{definition.alias}': "
                        f"'{existing_active_fqid}' already active. "
                        "Declare `replace` explicitly to replace a non-core service."
                    )

                if definition.replace:
                    if replace_target is None:
                        raise RuntimeError(
                            f"Service '{definition.fqid}' declares replace='{definition.replace}', "
                            "but target does not exist or is ambiguous."
                        )
                    if existing_active_fqid and replace_target.fqid != existing_active_fqid:
                        raise RuntimeError(
                            f"Service '{definition.fqid}' cannot replace '{replace_target.fqid}' "
                            f"while active alias '{definition.alias}' points to '{existing_active_fqid}'."
                        )
                    if replace_target.alias != definition.alias:
                        raise RuntimeError(
                            f"Service '{definition.fqid}' replace target alias mismatch: "
                            f"target '{replace_target.alias}', new '{definition.alias}'."
                        )
                    if self._is_core_service(replace_target):
                        raise RuntimeError(
                            f"Core service '{replace_target.fqid}' cannot be replaced."
                        )
                    self._validate_contract_compatibility(definition, replace_target)
                    self._validate_replace_chain_no_cycle(definition.fqid, replace_target.fqid)
                    definition.replaced_target_fqid = replace_target.fqid
                    replace_target.status = "replaced"

                self._fqid_map[definition.fqid] = definition
                self._active_alias_map[definition.alias] = definition.fqid
                logger.debug(
                    "Registered service '%s' alias='%s' domain=%s replace=%s",
                    definition.fqid,
                    definition.alias,
                    definition.domain,
                    definition.replace,
                )
            finally:
                self._registering.discard(definition.fqid)

    def register_instance(self, alias: str, instance: Any, fqid: Optional[str] = None, public: bool = False):
        with self._lock:
            target_fqid = fqid or f"core/{alias}"
            if alias in self._active_alias_map or target_fqid in self._fqid_map:
                existing_fqid = self._active_alias_map.get(alias) or target_fqid
                raise RuntimeError(
                    f"Core service registration conflict for alias '{alias}' "
                    f"(existing: '{existing_fqid}', new: '{target_fqid}')."
                )

            definition = ServiceDefinition(
                alias=alias,
                fqid=target_fqid,
                service_class=type(instance),
                plugin=None,
                public=public,
                domain="core",
                replace=None,
                instance=instance,
                status="resolved",
                singleton=True,
            )
            self._fqid_map[target_fqid] = definition
            self._active_alias_map[alias] = target_fqid
            self._instances[target_fqid] = instance
            logger.info("Registered core service instance '%s' (alias='%s').", target_fqid, alias)

    def get_service_instance(self, service_id: str, resolution_chain: Optional[List[str]] = None) -> Any:
        with self._lock:
            is_fqid_request = '/' in service_id
            target_fqid = service_id if is_fqid_request else self._active_alias_map.get(service_id)
            if not target_fqid:
                raise NameError(f"Service '{service_id}' is not registered.")
            if target_fqid in self._instances:
                return self._instances[target_fqid]
            return self._instantiate_service(target_fqid, resolution_chain or [])

    def _instantiate_service(self, fqid: str, resolution_chain: List[str]) -> Any:
        if fqid in resolution_chain:
            raise RecursionError(
                f"Service circular dependency detected: {' -> '.join(resolution_chain)} -> {fqid}"
            )
        resolution_chain.append(fqid)
        definition = self._fqid_map.get(fqid)
        if not definition:
            raise NameError(f"Service definition '{fqid}' not found.")
        if definition.status == "failed":
            raise RuntimeError(f"Service '{fqid}' previously failed to instantiate.")
        if definition.status == "resolving":
            raise RuntimeError(f"Service '{fqid}' is currently resolving.")

        try:
            definition.status = "resolving"
            dependencies = self._resolve_constructor_dependencies(definition, resolution_chain)
            instance = definition.service_class(**dependencies)
            if definition.singleton:
                self._instances[fqid] = instance
                definition.instance = instance
                definition.status = "resolved"
            else:
                definition.status = "defined"
            logger.info("Service '%s' instantiated.", fqid)
            return instance
        except Exception as e:
            definition.status = "failed"
            logger.error("Instantiate service '%s' failed: %s", fqid, e, exc_info=True)
            raise
        finally:
            if fqid in resolution_chain:
                resolution_chain.pop()

    def _resolve_constructor_dependencies(
        self, definition: ServiceDefinition, resolution_chain: List[str]
    ) -> Dict[str, Any]:
        dependencies = {}
        explicit_deps = dict(definition.service_deps or {})
        if explicit_deps:
            for param_name, dependency_fqid in explicit_deps.items():
                dependencies[param_name] = self.get_service_instance(dependency_fqid, resolution_chain)
            return dependencies

        init_signature = inspect.signature(definition.service_class.__init__)
        type_to_fqid_map = {
            self._fqid_map[active_fqid].service_class: active_fqid
            for active_fqid in self._active_alias_map.values()
        }
        for param_name, param in init_signature.parameters.items():
            if param_name in ["self"]:
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            dependency_fqid = None
            if param.annotation is not inspect.Parameter.empty and param.annotation in type_to_fqid_map:
                dependency_fqid = type_to_fqid_map[param.annotation]
            if dependency_fqid is None:
                dependency_alias = param_name
                dependency_fqid = self._active_alias_map.get(dependency_alias)
                if not dependency_fqid:
                    if param.default is not inspect.Parameter.empty:
                        continue
                    raise NameError(
                        f"Failed to resolve dependency '{param_name}' for service '{definition.fqid}'."
                    )
            dependencies[param_name] = self.get_service_instance(dependency_fqid, resolution_chain)
        return dependencies

    def get_all_service_definitions(self) -> List[ServiceDefinition]:
        with self._lock:
            return sorted(list(self._fqid_map.values()), key=lambda s: s.fqid)

    def get_all_services(self) -> Dict[str, Any]:
        with self._lock:
            services: Dict[str, Any] = {}
            for alias, fqid in self._active_alias_map.items():
                if fqid not in self._instances:
                    continue
                instance = self._instances[fqid]
                services[alias] = instance
                services[fqid] = instance
            return services

    def validate_no_circular_dependencies(self):
        with self._lock:
            from graphlib import TopologicalSorter

            logger.info("Validating service dependency graph...")
            graph = {}
            for fqid in self._active_alias_map.values():
                definition = self._fqid_map[fqid]
                if definition.status == "resolved":
                    continue

                deps = self._collect_dependency_fqids(definition)

                graph[fqid] = deps

            try:
                sorter = TopologicalSorter(graph)
                sorter.prepare()
                logger.info("Service dependency graph validated (%d services).", len(graph))
            except Exception as e:
                error_msg = f"Circular service dependency detected: {e}\n\nDependency graph:\n"
                for service_fqid, service_deps in graph.items():
                    if service_deps:
                        error_msg += f"  {service_fqid} -> {service_deps}\n"
                logger.error(error_msg)
                raise ValueError(error_msg)

    def _collect_dependency_fqids(self, definition: ServiceDefinition) -> List[str]:
        if definition.service_deps:
            return list(definition.service_deps.values())

        deps: List[str] = []
        init_signature = inspect.signature(definition.service_class.__init__)
        type_to_fqid_map = {
            self._fqid_map[active_fqid].service_class: active_fqid
            for active_fqid in self._active_alias_map.values()
        }

        for param_name, param in init_signature.parameters.items():
            if param_name in ["self"]:
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            dependency_fqid = None
            if param.annotation is not inspect.Parameter.empty and param.annotation in type_to_fqid_map:
                dependency_fqid = type_to_fqid_map[param.annotation]
            elif param_name in self._active_alias_map:
                dependency_fqid = self._active_alias_map[param_name]

            if dependency_fqid:
                deps.append(dependency_fqid)

        return deps

    def remove_services_by_prefix(self, prefix: str = "", exclude_prefix: Optional[str] = None):
        with self._lock:
            fqids_to_remove = [
                fqid
                for fqid in list(self._fqid_map.keys())
                if fqid.startswith(prefix) and not (exclude_prefix and fqid.startswith(exclude_prefix))
            ]
            if fqids_to_remove:
                logger.info("Removing %d services by prefix '%s'.", len(fqids_to_remove), prefix)
                for fqid in fqids_to_remove:
                    definition = self._fqid_map.pop(fqid, None)
                    instance = self._instances.pop(fqid, None)

                    if instance and hasattr(instance, 'shutdown'):
                        try:
                            logger.debug("Calling shutdown hook for service '%s'.", fqid)
                            instance.shutdown()
                        except Exception as e:
                            logger.error("Service '%s' shutdown failed: %s", fqid, e, exc_info=True)

                    if definition and self._active_alias_map.get(definition.alias) == fqid:
                        self._active_alias_map.pop(definition.alias, None)
                    logger.debug("Service '%s' removed.", fqid)

    def _resolve_replace_target(self, replace: Optional[str]) -> Optional[ServiceDefinition]:
        if not replace:
            return None
        token = str(replace).strip()
        if not token:
            return None
        if "/" in token:
            return self._fqid_map.get(token)

        candidates = [sd for sd in self._fqid_map.values() if sd.alias == token]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return None
        active_fqid = self._active_alias_map.get(token)
        if not active_fqid:
            return None
        return self._fqid_map.get(active_fqid)

    @staticmethod
    def _is_core_service(definition: ServiceDefinition) -> bool:
        if definition.domain == "core":
            return True
        if definition.plugin is None:
            return True
        return definition.fqid.startswith("core/")

    @staticmethod
    def _validate_contract_compatibility(new_def: ServiceDefinition, target_def: ServiceDefinition):
        new_cls = new_def.service_class
        target_cls = target_def.service_class
        if new_cls is target_cls:
            return
        if issubclass(new_cls, target_cls):
            return
        if issubclass(target_cls, new_cls):
            return
        new_bases = set(new_cls.mro())
        target_bases = set(target_cls.mro())
        common_non_object = [base for base in (new_bases & target_bases) if base is not object]
        if common_non_object:
            return
        raise RuntimeError(
            f"Service replacement contract mismatch: '{new_def.fqid}' ({new_cls.__name__}) "
            f"is not compatible with '{target_def.fqid}' ({target_cls.__name__})."
        )

    def _validate_replace_chain_no_cycle(self, new_fqid: str, target_fqid: str):
        visited: Set[str] = set()
        current = target_fqid
        while current:
            if current == new_fqid:
                raise RuntimeError(f"Replace chain cycle detected: '{new_fqid}' -> ... -> '{current}'")
            if current in visited:
                break
            visited.add(current)
            current_def = self._fqid_map.get(current)
            if not current_def:
                break
            current = current_def.replaced_target_fqid


class HookManager:
    """管理框架中所有钩子(Hook)的注册与触发。

    这是一个异步的、线程安全的类,用于处理框架生命周期事件。
    """
    def __init__(self):
        """初始化 HookManager。"""
        self._hooks: Dict[str, List[Callable]] = defaultdict(list)

    def register(self, hook_name: str, func: Callable):
        """注册一个钩子回调函数。

        Args:
            hook_name: 钩子的名称。
            func: 要注册的回调函数。
        """
        logger.debug(f"注册钩子 '{hook_name}' -> {func.__module__}.{func.__name__}")
        self._hooks[hook_name].append(func)

    async def trigger(self, hook_name: str, *args, **kwargs):
        """异步地触发一个钩子,并执行所有已注册的回调函数。

        会并发执行所有回调,并忽略执行中的异常(仅记录日志)。

        Args:
            hook_name: 要触发的钩子的名称。
            *args: 传递给回调函数的位置参数。
            **kwargs: 传递给回调函数的关键字参数。
        """
        if hook_name not in self._hooks: return
        logger.trace(f"触发钩子: '{hook_name}'")
        tasks = [self._execute_hook(func, *args, **kwargs) for func in self._hooks[hook_name]]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def trigger_with_results(self, hook_name: str, *args,
                                   stop_on_false: bool = False,
                                   raise_on_error: bool = False,
                                   **kwargs) -> List[HookResult]:
        """触发钩子并收集结果,可选择在返回 False 或异常时中止。"""
        if hook_name not in self._hooks:
            return []
        results: List[HookResult] = []
        for func in self._hooks[hook_name]:
            try:
                result = await self._execute_hook_with_result(func, *args, **kwargs)
                results.append(HookResult(func=func, ok=True, result=result))
                if stop_on_false and result is False:
                    break
            except Exception as e:
                logger.error(
                    "执行钩子回调函数 '%s.%s' 时发生错误: %s",
                    func.__module__,
                    func.__name__,
                    e,
                    exc_info=True,
                )
                results.append(HookResult(func=func, ok=False, error=str(e)))
                if raise_on_error:
                    raise
        return results

    async def _execute_hook(self, func: Callable, *args, **kwargs):
        """(私有) 安全地执行单个钩子回调函数。"""
        try:
            if asyncio.iscoroutinefunction(func):
                await func(*args, **kwargs)
            else:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: func(*args, **kwargs))
        except Exception as e:
            logger.error(f"执行钩子回调函数 '{func.__module__}.{func.__name__}' 时发生错误: {e}", exc_info=True)

    async def _execute_hook_with_result(self, func: Callable, *args, **kwargs) -> Any:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    def clear(self):
        """清空管理器中的所有钩子。"""
        self._hooks.clear()
        logger.info("钩子管理器已清空。")


# 全局注册表实例
ACTION_REGISTRY = ActionRegistry()
service_registry = ServiceRegistry()
hook_manager = HookManager()
