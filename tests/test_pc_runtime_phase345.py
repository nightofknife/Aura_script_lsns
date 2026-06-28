from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import yaml

from packages.aura_core.context.plan import current_plan_name
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.windows.debug_artifacts import DebugArtifactsManager
from plans.aura_base.src.platform.windows.window_spec import WindowSpecStatus, ensure_window_spec, evaluate_window_spec
from plans.aura_base.src.services.gamepad_service import GamepadService
from plans.aura_base.src.services.input_mapping_service import InputMappingService
from plans.aura_base.src.services.target_runtime_service import TargetRuntimeService
from plans.aura_base.src.services.windows_diagnostics_service import WindowsDiagnosticsService


class _FakeConfig:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        current = self._values
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


class _FakeController:
    def __init__(self, calls):
        self._calls = calls

    def click(self, x=None, y=None, button="left", clicks=1, interval=None):
        self._calls.append(("click", x, y, button, clicks, interval))

    def mouse_down(self, button="left"):
        self._calls.append(("mouse_down", button))

    def mouse_up(self, button="left"):
        self._calls.append(("mouse_up", button))


class _FakeApp:
    def __init__(self):
        self.calls = []
        self.controller = _FakeController(self.calls)

    def press_key(self, key, presses=1, interval=None):
        self.calls.append(("press_key", key, presses, interval))

    def key_down(self, key):
        self.calls.append(("key_down", key))

    def key_up(self, key):
        self.calls.append(("key_up", key))

    def type_text(self, text, interval=None):
        self.calls.append(("type_text", text, interval))

    def look_delta(self, dx, dy):
        self.calls.append(("look_delta", dx, dy))

    def look_hold(self, vx, vy, *, duration_ms, tick_ms=None):
        self.calls.append(("look_hold", vx, vy, duration_ms, tick_ms))


class _FakeGamepad:
    def __init__(self):
        self.calls = []

    def press_button(self, button):
        self.calls.append(("press_button", button))

    def release_button(self, button):
        self.calls.append(("release_button", button))

    def tap_button(self, button, *, duration_ms=0):
        self.calls.append(("tap_button", button, duration_ms))

    def tilt_stick(self, *, stick, x, y, duration_ms=0, auto_center=False):
        self.calls.append(("tilt_stick", stick, x, y, duration_ms, auto_center))

    def center_stick(self, stick):
        self.calls.append(("center_stick", stick))

    def set_trigger(self, *, side, value, duration_ms=0, auto_reset=False):
        self.calls.append(("set_trigger", side, value, duration_ms, auto_reset))


class TestInputMappingService(unittest.TestCase):
    def test_available_profiles_and_list_actions_merge_base_and_plan_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            base_dir = repo_root / "plans" / "aura_base" / "data" / "input_profiles"
            demo_dir = repo_root / "plans" / "demo" / "data" / "input_profiles"
            base_dir.mkdir(parents=True, exist_ok=True)
            demo_dir.mkdir(parents=True, exist_ok=True)

            (base_dir / "default_pc.yaml").write_text(
                yaml.safe_dump(
                    {
                        "actions": {
                            "confirm": {"type": "key", "key": "enter"},
                            "base_only": {"type": "key", "key": "f"},
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (demo_dir / "default_pc.yaml").write_text(
                yaml.safe_dump(
                    {
                        "actions": {
                            "confirm": {"type": "key", "key": "space"},
                            "plan_only": {"type": "key", "key": "e"},
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (demo_dir / "boss_gamepad.yaml").write_text(
                yaml.safe_dump({"actions": {"lock_target": {"type": "gamepad_button", "button": "rs"}}}, sort_keys=False),
                encoding="utf-8",
            )

            service = InputMappingService(
                _FakeConfig({"input": {"actions": {"config_only": {"type": "key", "key": "q"}}}}),
                _FakeGamepad(),
            )
            service._repo_root = repo_root

            token = current_plan_name.set("demo")
            try:
                profiles = service.available_profiles()
                actions = service.list_actions()
            finally:
                current_plan_name.reset(token)

        self.assertEqual(profiles, ["boss_gamepad", "default_pc"])
        self.assertEqual(actions["confirm"]["key"], "space")
        self.assertEqual(actions["base_only"]["key"], "f")
        self.assertEqual(actions["plan_only"]["key"], "e")
        self.assertEqual(actions["config_only"]["key"], "q")

    def test_chord_press_holds_all_modifiers_until_trigger_key(self):
        service = InputMappingService(_FakeConfig(), _FakeGamepad())
        app = _FakeApp()

        result = service.execute_binding(
            {"type": "chord", "keys": ["ctrl", "shift", "i"], "action_name": "open_overlay"},
            phase="press",
            app=app,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            app.calls,
            [
                ("key_down", "ctrl"),
                ("key_down", "shift"),
                ("press_key", "i", 1, 0.0),
                ("key_up", "shift"),
                ("key_up", "ctrl"),
            ],
        )

    def test_look_and_gamepad_bindings_route_to_the_expected_primitives(self):
        fake_gamepad = _FakeGamepad()
        service = InputMappingService(_FakeConfig(), fake_gamepad)
        app = _FakeApp()

        service.execute_binding(
            {"type": "look", "direction": "left", "strength": 0.6, "duration_ms": 120, "tick_ms": 20},
            phase="press",
            app=app,
        )
        service.execute_binding(
            {"type": "gamepad_button", "button": "a"},
            phase="hold",
            app=app,
        )
        service.execute_binding(
            {"type": "gamepad_stick", "stick": "right", "x": 0.75, "y": -0.25},
            phase="release",
            app=app,
        )
        service.execute_binding(
            {"type": "trigger", "side": "left", "value": 1.0, "duration_ms": 40},
            phase="tap",
            app=app,
        )

        self.assertEqual(app.calls, [("look_hold", -0.6, 0.0, 120, 20)])
        self.assertEqual(
            fake_gamepad.calls,
            [
                ("press_button", "a"),
                ("center_stick", "right"),
                ("set_trigger", "left", 1.0, 40, True),
            ],
        )

    def test_look_binding_rejects_invalid_direction(self):
        service = InputMappingService(_FakeConfig(), _FakeGamepad())

        with self.assertRaises(TargetRuntimeError) as cm:
            service.execute_binding(
                {"type": "look", "direction": "northwest", "strength": 0.6},
                phase="press",
                app=_FakeApp(),
            )

        self.assertEqual(cm.exception.code, "look_direction_invalid")


class TestDebugArtifactsManager(unittest.TestCase):
    def test_repo_root_resolves_to_workspace_root(self):
        manager = DebugArtifactsManager(_FakeConfig())
        self.assertTrue((manager._repo_root / "plans" / "aura_base").is_dir())

    def test_capture_error_artifacts_writes_failure_bundle_when_enabled(self):
        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "hwnd", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                    "debug": {
                        "dump_window_summary_on_error": True,
                        "input_trace_size": 2,
                        "artifact_dir": "artifacts",
                    },
                }
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DebugArtifactsManager(config)
            manager._repo_root = Path(temp_dir)
            manager.record_input_event("move_to", {"x": 10, "y": 20})
            manager.record_input_event("click", {"x": 15, "y": 25})
            manager.record_input_event("press_key", {"key": "f"})

            token = current_plan_name.set("demo")
            try:
                result = manager.capture_error_artifacts(
                    method_name="focus",
                    exc=TargetRuntimeError("window_not_found", "lost", {"hwnd": 100}),
                    session=SimpleNamespace(self_check=lambda: {"ok": True, "target": {"hwnd": 100}}),
                    extra={"attempt": 1},
                )
            finally:
                current_plan_name.reset(token)

            self.assertIsNotNone(result)
            artifact_path = Path(result["artifact_path"])
            self.assertTrue(artifact_path.is_file())
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["method_name"], "focus")
        self.assertEqual(payload["plan_name"], "demo")
        self.assertEqual(payload["session_self_check"]["target"]["hwnd"], 100)
        self.assertEqual([item["event"] for item in payload["input_trace"]], ["click", "press_key"])
        self.assertIsNone(result["capture_path"])

    def test_capture_error_artifacts_returns_none_when_debugging_is_disabled(self):
        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "hwnd", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                }
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = DebugArtifactsManager(config)
            manager._repo_root = Path(temp_dir)
            result = manager.capture_error_artifacts(
                method_name="focus",
                exc=TargetRuntimeError("window_not_found", "lost"),
                session=None,
            )

            self.assertIsNone(result)
            self.assertFalse((Path(temp_dir) / "logs").exists())


class TestGamepadService(unittest.TestCase):
    def test_self_check_reports_disabled_when_gamepad_is_off(self):
        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "hwnd", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                }
            }
        )

        service = GamepadService(config)
        result = service.self_check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "disabled")

    def test_enabled_gamepad_service_reuses_backend_and_routes_calls(self):
        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "hwnd", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                    "gamepad": {"enabled": True, "backend": "vgamepad", "device_type": "xbox360"},
                }
            }
        )
        backend = Mock()
        backend.self_check.return_value = {"ok": True}

        with patch("plans.aura_base.src.services.gamepad_service.build_gamepad_backend", return_value=backend) as build_backend:
            service = GamepadService(config)
            self.assertTrue(service.self_check()["ok"])
            service.press_button("a")
            service.release_button("a")
            service.tap_button("b", duration_ms=10)
            service.tilt_stick(stick="left", x=0.5, y=-0.2, duration_ms=30, auto_center=True)
            service.center_stick("right")
            service.set_trigger(side="left", value=1.0, duration_ms=50, auto_reset=True)
            service.reset()

        self.assertEqual(build_backend.call_count, 1)
        backend.press_button.assert_called_once_with("a")
        backend.release_button.assert_called_once_with("a")
        backend.tap_button.assert_called_once_with("b", duration_ms=10)
        backend.tilt_stick.assert_called_once_with(stick="left", x=0.5, y=-0.2, duration_ms=30, auto_center=True)
        backend.center_stick.assert_called_once_with("right")
        backend.set_trigger.assert_called_once_with(side="left", value=1.0, duration_ms=50, auto_reset=True)
        backend.reset.assert_called_once_with()

    def test_self_check_surfaces_backend_initialization_errors(self):
        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "hwnd", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                    "gamepad": {"enabled": True, "backend": "vgamepad", "device_type": "xbox360"},
                }
            }
        )

        with patch(
            "plans.aura_base.src.services.gamepad_service.build_gamepad_backend",
            side_effect=TargetRuntimeError("gamepad_backend_unavailable", "missing"),
        ):
            service = GamepadService(config)
            result = service.self_check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "gamepad_backend_unavailable")


class TestWindowSpecAndDiagnostics(unittest.TestCase):
    def test_evaluate_window_spec_detects_mismatches_and_require_exact_raises(self):
        fake_target = SimpleNamespace(
            hwnd=100,
            get_window_rect_screen=lambda: (10, 20, 1300, 760),
            get_client_rect=lambda: (0, 0, 1280, 720),
        )

        with patch("plans.aura_base.src.platform.windows.window_spec._get_window_monitor_index", return_value=1):
            status = evaluate_window_spec(
                fake_target,
                SimpleNamespace(mode="require_exact", client_size=(1600, 900), position=(50, 60), monitor_index=0),
            )

        self.assertFalse(status.ok)
        self.assertEqual(status.mismatches, ("client_size", "position", "monitor_index"))

        with patch("plans.aura_base.src.platform.windows.window_spec._get_window_monitor_index", return_value=1):
            with self.assertRaises(TargetRuntimeError) as cm:
                ensure_window_spec(
                    fake_target,
                    SimpleNamespace(mode="require_exact", client_size=(1600, 900), position=(50, 60), monitor_index=0),
                )

        self.assertEqual(cm.exception.code, "window_spec_mismatch")

    def test_try_resize_then_verify_applies_window_spec_and_rechecks(self):
        fake_target = SimpleNamespace(hwnd=100)
        first = WindowSpecStatus(
            ok=False,
            applied=False,
            mismatches=("client_size",),
            current={"client_size": [1280, 720]},
            desired={"client_size": [1600, 900]},
        )
        second = WindowSpecStatus(
            ok=True,
            applied=True,
            mismatches=(),
            current={"client_size": [1600, 900]},
            desired={"client_size": [1600, 900]},
        )

        with (
            patch("plans.aura_base.src.platform.windows.window_spec.evaluate_window_spec", side_effect=[first, second]),
            patch("plans.aura_base.src.platform.windows.window_spec.apply_window_spec") as apply_spec,
        ):
            result = ensure_window_spec(
                fake_target,
                SimpleNamespace(mode="try_resize_then_verify", client_size=(1600, 900), position=None, monitor_index=None),
            )

        self.assertTrue(result.ok)
        apply_spec.assert_called_once_with(
            fake_target,
            SimpleNamespace(mode="try_resize_then_verify", client_size=(1600, 900), position=None, monitor_index=None),
        )

    def test_windows_diagnostics_exposes_window_spec_and_gamepad_state(self):
        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "hwnd", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                    "window_spec": {"mode": "require_exact", "client_size": [1600, 900]},
                }
            }
        )
        diagnostics = WindowsDiagnosticsService(config, TargetRuntimeService(config))
        status = WindowSpecStatus(
            ok=True,
            applied=False,
            mismatches=(),
            current={"client_size": [1600, 900]},
            desired={"client_size": [1600, 900]},
        )

        with (
            patch("plans.aura_base.src.services.windows_diagnostics_service.WindowTarget.create", return_value=SimpleNamespace(hwnd=100)),
            patch("plans.aura_base.src.services.windows_diagnostics_service.evaluate_window_spec", return_value=status) as evaluate_spec,
            patch("plans.aura_base.src.services.windows_diagnostics_service.ensure_window_spec", return_value=status) as ensure_spec,
        ):
            check_result = diagnostics.check_window_spec()
            ensure_result = diagnostics.ensure_window_spec()
            capabilities = diagnostics.show_runtime_capabilities()
            gamepad_info = diagnostics.show_gamepad_info()

        self.assertTrue(check_result["ok"])
        self.assertTrue(ensure_result["ok"])
        self.assertEqual(capabilities["window_spec"]["mode"], "require_exact")
        self.assertFalse(gamepad_info["ok"])
        self.assertEqual(gamepad_info["reason"], "disabled")
        evaluate_spec.assert_called_once_with(ANY, ANY)
        ensure_spec.assert_called_once_with(ANY, ANY)


class TestTargetRuntimeServiceDebugIntegration(unittest.TestCase):
    def test_terminal_runtime_errors_trigger_debug_artifact_capture(self):
        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "hwnd", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                }
            }
        )
        runtime = TargetRuntimeService(config)

        class _BrokenAdapter:
            def close(self):
                return None

            def focus(self):
                raise TargetRuntimeError("window_not_found", "lost")

        runtime._build_session = lambda resolved: _BrokenAdapter()  # type: ignore[assignment]

        with patch.object(runtime._debug_artifacts, "capture_error_artifacts") as capture_artifacts:
            with self.assertRaises(TargetRuntimeError) as cm:
                runtime.focus()

        self.assertEqual(cm.exception.code, "window_not_found")
        capture_artifacts.assert_called_once()


if __name__ == "__main__":
    unittest.main()
