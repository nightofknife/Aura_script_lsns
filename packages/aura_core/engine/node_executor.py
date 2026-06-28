# -*- coding: utf-8 -*-
"""节点执行模块

负责执行DAG中的单个节点，包括：
- 执行DAG节点（含重试机制）
- 执行单个Action
- 执行循环（for_each, times, while）
- 解析重试配置
- 处理超时配置

迁移自 engine.py:407-806
"""
import asyncio
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.engine.action_injector import ActionInjector
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.utils.exceptions import StopTaskException
from packages.aura_core.config.template import TemplateRenderer

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine, StepState


class NodeExecutor:
    """节点执行器

    负责执行DAG中的单个节点，包括重试、超时、循环等逻辑。
    """

    def __init__(self, engine: 'ExecutionEngine'):
        """初始化节点执行器

        Args:
            engine: 父级ExecutionEngine实例
        """
        self.engine = engine

    async def execute_dag_node(self, node_id: str, node_context: ExecutionContext):
        """执行DAG中的一个节点

        迁移自 engine.py:501-731

        这是最复杂的方法，负责：
        - 更新节点元数据
        - 执行重试逻辑
        - 处理超时
        - 处理输出映射
        - 触发事件回调
        - 异常处理

        Args:
            node_id: 要执行的节点ID
            node_context: 节点的执行上下文
        """
        # ===== 更新节点元数据 =====
        metadata = self.engine.node_metadata[node_id]
        metadata['execution_count'] += 1
        if metadata['first_executed_at'] is None:
            metadata['first_executed_at'] = time.time()
        metadata['last_executed_at'] = time.time()

        # 将元数据注入上下文，供模板使用
        node_context.data['nodes'].setdefault(node_id, {})
        node_context.data['nodes'][node_id]['metadata'] = metadata.copy()
        self.engine.root_context.data['nodes'].setdefault(node_id, {})
        self.engine.root_context.data['nodes'][node_id]['metadata'] = metadata.copy()

        self.engine.step_states[node_id] = self.engine.StepState.RUNNING
        start_time = time.time()
        node_result: Dict[str, Any] = {}
        error_details: Optional[Dict[str, Any]] = None
        execution_success = False
        action_result = None
        loop_info: Dict[str, Any] = {}
        try:
            loop_info = getattr(node_context, "data", {}).get("loop", {}) or {}
        except Exception:
            loop_info = {}

        def _base_payload() -> Dict[str, Any]:
            """构建事件基础载荷"""
            payload = {
                "node_id": node_id,
                "node_name": self.engine.nodes.get(node_id, {}).get("name"),
                "start_time": start_time,
                "loop_index": loop_info.get("index", 0) or 0,
            }
            if "item" in loop_info:
                try:
                    item_val = loop_info.get("item")
                    item_repr = str(item_val)
                except Exception:
                    item_repr = repr(loop_info.get("item"))
                if item_repr and len(item_repr) > 200:
                    item_repr = item_repr[:200] + "..."
                payload["loop_item"] = item_repr
            return payload

        try:
            node_data = self.engine.nodes[node_id]
            step_note, _ = await self._resolve_step_note(node_id, node_data, node_context)
            if step_note:
                logger.info("Step note | node='%s' note=%s", node_id, step_note)
            if self.engine.event_callback:
                start_payload = _base_payload()
                if step_note:
                    start_payload["step_note"] = step_note
                await self.engine.event_callback('node.started', start_payload)

            when_passed = await self._evaluate_when(node_id, node_data, node_context)
            if not when_passed:
                execution_success = True
                self.engine.step_states[node_id] = self.engine.StepState.SKIPPED
                node_result['output'] = None

                if self.engine.event_callback:
                    end_ts = time.time()
                    await self.engine.event_callback('node.skipped', {
                        **_base_payload(),
                        'end_time': end_ts,
                        'duration_ms': round((end_ts - start_time) * 1000, 3),
                        'reason': 'when_condition_is_false',
                    })
                return

            retry_cfg = self.parse_retry_config(node_data)
            max_attempts = retry_cfg["count"] + 1
            delay_sec = retry_cfg["delay"]
            retry_on = retry_cfg["on_exception"]
            cond_expr = retry_cfg["condition"]

            # ===== 记录重试次数 =====
            actual_retry_count = 0

            attempt = 1
            while attempt <= max_attempts:
                await self.engine._check_pause()
                try:
                    async def _run_action():
                        loop_config = node_data.get('loop')
                        if loop_config:
                            return await self.execute_loop(
                                node_id, node_data, node_context, loop_config
                            )
                        return await self.execute_single_action(node_data, node_context)

                    timeout_sec = self.resolve_node_timeout(node_data)
                    if timeout_sec:
                        action_result = await asyncio.wait_for(_run_action(), timeout=timeout_sec)
                    else:
                        action_result = await _run_action()

                    if cond_expr:
                        renderer = TemplateRenderer(node_context, self.engine.state_store)
                        scope = {"result": action_result, "attempt": attempt}
                        should_retry = bool(
                            await renderer._render_recursive(
                                cond_expr, {**scope, **(await renderer.get_render_scope())}
                            )
                        )
                        if should_retry:
                            if attempt < max_attempts:
                                actual_retry_count += 1
                                logger.warning(
                                    f"节点 '{node_id}' 第{attempt}/{max_attempts - 1} 次重试：不满足 retry_condition，"
                                    f"{delay_sec}s 后重试..."
                                )
                                if self.engine.event_callback:
                                    await self.engine.event_callback('node.retrying', {
                                        **_base_payload(),
                                        'retry_seq': attempt,
                                        'retry_limit': max_attempts - 1,
                                        'reason': 'condition',
                                        'delay_sec': delay_sec,
                                    })
                                await asyncio.sleep(delay_sec)
                                attempt += 1
                                continue
                            raise RuntimeError("RetryConditionNotSatisfied")

                    break

                except Exception as e:
                    if attempt < max_attempts and self.should_retry_on_exception(e, retry_on):
                        actual_retry_count += 1
                        logger.warning(
                            f"节点 '{node_id}' 第{attempt}/{max_attempts - 1} 次重试：捕获到{type(e).__name__}: {e}，"
                            f"{delay_sec}s 后重试..."
                        )
                        if self.engine.event_callback:
                            await self.engine.event_callback('node.retrying', {
                                **_base_payload(),
                                'retry_seq': attempt,
                                'retry_limit': max_attempts - 1,
                                'exception_type': type(e).__name__,
                                'exception_message': str(e),
                                'delay_sec': delay_sec,
                            })
                        await asyncio.sleep(delay_sec)
                        attempt += 1
                        continue
                    raise

            # ===== 更新 retry_count 元数据 =====
            metadata['retry_count'] = actual_retry_count
            node_context.data['nodes'][node_id]['metadata'] = metadata.copy()
            self.engine.root_context.data['nodes'][node_id]['metadata'] = metadata.copy()

            outputs_block = node_data.get('outputs', {})
            if outputs_block:
                renderer = TemplateRenderer(node_context, self.engine.state_store)
                output_render_scope = {"result": action_result, **(await renderer.get_render_scope())}
                for name, template in outputs_block.items():
                    node_result[name] = await renderer._render_recursive(template, output_render_scope)
            else:
                node_result['output'] = action_result

            self.engine.step_states[node_id] = self.engine.StepState.SUCCESS
            execution_success = True
            if self.engine.event_callback:
                end_ts = time.time()
                await self.engine.event_callback('node.succeeded', {
                    **_base_payload(),
                    'end_time': end_ts,
                    'duration_ms': round((end_ts - start_time) * 1000, 3),
                    'retry_count': actual_retry_count,
                    'output': node_result
                })


        # 统一异常处理
        except StopTaskException as e:
            execution_success = False
            logger.error(f"节点'{node_id}'遇到控制流异常: {e}", exc_info=self.engine.debug_mode)
            self.engine.step_states[node_id] = self.engine.StepState.FAILED
            error_details = {
                "type": type(e).__name__,
                "message": str(e),
                "severity": getattr(e, 'severity', 'error')
            }
            if self.engine.debug_mode:
                error_details["traceback"] = getattr(
                    e, 'get_full_traceback', lambda: traceback.format_exc()
                )()

            if self.engine.event_callback:
                end_ts = time.time()
                await self.engine.event_callback('node.failed', {
                    **_base_payload(),
                    'end_time': end_ts,
                    'duration_ms': round((end_ts - start_time) * 1000, 3),
                    'exception_type': type(e).__name__,
                    'exception_message': str(e),
                    'is_control_flow_exception': True
                })

        except Exception as e:
            execution_success = False
            logger.error(f"节点'{node_id}'执行失败: {e}", exc_info=True)
            self.engine.step_states[node_id] = self.engine.StepState.FAILED
            error_details = {"type": type(e).__name__, "message": str(e)}
            if self.engine.debug_mode:
                error_details["traceback"] = traceback.format_exc()

            if self.engine.event_callback:
                end_ts = time.time()
                await self.engine.event_callback('node.failed', {
                    **_base_payload(),
                    'end_time': end_ts,
                    'duration_ms': round((end_ts - start_time) * 1000, 3),
                    'exception_type': type(e).__name__,
                    'exception_message': str(e),
                    'is_control_flow_exception': False
                })

        finally:
            # 验证状态一致性
            if execution_success and error_details is not None:
                logger.error(
                    f"逻辑错误：节点'{node_id}'标记为成功但有错误详情。"
                    f"execution_success={execution_success}, error_details={error_details}"
                )
            if not execution_success and error_details is None:
                logger.warning(
                    f"状态不一致：节点'{node_id}'标记为失败但无错误详情。"
                )

            run_state = self.engine._create_run_state(
                self.engine.step_states.get(node_id, self.engine.StepState.FAILED),
                start_time,
                error=error_details
            )
            final_node_output = {"run_state": run_state, **node_result}
            node_context.add_node_result(node_id, final_node_output)
            self.engine.root_context.add_node_result(node_id, final_node_output)

            if self.engine.event_callback:
                final_state = self.engine.step_states.get(node_id, self.engine.StepState.FAILED)
                if final_state == self.engine.StepState.SKIPPED:
                    status = 'skipped'
                elif error_details:
                    status = 'failed'
                else:
                    status = 'success'
                end_ts = time.time()
                await self.engine.event_callback('node.finished', {
                    **_base_payload(),
                    'end_time': end_ts,
                    'duration_ms': round((end_ts - start_time) * 1000, 3),
                    'status': status
                })

    async def _resolve_step_note(
        self,
        node_id: str,
        node_data: Dict[str, Any],
        node_context: ExecutionContext,
    ) -> Tuple[Optional[str], Any]:
        """Render step_note with a restricted scope and return rendered params snapshot."""
        renderer = TemplateRenderer(node_context, self.engine.state_store)
        raw_params = node_data.get("params", {})

        try:
            render_scope = await renderer.get_render_scope()
            rendered_params = await renderer.render(raw_params, scope=render_scope)
        except Exception as exc:
            logger.warning("Failed to render params for step_note on node '%s': %s", node_id, exc)
            rendered_params = raw_params

        note_template = node_data.get("step_note")
        if note_template is None:
            return None, rendered_params
        if not isinstance(note_template, str):
            raise TypeError(f"Node '{node_id}' step_note must be a string.")

        note_scope = {
            "inputs": node_context.data.get("inputs", {}),
            "loop": node_context.data.get("loop", {}),
            "params": rendered_params,
        }
        try:
            rendered_note = await renderer.render(note_template, scope=note_scope)
            if rendered_note is None:
                return None, rendered_params
            return str(rendered_note), rendered_params
        except Exception as exc:
            logger.warning("Failed to render step_note on node '%s': %s", node_id, exc)
            return note_template, rendered_params

    async def _evaluate_when(
        self,
        node_id: str,
        node_data: Dict[str, Any],
        node_context: ExecutionContext,
    ) -> bool:
        """Evaluate optional step-level `when` condition."""
        when_expr = node_data.get("when")
        if when_expr is None:
            return True
        if not isinstance(when_expr, str):
            raise TypeError(f"Node '{node_id}' when must be a string.")

        renderer = TemplateRenderer(node_context, self.engine.state_store)
        scope = await renderer.get_render_scope()
        rendered = await renderer.render(when_expr, scope=scope)

        if rendered is None and ("{{" in when_expr or "{%" in when_expr):
            raise RuntimeError(
                f"Failed to evaluate when expression for node '{node_id}': {when_expr}"
            )

        # Template renderer returns the original string when render fails.
        if (
            isinstance(rendered, str)
            and rendered == when_expr
            and ("{{" in when_expr or "{%" in when_expr)
        ):
            raise RuntimeError(
                f"Failed to evaluate when expression for node '{node_id}': {when_expr}"
            )

        return self._coerce_to_bool(rendered)

    @staticmethod
    def _coerce_to_bool(value: Any) -> bool:
        """Normalize templated values to boolean for `when` evaluation."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "0", "false", "no", "off", "none", "null"}:
                return False
            if normalized in {"1", "true", "yes", "on"}:
                return True
        return bool(value)

    async def execute_single_action(
        self, node_data: Dict, node_context: ExecutionContext
    ) -> Any:
        """执行单个action

        迁移自 engine.py:732-745

        Args:
            node_data: 节点配置数据
            node_context: 节点执行上下文

        Returns:
            action执行结果

        Raises:
            ValueError: 当节点缺少action字段时
        """
        renderer = TemplateRenderer(node_context, self.engine.state_store)
        current_package = getattr(self.engine.orchestrator, "loaded_package", None)
        injector = ActionInjector(
            node_context,
            self.engine,
            renderer,
            self.engine.services,
            current_package=current_package,
            service_resolver=self.engine.orchestrator.resolve_service,
        )

        action_name = node_data.get('action')
        if not action_name:
            raise ValueError("节点缺少 action")

        raw_params = node_data.get('params', {})

        # 委托给 ActionInjector.execute
        return await injector.execute(action_name, raw_params)

    async def execute_loop(
        self,
        node_id: str,
        node_data: Dict,
        node_context: ExecutionContext,
        loop_config: Dict
    ) -> List[Any]:
        """执行循环节点

        迁移自 engine.py:746-806

        支持的循环类型：
        - for_each: 遍历列表或字典
        - times: 重复执行N次
        - while: 条件循环

        Args:
            node_id: 节点ID
            node_data: 节点配置数据
            node_context: 节点执行上下文
            loop_config: 循环配置

        Returns:
            所有迭代的结果列表

        Raises:
            TypeError: 当loop配置格式错误时
            ValueError: 当loop类型不支持时
        """
        renderer = TemplateRenderer(node_context, self.engine.state_store)
        rendered_config = await renderer.render(loop_config)

        tasks: List = []

        if 'for_each' in rendered_config:
            items = rendered_config['for_each']
            if not isinstance(items, (list, dict)):
                raise TypeError(f"loop.for_each 必须是列表或字典，收到: {type(items)}")

            item_source = items.items() if isinstance(items, dict) else enumerate(items)
            for index, item in item_source:
                iter_context = node_context.fork()
                iter_context.set_loop_variables({'item': item, 'index': index})
                tasks.append(self.execute_single_action(node_data, iter_context))

        elif 'times' in rendered_config:
            try:
                count = int(rendered_config['times'])
            except (ValueError, TypeError):
                raise TypeError(f"loop.times 必须是整数，收到: {rendered_config['times']}")

            for i in range(count):
                iter_context = node_context.fork()
                iter_context.set_loop_variables({'index': i})
                tasks.append(self.execute_single_action(node_data, iter_context))

        elif 'while' in loop_config:
            results = []
            index = 0
            max_iterations = rendered_config.get('max_iterations', 1000)
            while index < max_iterations:
                iter_context = node_context.fork()
                iter_context.set_loop_variables({'index': index})

                while_renderer = TemplateRenderer(iter_context, self.engine.state_store)
                condition = bool(await while_renderer.render(loop_config['while']))

                if not condition:
                    break

                result = await self.execute_single_action(node_data, iter_context)
                results.append(result)
                index += 1
            return results

        else:
            raise ValueError(f"节点 '{node_id}' 的 loop 格式错误: {loop_config}")

        parallelism = rendered_config.get('parallelism', len(tasks))
        semaphore = asyncio.Semaphore(parallelism)

        async def run_with_semaphore(task) -> Any:
            async with semaphore:
                return await task

        results = await asyncio.gather(*[run_with_semaphore(task) for task in tasks])
        return results

    def parse_retry_config(self, node_data: Dict[str, Any]) -> Dict[str, Any]:
        """解析节点的重试配置，支持旧格式和新格式

        迁移自 engine.py:407-481

        旧格式（兼容）：
            retry: 3
            retry_delay: 1
            retry_on: ["TimeoutError"]
            retry_condition: "{{ result.status >= 500 }}"

        新格式（推荐）：
            on_exception:
                retry: 3
                retry_on: ["TimeoutError"]
                delay: 1
            on_result:
                retry_when: "{{ result.status >= 500 }}"
                max_retries: 5
                delay: 2

        Args:
            node_data: 节点配置数据

        Returns:
            规范化的重试配置字典，包含：
            - count: 最大重试次数
            - delay: 重试延迟（秒）
            - on_exception: 要重试的异常类型列表
            - condition: 重试条件表达式
        """
        # 默认值
        config = {
            "count": 0,
            "delay": 1.0,
            "on_exception": [],
            "condition": None
        }

        # 新格式：on_exception
        if 'on_exception' in node_data:
            exc_cfg = node_data['on_exception']
            if isinstance(exc_cfg, dict):
                config["count"] = int(exc_cfg.get('retry', exc_cfg.get('max_retries', 0)))
                config["delay"] = float(exc_cfg.get('delay', 1.0))
                exc_list = exc_cfg.get('retry_on', exc_cfg.get('on', []))
                if isinstance(exc_list, str):
                    exc_list = [exc_list]
                config["on_exception"] = exc_list

        # 新格式：on_result
        if 'on_result' in node_data:
            result_cfg = node_data['on_result']
            if isinstance(result_cfg, dict):
                result_retries = int(result_cfg.get('max_retries', result_cfg.get('retry', 0)))
                # 取两者中的最大值
                config["count"] = max(config["count"], result_retries)
                config["delay"] = float(result_cfg.get('delay', config["delay"]))
                config["condition"] = result_cfg.get('retry_when', result_cfg.get('when'))

        # 旧格式：兼容性支持
        if config["count"] == 0:  # 如果新格式没有设置，尝试旧格式
            default_delay = float(node_data.get("retry_delay", 1) or 1)
            retry_cfg = node_data.get("retry", {})

            if isinstance(retry_cfg, int):
                config["count"] = max(0, retry_cfg)
            elif isinstance(retry_cfg, dict):
                config["count"] = int(retry_cfg.get("count", retry_cfg.get("retry", 0) or 0))
                config["delay"] = float(
                    retry_cfg.get("delay", retry_cfg.get("retry_delay", default_delay) or default_delay)
                )
                on_exception = retry_cfg.get("on_exception") or retry_cfg.get("retry_on") or []
                config["condition"] = retry_cfg.get("condition") or retry_cfg.get("retry_condition") or config["condition"]
            else:
                try:
                    config["count"] = int(retry_cfg)
                except Exception:
                    config["count"] = 0

            config["delay"] = float(node_data.get("retry_delay", config["delay"]) or config["delay"])
            on_exception = node_data.get("retry_on", config["on_exception"]) or []
            if isinstance(on_exception, str):
                on_exception = [on_exception]
            config["on_exception"] = on_exception
            config["condition"] = node_data.get("retry_condition") or config["condition"]

        return config

    def should_retry_on_exception(self, exc: Exception, retry_on: List[str]) -> bool:
        """判断是否应该对指定异常进行重试

        迁移自 engine.py:482-488

        Args:
            exc: 捕获的异常
            retry_on: 要重试的异常类型列表（可以是类名或完整路径）

        Returns:
            True表示应该重试
        """
        if not retry_on:
            return False
        exc_name = type(exc).__name__
        exc_full = f"{type(exc).__module__}.{exc_name}"
        return any(item == exc_name or item == exc_full for item in retry_on)

    def resolve_node_timeout(self, node_data: Dict[str, Any]) -> Optional[float]:
        """解析节点的超时配置

        迁移自 engine.py:489-500

        支持以下配置键：
        - timeout_sec
        - timeout
        - 默认使用 engine.default_node_timeout

        Args:
            node_data: 节点配置数据

        Returns:
            超时秒数（>0），如果不需要超时则返回None
        """
        timeout = node_data.get("timeout_sec")
        if timeout is None:
            timeout = node_data.get("timeout")
        if timeout is None or timeout == 0:
            timeout = self.engine.default_node_timeout
        try:
            timeout_val = float(timeout) if timeout is not None else 0
        except (TypeError, ValueError):
            return None
        return timeout_val if timeout_val > 0 else None
