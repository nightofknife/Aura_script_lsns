"""Entry-point and worker lifecycle checks without starting a game runtime."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from packages.aura_core.observability.logging import coordinator, stream_capture


ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_logging(monkeypatch):
    coordinator.close_session()
    monkeypatch.delenv("AURA_LOG_DESCRIPTOR", raising=False)
    monkeypatch.delenv("AURA_LOG_SESSION_ID", raising=False)
    monkeypatch.delenv("AURA_LOG_ROOT", raising=False)
    # Native stream capture has its own subprocess tests; keep pytest's own
    # capture handles out of launcher/update unit checks.
    monkeypatch.setattr(stream_capture, "install_capture", lambda **kwargs: None)
    monkeypatch.setattr(stream_capture, "uninstall_capture", lambda: None)
    yield
    coordinator.close_session()


def test_spawned_workers_and_host_shutdown_share_one_file(tmp_path):
    script = tmp_path / "worker_logging_check.py"
    log_root = tmp_path / "logs"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
from packages.aura_game.runner import SubprocessGameRunner
from packages.aura_core.observability.logging.coordinator import start_session, close_session
from packages.aura_core.observability.logging.core_logger import logger

def main():
    start_session({str(log_root)!r}, descriptor={{}}, role="test_host")
    logger.info("HOST_BEFORE_WORKERS")
    for index in range(2):
        runner = SubprocessGameRunner()
        try:
            assert runner.status()["scheduler_initialized"] is False
            try:
                runner._request("missing_test_operation")
            except RuntimeError:
                pass
            else:
                raise AssertionError("Missing worker operation unexpectedly succeeded")
        finally:
            runner.close()
    logger.info("HOST_AFTER_WORKERS")
    close_session()

if __name__ == "__main__":
    main()
''', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in {"AURA_LOG_DESCRIPTOR", "AURA_LOG_SESSION_ID"}}
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path),
               AURA_LOG_ROOT=str(log_root), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(script)], cwd=ROOT, env=env,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    paths = list(log_root.glob("*.log"))
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "HOST_BEFORE_WORKERS" in text and "HOST_AFTER_WORKERS" in text
    assert text.count("[WorkerLifecycle] phase=started") == 2
    assert text.count("[WorkerLifecycle] phase=exited") == 2
    assert text.count("[WorkerRequest] phase=failed op=missing_test_operation") == 2
    assert "AttributeError" in text
    assert "role=worker" in text
    assert text.index("HOST_AFTER_WORKERS") > text.rindex("[WorkerLifecycle] phase=exited")


def test_launcher_missing_runtime_records_startup_failure(tmp_path, monkeypatch, isolated_logging):
    launcher = _load("unified_launcher_under_test", "packaging/launcher/aura_resonance_launcher.py")
    monkeypatch.setattr(launcher, "_release_root", lambda: tmp_path)
    monkeypatch.setenv("AURA_LOG_ROOT", str(tmp_path / "logs"))
    messages = []
    monkeypatch.setattr(launcher, "_show_error", messages.append)
    assert launcher.main() == 2
    paths = list((tmp_path / "logs").glob("*.log"))
    assert len(paths) == 1
    assert "GUI runtime executable was not found" in paths[0].read_text(encoding="utf-8")
    assert len(messages) == 1


def test_launcher_self_check_verifies_log_without_starting_runtime(tmp_path, monkeypatch, isolated_logging):
    launcher = _load("unified_launcher_self_check", "packaging/launcher/aura_resonance_launcher.py")
    runtime = tmp_path / "runtime/AuraResonanceRuntime.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"synthetic runtime")
    monkeypatch.setattr(launcher, "_release_root", lambda: tmp_path)
    monkeypatch.setenv("AURA_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setattr(launcher.sys, "argv", ["launcher", "--self-check"])
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("self-check must not launch GUI"))
    assert launcher.main() == 0
    paths = list((tmp_path / "logs").glob("*.log"))
    assert len(paths) == 1
    assert "[Launcher] phase=self_check" in paths[0].read_text(encoding="utf-8")


def test_launcher_runtime_self_check_forwards_only_safe_flag_and_session(tmp_path, monkeypatch, isolated_logging):
    launcher = _load("unified_launcher_runtime_self_check", "packaging/launcher/aura_resonance_launcher.py")
    runtime = tmp_path / "runtime/AuraResonanceRuntime.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"synthetic runtime")
    monkeypatch.setattr(launcher, "_release_root", lambda: tmp_path)
    monkeypatch.setenv("AURA_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(launcher.sys, "argv", ["launcher", "--runtime-self-check", "--ignored-extra"])
    calls = []
    class Process:
        pid = 4321
        stderr = io.BytesIO(b"runtime bootstrap diagnostic\n")
        def wait(self):
            return 0
    def popen(args, **kwargs):
        calls.append((args, kwargs))
        assert args == [str(runtime), "--self-check"]
        assert kwargs["env"]["AURA_BASE_PATH"] == str(tmp_path)
        assert kwargs["env"]["QT_QPA_PLATFORM"] == "offscreen"
        assert json.loads(kwargs["env"]["AURA_LOG_DESCRIPTOR"]) == coordinator.get_session_descriptor()
        assert kwargs["stderr"] == subprocess.PIPE
        return Process()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert launcher.main() == 0
    assert len(calls) == 1
    paths = list((tmp_path / "logs").glob("*.log"))
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "runtime bootstrap diagnostic" in text
    assert "[Launcher] phase=runtime_exited pid=4321 exitcode=0" in text
    assert coordinator.get_session_descriptor() is None


@pytest.mark.parametrize("entry", ["launcher", "updater"])
@pytest.mark.parametrize("environment_override", [False, True])
def test_entrypoints_respect_framework_log_directory(tmp_path, monkeypatch, isolated_logging, entry, environment_override):
    (tmp_path / "config.yaml").write_text("logging:\n  log_dir: custom-logs\n", encoding="utf-8")
    expected = tmp_path / ("environment-logs" if environment_override else "custom-logs")
    if environment_override:
        monkeypatch.setenv("AURA_LOG_ROOT", str(expected))
    if entry == "launcher":
        launcher = _load("unified_launcher_config", "packaging/launcher/aura_resonance_launcher.py")
        monkeypatch.setattr(launcher, "_release_root", lambda: tmp_path)
        monkeypatch.setattr(launcher, "_show_error", lambda message: None)
        assert launcher.main() == 2
    else:
        updater = _load("unified_updater_config", "updater/aura_updater.py")
        path = updater._setup_log(tmp_path)
        assert path.parent == expected
        coordinator.close_session()
    assert len(list(expected.glob("*.log"))) == 1
    assert not (tmp_path / "logs").exists()


def test_cli_preserves_json_stdout_and_closes_session(tmp_path, monkeypatch, isolated_logging):
    from click.testing import CliRunner
    import cli
    monkeypatch.setenv("AURA_BASE_PATH", str(tmp_path))
    monkeypatch.setenv("AURA_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "ensure_admin_startup", lambda *args: None)
    capture_options = []
    monkeypatch.setattr(stream_capture, "install_capture", lambda **kwargs: capture_options.append(kwargs))
    class Runner:
        def doctor(self, **kwargs):
            return {"ok": True, "message": "offline check"}
        def close(self):
            pass
    monkeypatch.setattr(cli, "_make_runner", lambda mode: Runner())
    result = CliRunner().invoke(cli.aura, ["doctor"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True, "message": "offline check"}
    assert capture_options[0]["capture_stdout"] is False
    assert coordinator.get_session_descriptor() is None


def test_cli_fresh_process_bootstrap_never_writes_diagnostics_to_stdout(tmp_path):
    script = tmp_path / "fresh_cli.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import cli
cli.ensure_admin_startup = lambda *args: None
class Runner:
    def doctor(self, **kwargs): return {{"ok": True}}
    def close(self): pass
cli._make_runner = lambda mode: Runner()
cli.aura(args=["doctor"])
''', encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in {"AURA_LOG_DESCRIPTOR", "AURA_LOG_SESSION_ID"}}
    env.update(AURA_BASE_PATH=str(tmp_path), AURA_LOG_ROOT=str(tmp_path / "logs"),
               TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path))
    result = subprocess.run([sys.executable, str(script)], cwd=ROOT, env=env,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}
    assert "[LogSession] started" in next((tmp_path / 'logs').glob('*.log')).read_text(encoding='utf-8')


def test_cli_records_exception_before_closing_log(tmp_path, monkeypatch, isolated_logging):
    from click.testing import CliRunner
    import cli
    monkeypatch.setenv("AURA_BASE_PATH", str(tmp_path))
    monkeypatch.setenv("AURA_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "ensure_admin_startup", lambda *args: None)
    class Runner:
        def doctor(self, **kwargs):
            raise RuntimeError("synthetic CLI failure")
        def close(self):
            pass
    monkeypatch.setattr(cli, "_make_runner", lambda mode: Runner())
    result = CliRunner().invoke(cli.aura, ["doctor"])
    assert result.exit_code != 0
    text = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "synthetic CLI failure" in text and "Traceback" in text
    assert coordinator.get_session_descriptor() is None


@pytest.mark.parametrize("spawn_fails", [False, True])
def test_updater_helper_handoff_reuses_log_without_holding_starter(tmp_path, monkeypatch, isolated_logging, spawn_fails):
    updater = _load("unified_updater_under_test", "updater/aura_updater.py")
    executable = tmp_path / "starter.exe"
    executable.write_bytes(b"synthetic updater executable")
    monkeypatch.setattr(updater.sys, "executable", str(executable))
    path = updater._setup_log(tmp_path)
    updater.LOG.info("UPDATER_STARTER_RECORD")
    captured = {}

    def popen(args, **kwargs):
        assert coordinator.get_session_descriptor() is None
        captured.update(json.loads(kwargs["env"]["AURA_LOG_DESCRIPTOR"]))
        assert captured["resume"] and "transport" not in captured
        if spawn_fails:
            raise OSError("synthetic spawn failure")
        return object()

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    if spawn_fails:
        with pytest.raises(OSError, match="synthetic spawn failure"):
            updater._launch_helper(tmp_path)
        assert coordinator.get_current_log_path() == str(path)
        updater.LOG.error("UPDATER_SPAWN_FAILED")
    else:
        updater._launch_helper(tmp_path)
        coordinator.start_session(tmp_path / "logs", descriptor=captured, cleanup=False, role="updater_helper")
        assert coordinator.get_current_log_path() == str(path)
        updater.LOG.info("UPDATER_HELPER_RECORD")
    coordinator.close_session()
    assert list((tmp_path / "logs").glob("*.log")) == [path]
    text = path.read_text(encoding="utf-8")
    assert "UPDATER_STARTER_RECORD" in text and "phase=helper_staged" in text
    assert ("UPDATER_SPAWN_FAILED" if spawn_fails else "UPDATER_HELPER_RECORD") in text


def test_updater_failure_before_handoff_is_logged_and_session_closed(tmp_path, monkeypatch, isolated_logging):
    updater = _load("unified_updater_stage_failure", "updater/aura_updater.py")
    monkeypatch.setattr(updater, "application_root", lambda: tmp_path)
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "_wait_for_enter_after_failure", lambda: None)
    def fail_copy(*args, **kwargs):
        raise OSError("synthetic helper staging failure")
    monkeypatch.setattr(updater.shutil, "copy2", fail_copy)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn after failed staging"))
    assert updater.main([]) == 1
    assert coordinator.get_session_descriptor() is None
    paths = list((tmp_path / "logs").glob("*.log"))
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "phase=failed" in text and "synthetic helper staging failure" in text


def test_updater_status_remains_visible_and_is_logged_once(tmp_path, monkeypatch, isolated_logging, capsys):
    updater = _load("unified_updater_console", "updater/aura_updater.py")
    capture_options = []
    monkeypatch.setattr(stream_capture, "install_capture", lambda **kwargs: capture_options.append(kwargs))
    path = updater._setup_log(tmp_path)
    updater._write_console_line("更新器进度测试", "updater status test")
    coordinator.close_session()
    assert capture_options == [{"native_stderr": True, "capture_stdout": False}]
    assert "更新器进度测试" in capsys.readouterr().out
    text = path.read_text(encoding="utf-8")
    assert text.count("[UpdaterStatus] 更新器进度测试") == 1


def test_updater_self_check_exercises_one_unified_log_when_requested(tmp_path, monkeypatch, isolated_logging):
    updater = _load("unified_updater_self_check", "updater/aura_updater.py")
    monkeypatch.setenv("AURA_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setattr(updater, "application_root", lambda: tmp_path)
    monkeypatch.setattr(updater, "perform_update", lambda *a, **kw: pytest.fail("self-check cannot update"))
    monkeypatch.setattr(updater, "_launch_helper", lambda *a, **kw: pytest.fail("self-check cannot launch helper"))
    assert updater.main(["--self-check"]) == 0
    paths = list((tmp_path / "logs").glob("*.log"))
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert text.count("[Updater] phase=self_check pid=") == 1
    assert "role:updater" in text or "role=updater" in text
    assert coordinator.get_session_descriptor() is None


def test_updater_self_check_without_log_root_remains_pure(monkeypatch, isolated_logging):
    updater = _load("unified_updater_pure_self_check", "updater/aura_updater.py")
    monkeypatch.setattr(updater, "_setup_log", lambda *a, **kw: pytest.fail("no explicit self-check log directory"))
    assert updater.main(["--self-check"]) == 0
    assert coordinator.get_session_descriptor() is None


def test_gui_status_is_delivered_once_after_commit_and_filtered_by_bridge(tmp_path, isolated_logging):
    from PySide6.QtCore import Qt
    from packages.resonance_gui.bridge import RunnerBridge
    from packages.aura_core.observability.logging.core_logger import logger
    descriptor = coordinator.start_session(tmp_path / "logs", descriptor={}, role="test_gui")
    path = Path(descriptor["log_path"])
    first, second = RunnerBridge(), RunnerBridge()
    first_seen, second_seen = [], []
    first_ready, second_ready = threading.Event(), threading.Event()
    def observe_first(message):
        first_seen.append((message, path.read_text(encoding="utf-8")))
        first_ready.set()
    def observe_second(message):
        second_seen.append((message, path.read_text(encoding="utf-8")))
        second_ready.set()
    first.logMessage.connect(observe_first, Qt.ConnectionType.DirectConnection)
    second.logMessage.connect(observe_second, Qt.ConnectionType.DirectConnection)
    try:
        first._log_message("FIRST_GUI_STATUS")
        logger.debug("UNRELATED_DEBUG_RECORD")
        second._log_message("SECOND_GUI_STATUS")
        assert first_ready.wait(5) and second_ready.wait(5)
        assert [row[0] for row in first_seen] == ["FIRST_GUI_STATUS"]
        assert [row[0] for row in second_seen] == ["SECOND_GUI_STATUS"]
        assert "[GuiStatus] FIRST_GUI_STATUS" in first_seen[0][1]
        assert "[GuiStatus] SECOND_GUI_STATUS" in second_seen[0][1]
        first.close()
        assert first._log_unsubscribe is None
        first._log_message("CLOSED_BRIDGE_DIRECT_STATUS")
        second_ready.clear()
        logger.info("[GuiStatus] CLOSED_BRIDGE_STATUS", extra={
            "gui_display_id": first._log_display_id,
            "gui_display_message": "CLOSED_BRIDGE_STATUS",
        })
        second._log_message("SECOND_GUI_STILL_ACTIVE")
        assert second_ready.wait(5)
        assert [row[0] for row in first_seen] == ["FIRST_GUI_STATUS"]
        assert [row[0] for row in second_seen] == ["SECOND_GUI_STATUS", "SECOND_GUI_STILL_ACTIVE"]
    finally:
        first.close()
        second.close()
    assert coordinator.flush()
    assert path.read_text(encoding="utf-8").count("[GuiStatus] FIRST_GUI_STATUS") == 1


def test_offline_gui_without_session_emits_status_once(isolated_logging):
    from packages.resonance_gui.bridge import RunnerBridge
    bridge = RunnerBridge()
    seen = []
    bridge.logMessage.connect(seen.append)
    try:
        bridge._log_message("OFFLINE_GUI_STATUS")
        assert seen == ["OFFLINE_GUI_STATUS"]
    finally:
        bridge.close()


def test_workflow_status_displays_after_commit_and_unsubscribes_on_destruction(tmp_path, monkeypatch, isolated_logging):
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
    from PySide6.QtWidgets import QApplication
    from packages.resonance_gui.config_repository import ResonanceConfigRepository
    from packages.resonance_gui.widgets import workflow_page
    app = QApplication.instance() or QApplication([])
    descriptor = coordinator.start_session(tmp_path / "logs", descriptor={}, role="test_workflow")
    path = Path(descriptor["log_path"])
    released = []
    original_subscribe = workflow_page.subscribe
    def subscribe(callback):
        unsubscribe = original_subscribe(callback)
        def release():
            released.append(True)
            unsubscribe()
        return release
    monkeypatch.setattr(workflow_page, "subscribe", subscribe)
    settings = ResonanceConfigRepository(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    page = workflow_page.WorkflowPage(settings)
    committed = threading.Event()
    observed = []
    def on_committed(message):
        observed.append((message, path.read_text(encoding="utf-8")))
        committed.set()
    page._committedLog.connect(on_committed, Qt.ConnectionType.DirectConnection)
    try:
        page.append_log("WORKFLOW_COMMITTED_STATUS")
        assert "WORKFLOW_COMMITTED_STATUS" not in page.log_view.toPlainText()
        assert committed.wait(5)
        assert observed[0][0] == "WORKFLOW_COMMITTED_STATUS"
        assert "[WorkflowStatus] WORKFLOW_COMMITTED_STATUS" in observed[0][1]
        app.processEvents()
        assert page.log_view.toPlainText().count("WORKFLOW_COMMITTED_STATUS") == 1
        page.append_log("BRIDGE_ALREADY_COMMITTED", persist=False)
        assert "BRIDGE_ALREADY_COMMITTED" in page.log_view.toPlainText()
        assert coordinator.flush()
        text = path.read_text(encoding="utf-8")
        assert text.count("[WorkflowStatus] WORKFLOW_COMMITTED_STATUS") == 1
        assert "BRIDGE_ALREADY_COMMITTED" not in text
    finally:
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    assert released == [True]


def test_workflow_without_session_keeps_synchronous_status(tmp_path, isolated_logging):
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings
    from PySide6.QtWidgets import QApplication
    from packages.resonance_gui.config_repository import ResonanceConfigRepository
    from packages.resonance_gui.widgets.workflow_page import WorkflowPage
    app = QApplication.instance() or QApplication([])
    settings = ResonanceConfigRepository(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    page = WorkflowPage(settings)
    try:
        page.append_log("OFFLINE_WORKFLOW_STATUS")
        assert page.log_view.toPlainText().count("OFFLINE_WORKFLOW_STATUS") == 1
        assert page._log_unsubscribe is None
    finally:
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
