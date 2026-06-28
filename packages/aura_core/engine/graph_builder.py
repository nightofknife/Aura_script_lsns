# -*- coding: utf-8 -*-
"""Graph construction utilities for task DAG execution."""
from typing import Any, Dict, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine


class GraphBuilder:
    """Build dependency graph and detect cycles."""

    def __init__(self, engine: 'ExecutionEngine'):
        self.engine = engine

    def build_graph(self, steps_dict: Dict[str, Any]):
        """Build graph from task step definitions."""
        self.engine.nodes = steps_dict
        all_node_ids = set(self.engine.nodes.keys())

        for node_id, node_data in self.engine.nodes.items():
            self.engine.step_states[node_id] = self.engine.StepState.PENDING
            self.engine.reverse_dependencies.setdefault(node_id, set())

            self.engine.node_metadata[node_id] = {
                'execution_count': 0,
                'retry_count': 0,
                'first_executed_at': None,
                'last_executed_at': None,
            }

            deps_struct = node_data.get('depends_on')
            self.engine.dependencies[node_id] = deps_struct

            all_deps = self.get_all_deps_from_struct(deps_struct)
            for dep_id in all_deps:
                if dep_id not in all_node_ids:
                    raise KeyError(
                        f"Node '{node_id}' references undefined dependency '{dep_id}'"
                    )
                self.engine.reverse_dependencies.setdefault(dep_id, set()).add(node_id)

        self.detect_circular_dependencies(all_node_ids)

    def detect_circular_dependencies(self, all_nodes: Set[str]):
        """Detect circular dependencies with DFS coloring."""
        WHITE = 0
        GRAY = 1
        BLACK = 2

        colors = {node: WHITE for node in all_nodes}
        path = []

        def dfs(node: str) -> bool:
            colors[node] = GRAY
            path.append(node)

            deps = self.get_all_deps_from_struct(self.engine.dependencies.get(node))
            for dep in deps:
                if colors[dep] == GRAY:
                    cycle_start = path.index(dep)
                    cycle = path[cycle_start:] + [dep]
                    raise ValueError(
                        f"Detected circular dependency: {' -> '.join(cycle)}\n"
                        "Please check the depends_on configuration of involved nodes."
                    )
                if colors[dep] == WHITE:
                    if dfs(dep):
                        return True

            path.pop()
            colors[node] = BLACK
            return False

        for node in all_nodes:
            if colors[node] == WHITE:
                path = []
                dfs(node)

    def get_all_deps_from_struct(self, struct: Any) -> Set[str]:
        """Recursively collect node ids referenced by a dependency spec."""
        deps: Set[str] = set()

        if struct is None:
            return deps

        if isinstance(struct, str):
            if struct.strip().startswith("when:"):
                raise ValueError(
                    "Inline dependency condition 'when:' has been removed from 'depends_on'. "
                    "Please use step-level field 'when' instead."
                )
            deps.add(struct)
            return deps

        if isinstance(struct, list):
            raise ValueError(
                "List dependency shorthand has been removed from 'depends_on'. "
                "Please use '{ all: [...] }' instead."
            )

        if isinstance(struct, dict):
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
                    for item in payload:
                        deps.update(self.get_all_deps_from_struct(item))
                    return deps
                deps.update(self.get_all_deps_from_struct(payload))
                return deps

            # Status query form: {node_id: "success|failed|..."}
            deps.update(struct.keys())
            return deps

        raise ValueError(f"Unsupported dependency spec type: {type(struct).__name__}")
