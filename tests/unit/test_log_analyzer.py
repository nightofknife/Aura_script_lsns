"""Legacy and unified session logs share the existing outer text format."""
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools.log_analyzer import parse_log_file, summarize_entries, resolve_log_file
from scripts.release.validate_release import validate_logging_defaults


ROOT = Path(__file__).resolve().parents[2]


def line(message, level="INFO", cid="task-1"):
    return f"2026-10-28 20:43:59,123 - {level:<8} - [cid:{cid}] - AuraFramework - action.run:42 - {message}\n"


def test_old_and_unified_records_keep_tracebacks_and_context(tmp_path):
    path = tmp_path / "session.log"
    path.write_text(line("legacy record") + line(
        "[session:session-1 pid:13756 role:worker root:root-1 seq:23] failure", "ERROR"
    ) + "Traceback (most recent call last):\n  fixture.py:42\nValueError: failed\n", encoding="utf-8")
    entries, errors = parse_log_file(path)
    assert errors == 0 and len(entries) == 2
    assert entries[0].message == "legacy record" and entries[0].session_id is None
    entry = entries[1]
    assert (entry.session_id, entry.pid, entry.role, entry.root_cid, entry.sequence) == (
        "session-1", 13756, "worker", "root-1", 23)
    assert entry.message == "failure\nTraceback (most recent call last):\n  fixture.py:42\nValueError: failed"
    assert "[session:session-1" in entry.raw_lines[0]
    assert entry.to_dict()["sequence"] == 23


def test_context_does_not_fragment_repeated_message_summary(tmp_path):
    path = tmp_path / "aura_session_test.log"
    path.write_text("".join(line(f"[session:s pid:{i} role:worker root:r seq:{i}] same message")
                            for i in (1, 2)), encoding="utf-8")
    entries, errors = parse_log_file(path)
    summary = summarize_entries(path, entries, entries, unparsable_lines=errors, limit=10)
    assert summary["top_messages"] == [{"count": 2, "message": "same message"}]
    assert resolve_log_file(None, tmp_path) == path


def test_thread_and_process_instance_are_optional_and_preserved(tmp_path):
    path = tmp_path / "session.log"
    path.write_text(line("[session:s pid:13756 role:worker root:r seq:9 thread:3688 instance:13756-abcdef] ready")
                    + line("[session:s pid:13756 role:worker root:r seq:10] legacy context"), encoding="utf-8")
    entries, errors = parse_log_file(path)
    assert errors == 0 and len(entries) == 2
    assert entries[0].message == "ready"
    assert entries[0].thread_id == 3688
    assert entries[0].process_instance_id == "13756-abcdef"
    assert entries[0].to_dict()["thread_id"] == 3688
    assert entries[0].to_dict()["process_instance_id"] == "13756-abcdef"
    assert entries[1].thread_id is None and entries[1].process_instance_id is None


def test_incomplete_or_lookalike_context_remains_message(tmp_path):
    path = tmp_path / "session.log"
    message = "[session:partial pid:not-a-pid] original error"
    path.write_text(line(message), encoding="utf-8")
    entries, _ = parse_log_file(path)
    assert entries[0].message == message and entries[0].pid is None


def test_shipped_retention_defaults_match_supported_runtime_options():
    text = (ROOT / "packaging/templates/config.yaml").read_text(encoding="utf-8")
    validate_logging_defaults(text)
    config = yaml.safe_load(text)
    assert config["logging"] == {"log_dir": "logs"}
    config["observability"]["runs"]["retention_hours"] = 1
    with pytest.raises(ValueError, match="retention_hours"):
        validate_logging_defaults(yaml.safe_dump(config))


def test_lightweight_logging_imports_do_not_require_qt_or_runtime():
    code = '''
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'shiboken6', 'cv2', 'numpy', 'onnxruntime'}:
            raise AssertionError('Heavy import: ' + fullname)
        return None
sys.meta_path.insert(0, Guard())
from packages.aura_core.observability.logging import coordinator, transport, stream_capture
assert callable(coordinator.start_session)
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
