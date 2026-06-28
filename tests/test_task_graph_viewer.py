from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.task_graph_viewer import analyze_loaded_task, load_task_from_path, run_cli


class TestTaskGraphViewer(unittest.TestCase):
    def _write_task_file(self, root: Path) -> Path:
        task_path = root / "combat_loop.yaml"
        task_path.write_text(
            "\n".join(
                [
                    "combat_loop:",
                    "  meta:",
                    "    title: Combat loop",
                    "    description: Demo task graph",
                    "  steps:",
                    "    prepare:",
                    "      action: aura.click",
                    "    attack:",
                    "      action: aura.run_task",
                    "      depends_on: prepare",
                    "      params:",
                    "        task_ref: tasks:combat:attack.yaml",
                    "    finish:",
                    "      action: aura.wait",
                    "      depends_on:",
                    "        all:",
                    "          - attack",
                    "      when: \"{{ true }}\"",
                ]
            ),
            encoding="utf-8",
        )
        return task_path

    def test_analyze_loaded_task_from_direct_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_path = self._write_task_file(Path(tmpdir))

            loaded = load_task_from_path(task_path, task_key="combat_loop")
            report = analyze_loaded_task(loaded)

            self.assertTrue(report["summary"]["graph_valid"])
            self.assertEqual(report["summary"]["step_count"], 3)
            self.assertEqual(report["summary"]["root_steps"], ["prepare"])
            self.assertEqual(report["summary"]["leaf_steps"], ["finish"])
            self.assertIn("tasks:combat:attack.yaml", report["summary"]["subtask_calls"])
            self.assertIn("flowchart TD", report["mermaid"])

    def test_cli_json_output_for_direct_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_path = self._write_task_file(Path(tmpdir))

            stdout_capture = io.StringIO()
            with redirect_stdout(stdout_capture):
                exit_code = run_cli(
                    ["--path", str(task_path), "--task-key", "combat_loop", "--format", "json"]
                )

            payload = json.loads(stdout_capture.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["task"]["task_key"], "combat_loop")
            self.assertEqual(payload["summary"]["edge_count"], 2)


if __name__ == "__main__":
    unittest.main()
