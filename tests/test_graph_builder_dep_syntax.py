from __future__ import annotations

import unittest
from enum import Enum

from packages.aura_core.engine.graph_builder import GraphBuilder


class _StepState(Enum):
    PENDING = "PENDING"


class _DummyEngine:
    StepState = _StepState

    def __init__(self):
        self.nodes = {}
        self.dependencies = {}
        self.reverse_dependencies = {}
        self.step_states = {}
        self.node_metadata = {}


class TestGraphBuilderDependencySyntax(unittest.TestCase):
    def test_collects_deps_from_canonical_logical_object_with_list_payload(self):
        builder = GraphBuilder(_DummyEngine())

        deps = builder.get_all_deps_from_struct(
            {
                "all": [
                    "prepare",
                    {"assert_ready": "success"},
                ]
            }
        )

        self.assertEqual(deps, {"prepare", "assert_ready"})

    def test_rejects_removed_list_shorthand(self):
        builder = GraphBuilder(_DummyEngine())

        with self.assertRaises(ValueError) as cm:
            builder.get_all_deps_from_struct(["a", "b"])

        self.assertIn("List dependency shorthand has been removed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
