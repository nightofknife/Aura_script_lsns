from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from tools.plan_doctor import REPO_ROOT, inspect_plan


class TestPlanDoctor(unittest.TestCase):
    def test_inspect_existing_plan_returns_summary(self):
        report = inspect_plan("aura_benchmark")

        self.assertEqual(report["plan_name"], "aura_benchmark")
        self.assertIn("summary", report)
        self.assertIn("errors", report["summary"])
        self.assertIn("warnings", report["summary"])
        self.assertIsInstance(report["findings"], list)
        if report["findings"]:
            self.assertIn("remediation", report["findings"][0])

    def test_detects_deprecated_task_syntax_and_missing_state_map(self):
        plan_name = "_tmp_plan_doctor_case"
        target = REPO_ROOT / "plans" / plan_name
        if target.exists():
            shutil.rmtree(target)

        try:
            (target / "tasks").mkdir(parents=True)
            (target / "src" / "actions").mkdir(parents=True)
            (target / "src" / "services").mkdir(parents=True)
            (target / "src" / "actions" / "__init__.py").write_text("", encoding="utf-8")
            (target / "src" / "services" / "__init__.py").write_text("", encoding="utf-8")
            (target / "manifest.yaml").write_text(
                "\n".join(
                    [
                        "package:",
                        f"  name: '@plans/{plan_name}'",
                        "  version: '0.1.0'",
                        "  description: ''",
                        "  license: MIT",
                        "dependencies: {}",
                        "exports:",
                        "  actions: []",
                        "  services: []",
                        "  tasks: []",
                    ]
                ),
                encoding="utf-8",
            )
            (target / "tasks" / "bad.yaml").write_text(
                "\n".join(
                    [
                        "bad_task:",
                        "  meta:",
                        "    requires_initial_state: world",
                        "  steps:",
                        "    run:",
                        "      action: run_task",
                        "      params:",
                        "        task_ref: tasks:other.yaml",
                    ]
                ),
                encoding="utf-8",
            )

            report = inspect_plan(plan_name)
            codes = {item["code"] for item in report["findings"]}

            self.assertIn("deprecated_syntax", codes)
            self.assertIn("states_map_missing", codes)
            remediation_by_code = {item["code"]: item["remediation"] for item in report["findings"]}
            self.assertIn("aura.run_task", remediation_by_code["deprecated_syntax"])
            self.assertIn("states:", remediation_by_code["states_map_missing"])
        finally:
            if target.exists():
                shutil.rmtree(target)


if __name__ == "__main__":
    unittest.main()
