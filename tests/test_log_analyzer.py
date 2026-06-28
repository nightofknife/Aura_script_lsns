from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.log_analyzer import filter_entries, parse_log_file, run_cli, summarize_entries


class TestLogAnalyzer(unittest.TestCase):
    def _write_log(self, root: Path) -> Path:
        log_dir = root / "logs"
        log_dir.mkdir()
        log_path = log_dir / "aura_session_20260422-010203.log"
        log_path.write_text(
            "\n".join(
                [
                    "2026-04-22 01:02:03,100 - INFO     - [cid:-] - AuraFramework - loader.info:10 - Boot start",
                    "2026-04-22 01:02:03,200 - WARNING  - [cid:run-1] - AuraFramework - scheduler.warn:11 - Slow node detected",
                    "2026-04-22 01:02:03,250 - ERROR    - [cid:run-1] - AuraFramework - scheduler.error:12 - Task failed",
                    "Traceback line 1",
                    "Traceback line 2",
                    "2026-04-22 01:02:03,300 - INFO     - [cid:run-2] - AuraFramework - worker.info:13 - Completed",
                ]
            ),
            encoding="utf-8",
        )
        return log_path

    def test_parse_and_filter_log_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_path = self._write_log(root)

            entries, unparsable_lines = parse_log_file(log_path)

            self.assertEqual(len(entries), 4)
            self.assertEqual(unparsable_lines, 0)
            self.assertIn("Traceback line 1", entries[2].message)

            filtered = filter_entries(entries, levels={"ERROR"}, cid="run-1", keyword="failed")
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0].level, "ERROR")

            summary = summarize_entries(
                log_path,
                entries,
                filtered,
                unparsable_lines=unparsable_lines,
                limit=10,
            )
            self.assertEqual(summary["matched_entries"], 1)
            self.assertEqual(summary["likely_issue_count"], 1)

    def test_cli_json_output_uses_latest_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_log(root)

            stdout_capture = io.StringIO()
            with redirect_stdout(stdout_capture):
                exit_code = run_cli(["--logs-dir", str(root / "logs"), "--json", "--limit", "5"])

            payload = json.loads(stdout_capture.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["summary"]["total_entries"], 4)
            self.assertEqual(payload["summary"]["counts_by_level"]["ERROR"], 1)


if __name__ == "__main__":
    unittest.main()
