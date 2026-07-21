from __future__ import annotations

import multiprocessing
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packages.aura_core.scheduler.cancellation import clear_task_cancel, is_task_cancel_requested
from packages.aura_core.scheduler.task_dispatcher import TaskDispatcher
from packages.aura_game import EmbeddedGameRunner, SubprocessGameRunner
from packages.aura_game import runner as runner_module


class TestGameRunners(unittest.TestCase):
    def _skip_without_admin_on_windows(self):
        return

    def test_embedded_runner_start_stop_lifecycle(self):
        self._skip_without_admin_on_windows()
        runner = EmbeddedGameRunner()
        try:
            self.assertFalse(runner.status()["ready"])
            self.assertTrue(runner.start()["ready"])
            self.assertFalse(runner.stop()["ready"])
        finally:
            runner.close()

    def test_embedded_runner_lists_games_and_tasks(self):
        self._skip_without_admin_on_windows()
        runner = EmbeddedGameRunner()
        try:
            games = runner.list_games()
            names = {row["game_name"] for row in games}
            self.assertIn("aura_benchmark", names)

            tasks = runner.list_tasks("aura_benchmark")
            refs = {row["task_ref"] for row in tasks}
            self.assertIn("tasks:single_sleep.yaml", refs)
        finally:
            runner.close()

    def test_embedded_runner_can_execute_benchmark_task(self):
        self._skip_without_admin_on_windows()
        runner = EmbeddedGameRunner()
        try:
            result = runner.run_task(
                game_name="aura_benchmark",
                task_ref="tasks:single_sleep.yaml",
                inputs={"duration_ms": 1, "scenario": "embedded_test"},
                wait=True,
                timeout_sec=60,
            )
            self.assertEqual(result["dispatch"]["game_name"], "aura_benchmark")
            self.assertEqual(result["run"]["detail"]["status"], "success")
            runs = runner.list_runs(limit=5, game_name="aura_benchmark")
            self.assertTrue(runs)
            self.assertEqual(runs[0]["game_name"], "aura_benchmark")
        finally:
            runner.close()

    def test_embedded_runner_wait_for_run_zero_timeout_waits_until_terminal(self):
        runner = EmbeddedGameRunner()
        fake_runtime = Mock()
        fake_runtime.get_batch_task_status.return_value = [{"cid": "cid-123", "status": "success"}]
        fake_runtime.get_run_detail.return_value = {"cid": "cid-123", "status": "success"}

        with patch.object(runner, "_ensure_runtime", return_value=fake_runtime):
            result = runner.wait_for_run("cid-123", timeout_sec=0)

        self.assertEqual(result["summary"]["status"], "success")
        fake_runtime.get_batch_task_status.assert_called_once_with(["cid-123"])

    def test_embedded_runner_cancel_task_delegates_to_runtime(self):
        runner = EmbeddedGameRunner()
        fake_runtime = Mock()
        fake_runtime.cancel_task.return_value = {"status": "success", "message": "cancelled"}
        with patch("packages.aura_game.runner.create_runtime", return_value=fake_runtime):
            result = runner.cancel_task("cid-123")

        self.assertEqual(result["status"], "success")
        fake_runtime.cancel_task.assert_called_once_with("cid-123")

    def test_embedded_runner_target_status_uses_runtime_target_service(self):
        runner = EmbeddedGameRunner()
        fake_service = Mock()
        fake_service.target_summary.return_value = {"title": "Resonance", "client_rect_screen": [1, 2, 3, 4]}
        with (
            patch.object(runner, "_ensure_running_runtime", return_value=object()),
            patch("packages.aura_game.runner.service_registry.get_service_instance", return_value=fake_service) as get_service,
        ):
            result = runner.target_status(game_name="resonance")

        self.assertTrue(result["ok"])
        self.assertEqual(result["game_name"], "resonance")
        self.assertEqual(result["target"]["title"], "Resonance")
        get_service.assert_called_once_with("target_runtime")

    def test_embedded_runner_target_snapshot_serializes_runtime_capture(self):
        try:
            import numpy as np
        except ModuleNotFoundError:
            self.skipTest("numpy is not installed")

        runner = EmbeddedGameRunner()
        fake_service = Mock()
        fake_service.capture.return_value = SimpleNamespace(
            success=True,
            image=np.zeros((2, 3, 3), dtype=np.uint8),
            backend="gdi",
            image_size=(3, 2),
            window_rect=(10, 20, 30, 40),
            relative_rect=(0, 0, 3, 2),
            quality_flags=["test"],
            error_message="",
        )
        fake_service.target_summary.return_value = {"title": "Resonance"}
        with (
            patch.object(runner, "_ensure_running_runtime", return_value=object()),
            patch("packages.aura_game.runner.service_registry.get_service_instance", return_value=fake_service),
        ):
            result = runner.target_snapshot(game_name="resonance", backend="gdi")

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "gdi")
        self.assertEqual(result["image_size"], [3, 2])
        self.assertEqual(result["window_rect"], [10, 20, 30, 40])
        self.assertEqual(result["quality_flags"], ["test"])
        self.assertTrue(result["image_png"].startswith(b"\x89PNG"))

    def test_subprocess_runner_start_stop_lifecycle(self):
        self._skip_without_admin_on_windows()
        runner = SubprocessGameRunner()
        try:
            self.assertFalse(runner.status()["ready"])
            self.assertTrue(runner.start()["ready"])
            self.assertFalse(runner.stop()["ready"])
        finally:
            runner.close()

    def test_subprocess_runner_lists_games(self):
        self._skip_without_admin_on_windows()
        runner = SubprocessGameRunner()
        try:
            games = runner.list_games()
            names = {row["game_name"] for row in games}
            self.assertIn("aura_benchmark", names)
        finally:
            runner.close()

    def test_subprocess_runner_cancel_task_uses_request_channel(self):
        runner = SubprocessGameRunner()
        with patch.object(runner, "_request", return_value={"status": "success"}) as request:
            result = runner.cancel_task("cid-123")

        self.assertEqual(result["status"], "success")
        request.assert_called_once_with("cancel_task", cid="cid-123")

    def test_subprocess_runner_target_helpers_use_request_channel(self):
        runner = SubprocessGameRunner()
        with patch.object(runner, "_request", return_value={"ok": True}) as request:
            result = runner.target_status(game_name="resonance")
            self.assertTrue(result["ok"])
            request.assert_called_once_with("target_status", game_name="resonance")

        with patch.object(runner, "_request", return_value={"ok": True}) as request:
            result = runner.target_snapshot(game_name="resonance", backend="gdi")
            self.assertTrue(result["ok"])
            request.assert_called_once_with("target_snapshot", game_name="resonance", backend="gdi")

    def test_embedded_doctor_can_require_cpu_ocr(self):
        runner = EmbeddedGameRunner()
        fake_runtime = SimpleNamespace(
            actions={},
            get_all_services_for_api=lambda: [],
        )
        fake_screen = Mock()
        fake_screen.self_check.return_value = {"ok": True}
        fake_ocr = Mock()
        fake_ocr.self_check.return_value = True
        fake_ocr.get_backend.return_value = "onnxruntime"
        fake_ocr.get_provider.return_value = "CPUExecutionProvider"
        fake_ocr.get_model.return_value = "ppocrv5_server"

        def get_service(name):
            return {"screen": fake_screen, "ocr": fake_ocr}[name]

        with (
            patch.object(runner, "_ensure_runtime", return_value=fake_runtime),
            patch.object(runner, "list_games", return_value=[{"game_name": "resonance", "kind": "game"}]),
            patch.object(runner, "status", return_value={"ready": False}),
            patch("packages.aura_game.runner.service_registry.get_service_instance", side_effect=get_service),
        ):
            result = runner.doctor(check_ocr=True, required_ocr_provider="cpu")

        self.assertTrue(result["ok"])
        self.assertEqual(result["ocr"]["provider"], "CPUExecutionProvider")
        self.assertEqual(result["ocr"]["backend"], "onnxruntime")

    def test_subprocess_doctor_forwards_ocr_options(self):
        runner = SubprocessGameRunner()
        with patch.object(runner, "_request", return_value={"ok": True}) as request:
            result = runner.doctor(check_ocr=True, required_ocr_provider="cuda")

        self.assertTrue(result["ok"])
        request.assert_called_once_with(
            "doctor",
            include_shared=True,
            check_ocr=True,
            required_ocr_provider="cuda",
        )

    def test_subprocess_runner_applies_env_overrides_only_while_starting_process(self):
        seen: dict[str, str | None] = {}

        class FakeProcess:
            def start(self):
                seen["AURA_BASE_PATH"] = os.environ.get("AURA_BASE_PATH")
                seen["AURA_TEST_ONLY"] = os.environ.get("AURA_TEST_ONLY")

        old_base = os.environ.get("AURA_BASE_PATH")
        os.environ["AURA_BASE_PATH"] = "old-base"
        os.environ.pop("AURA_TEST_ONLY", None)
        try:
            runner = SubprocessGameRunner(env_overrides={"AURA_BASE_PATH": "new-base", "AURA_TEST_ONLY": "1"})
            runner._start_process_with_env(FakeProcess())
        finally:
            if old_base is None:
                os.environ.pop("AURA_BASE_PATH", None)
            else:
                os.environ["AURA_BASE_PATH"] = old_base
            os.environ.pop("AURA_TEST_ONLY", None)

        self.assertEqual(seen, {"AURA_BASE_PATH": "new-base", "AURA_TEST_ONLY": "1"})
        self.assertEqual(os.environ.get("AURA_BASE_PATH"), old_base)
        self.assertIsNone(os.environ.get("AURA_TEST_ONLY"))

    def test_subprocess_runner_start_timeout_includes_spawn_margin(self):
        runner = SubprocessGameRunner(startup_timeout_sec=10)

        timeout = runner._request_timeout_sec("start", {})

        self.assertGreaterEqual(timeout, 40.0)

    def test_subprocess_runner_run_task_zero_timeout_has_no_parent_deadline(self):
        runner = SubprocessGameRunner(startup_timeout_sec=10)

        self.assertIsNone(runner._request_timeout_sec("run_task", {"wait": True, "timeout_sec": 0}))
        self.assertIsNone(runner._request_timeout_sec("run_task", {"wait": True}))
        self.assertGreaterEqual(
            runner._request_timeout_sec("run_task", {"wait": True, "timeout_sec": 60}),
            65.0,
        )

    def test_subprocess_runner_discards_process_after_request_timeout(self):
        class FakeConnection:
            def __init__(self) -> None:
                self.sent = []
                self.closed = False
                self.timeout = None

            def send(self, payload):
                self.sent.append(payload)

            def poll(self, timeout):
                self.timeout = timeout
                return False

            def close(self):
                self.closed = True

        class FakeProcess:
            def __init__(self) -> None:
                self.terminated = False
                self.join_calls = 0

            def is_alive(self) -> bool:
                return not self.terminated

            def terminate(self) -> None:
                self.terminated = True

            def join(self, timeout=None) -> None:
                self.join_calls += 1

        runner = SubprocessGameRunner(startup_timeout_sec=10)
        connection = FakeConnection()
        process = FakeProcess()
        runner._parent_conn = connection
        runner._process = process

        with patch.object(runner, "_ensure_process", lambda: None):
            with self.assertRaisesRegex(TimeoutError, "Subprocess runner request 'start' timed out"):
                runner._request("start")

        self.assertEqual(connection.sent, [{"op": "start", "kwargs": {}}])
        self.assertGreaterEqual(connection.timeout, 40.0)
        self.assertTrue(connection.closed)
        self.assertTrue(process.terminated)
        self.assertGreaterEqual(process.join_calls, 1)
        self.assertIsNone(runner._parent_conn)
        self.assertIsNone(runner._process)

    def test_subprocess_entry_exits_cleanly_when_parent_pipe_closes(self):
        class FakeEmbeddedRunner:
            close_called = False

            def __init__(self, **_kwargs):
                pass

            def close(self):
                type(self).close_called = True

        parent_conn, child_conn = multiprocessing.Pipe()
        parent_conn.close()
        with patch.object(runner_module, "EmbeddedGameRunner", FakeEmbeddedRunner):
            runner_module._subprocess_entry(child_conn, "embedded_full", 1)

        self.assertTrue(FakeEmbeddedRunner.close_called)

    def test_embedded_runner_propagates_scheduler_startup_errors(self):
        runner = EmbeddedGameRunner()
        try:
            with patch(
                "packages.aura_core.scheduler.core.ensure_admin_startup",
                side_effect=RuntimeError("scheduler startup failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "scheduler startup failed"):
                    runner.list_games()
        finally:
            runner.close()

    def test_dispatcher_cancel_marks_cooperative_cancel_request(self):
        class FakeTask:
            def __init__(self):
                self.cancel_called = False

            def done(self):
                return False

            def cancel(self):
                self.cancel_called = True

        task = FakeTask()
        scheduler = SimpleNamespace(
            fallback_lock=threading.RLock(),
            running_tasks={"cid-123": task},
            _running_task_meta={},
            _loop=None,
        )
        clear_task_cancel("cid-123")

        try:
            result = TaskDispatcher(scheduler).cancel_task("cid-123")

            self.assertEqual(result["status"], "success")
            self.assertTrue(task.cancel_called)
            self.assertTrue(is_task_cancel_requested("cid-123"))
        finally:
            clear_task_cancel("cid-123")


if __name__ == "__main__":
    unittest.main()
