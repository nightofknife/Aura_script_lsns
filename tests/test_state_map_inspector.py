from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tools.state_map_inspector import inspect_state_map


class TestStateMapInspector(unittest.TestCase):
    def test_inspects_direct_state_map_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "states_map.yaml"
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(
                    {
                        "states": {
                            "idle": {"check_task": "tasks:checks:idle.yaml"},
                            "ready": {"check_task": "tasks:checks:ready.yaml"},
                        },
                        "transitions": [
                            {
                                "from": "idle",
                                "to": "ready",
                                "cost": 1,
                                "transition_task": "tasks:transitions:to_ready.yaml",
                            }
                        ],
                    },
                    handle,
                    sort_keys=False,
                )

            report = inspect_state_map(plan_name=None, state_map_path=path)

            self.assertEqual(report["summary"]["state_count"], 2)
            self.assertEqual(report["summary"]["transition_count"], 1)
            self.assertIn("flowchart TD", report["mermaid"])
            self.assertEqual(report["summary"]["findings"]["errors"], 0)


if __name__ == "__main__":
    unittest.main()
