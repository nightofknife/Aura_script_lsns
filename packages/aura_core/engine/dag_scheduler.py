# -*- coding: utf-8 -*-
"""DAG scheduler module."""
import asyncio
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine


class DAGScheduler:
    """Schedule and run DAG nodes based on dependency state."""

    def __init__(self, engine: 'ExecutionEngine'):
        self.engine = engine

    async def run_dag_scheduler(self):
        """Start scheduler loop and wait for completion."""
        self.engine.completion_event = asyncio.Event()
        await self.schedule_ready_nodes()

        if not self.engine.running_tasks and self.engine.nodes:
            if all(state == self.engine.StepState.PENDING for state in self.engine.step_states.values()):
                raise ValueError("Unable to schedule any node; please check dependency configuration.")

        if self.engine.running_tasks:
            await self.engine.completion_event.wait()

    async def schedule_ready_nodes(self):
        """Find and enqueue all currently runnable nodes."""
        for node_id in self.engine.nodes:
            await self.enqueue_ready_node(node_id)
        await self.drain_ready_queue()

    async def enqueue_ready_node(self, node_id: str):
        """Enqueue a node if it is pending and dependencies are met."""
        if node_id in self.engine._ready_set:
            return
        if self.engine.step_states.get(node_id) != self.engine.StepState.PENDING:
            return
        if await self.are_dependencies_met(node_id):
            self.engine.ready_queue.append(node_id)
            self.engine._ready_set.add(node_id)

    async def drain_ready_queue(self):
        """Launch execution tasks for all queued ready nodes."""
        while self.engine.ready_queue:
            node_id = self.engine.ready_queue.popleft()
            self.engine._ready_set.discard(node_id)
            if self.engine.step_states.get(node_id) != self.engine.StepState.PENDING:
                continue

            node_context = self.engine._prepare_node_context(node_id)
            self.engine.node_contexts[node_id] = node_context

            task = asyncio.create_task(
                self.engine.node_executor.execute_dag_node(node_id, node_context)
            )
            self.engine.running_tasks.add(task)
            task.add_done_callback(
                lambda t, nid=node_id: self.engine._on_task_completed(t, nid)
            )

    async def are_dependencies_met(self, node_id: str) -> bool:
        """Return whether node dependency spec is currently satisfied."""
        dep_struct = self.engine.dependencies.get(node_id)
        return await self.evaluate_dep_struct(dep_struct)

    async def evaluate_dep_struct(self, struct: Any) -> bool:
        """Evaluate dependency spec with `all/any/none` operators."""
        if struct is None:
            return True

        if isinstance(struct, str):
            if struct.strip().startswith("when:"):
                raise ValueError(
                    "Inline dependency condition 'when:' has been removed from 'depends_on'. "
                    "Please use step-level field 'when' instead."
                )
            state = self.engine.step_states.get(struct)
            return state == self.engine.StepState.SUCCESS

        if isinstance(struct, list):
            raise ValueError(
                "List dependency shorthand has been removed from 'depends_on'. "
                "Please use '{ all: [...] }' instead."
            )

        if isinstance(struct, dict):
            if not struct:
                return True

            legacy_operators = {"and", "or", "not"}
            if legacy_operators.intersection(struct.keys()):
                raise ValueError(
                    "Dependency operators 'and/or/not' have been removed. "
                    "Please use 'all/any/none'."
                )

            logical_operators = {"all", "any", "none"}
            present_operators = logical_operators.intersection(struct.keys())
            if present_operators:
                if len(present_operators) != 1 or len(struct) != 1:
                    raise ValueError(
                        "Dependency object with logical operator must contain exactly one key "
                        "from {'all', 'any', 'none'}."
                    )

                operator = next(iter(present_operators))
                payload = struct[operator]

                if isinstance(payload, list):
                    results = await asyncio.gather(*[self.evaluate_dep_struct(item) for item in payload])
                    if operator == "all":
                        return all(results)
                    if operator == "any":
                        return any(results)
                    return not any(results)

                result = await self.evaluate_dep_struct(payload)
                if operator == "none":
                    return not result
                return result

            # Status query form: {node_id: "success|failed|..."}
            if len(struct) != 1:
                raise ValueError(
                    f"Invalid dependency condition format: {struct}. "
                    "Status query must contain exactly one key-value pair."
                )

            node_id, expected_status_str = next(iter(struct.items()))
            if not isinstance(expected_status_str, str):
                raise ValueError(
                    f"Dependency status for node '{node_id}' must be a string, got "
                    f"{type(expected_status_str).__name__}."
                )

            raw_statuses = {s.strip().lower() for s in expected_status_str.split("|")}
            invalid_statuses = raw_statuses - self.engine.VALID_DEPENDENCY_STATUSES
            if invalid_statuses:
                raise ValueError(
                    f"Unknown dependency statuses: {invalid_statuses}. "
                    f"Supported statuses: {self.engine.VALID_DEPENDENCY_STATUSES}"
                )

            current_state_enum = self.engine.step_states.get(node_id)
            if not current_state_enum:
                return False

            current_state_str = current_state_enum.name.lower()
            return current_state_str in raw_statuses

        raise ValueError(f"Unsupported dependency spec type: {type(struct).__name__}")
