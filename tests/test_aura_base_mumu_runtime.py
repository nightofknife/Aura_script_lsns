from __future__ import annotations

import subprocess
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import win32con

from packages.aura_game.runner import EmbeddedGameRunner
from plans.aura_base.src.actions.atomic_actions import focus_window_with_input
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.mumu.adb_discovery import AdbController
from plans.aura_base.src.platform.mumu.android_touch_input import MuMuAndroidTouchInputBackend
from plans.aura_base.src.platform.mumu.helper_manager import AndroidTouchHelperManager
from plans.aura_base.src.platform.mumu.runtime_assets import resolve_android_touch_helper_path, resolve_scrcpy_server_jar_path
from plans.aura_base.src.platform.mumu.scrcpy_capture import MuMuScrcpyCaptureBackend
from plans.aura_base.src.platform.mumu.session import MuMuSession
from plans.aura_base.src.platform.runtime_config import RuntimeCaptureConfig, RuntimeInputConfig, RuntimeTargetConfig
from plans.aura_base.src.platform.runtime_service import TargetRuntimeService
from plans.aura_base.src.platform.windows.capture_backends import WindowsGdiCaptureBackend
from plans.aura_base.src.platform.windows.desktop_adapter import WindowsDesktopAdapter
from plans.aura_base.src.platform.windows.input_backends import WindowsSendInputBackend, WindowsWindowMessageInputBackend
from plans.aura_base.src.platform.windows.window_target import WindowTarget


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


class _FakeHelperManager:
    def __init__(self):
        self.local_port = 19889
        self.remote_port = 9889
        self.batches = []
        self.ready_calls = 0

    def ensure_ready(self):
        self.ready_calls += 1

    def is_healthy(self):
        return True

    def send_commands(self, commands):
        self.batches.append(commands)
        return {"ok": True}

    def close(self):
        return None


class _FakeSession:
    def __init__(self, serial: str):
        self.serial = serial
        self.ensure_ready_calls = 0

    def ensure_ready(self):
        self.ensure_ready_calls += 1

    def close(self):
        return None

    def self_check(self):
        return {
            "ok": True,
            "provider": "mumu",
            "serial": self.serial,
            "capture": {"backend": "scrcpy_stream"},
            "input": {"backend": "android_touch"},
            "capabilities": self.capabilities(),
        }

    def capabilities(self):
        return {
            "absolute_pointer": True,
            "relative_look": False,
            "keyboard": True,
            "text_input": True,
            "background_input": True,
        }


class _FakeRuntimeAdapter:
    def __init__(self, provider: str = "windows"):
        self.provider = provider
        self.ensure_ready_calls = 0
        self.closed = False

    def ensure_ready(self):
        self.ensure_ready_calls += 1

    def close(self):
        self.closed = True

    def self_check(self):
        return {
            "ok": True,
            "provider": self.provider,
            "target": {"mode": "title", "hwnd": 100},
            "capture": {"backend": "gdi"},
            "input": {"backend": "sendinput"},
            "capabilities": self.capabilities(),
        }

    def set_capture_backend(self, backend):
        if backend != "gdi":
            raise TargetRuntimeError("backend_runtime_switch_unsupported", "unsupported")

    def capabilities(self):
        return {
            "absolute_pointer": True,
            "relative_look": self.provider == "windows",
            "keyboard": True,
            "text_input": True,
            "background_input": False,
        }


class _FakeApp:
    def __init__(self):
        self.focus_calls = []

    def focus_with_input(self, click_delay: float = 0.3):
        self.focus_calls.append(click_delay)
        return True


class _FakeAdbForHelper:
    def __init__(self):
        self.calls = []
        self.info = SimpleNamespace(abi="x86_64")

    def remove_forward(self, serial, local_port):
        self.calls.append(("remove_forward", serial, local_port))

    def forward(self, serial, local_port, remote_port):
        self.calls.append(("forward", serial, local_port, remote_port))

    def shell_script(self, serial, script, timeout_sec=None, check=True):
        self.calls.append(("shell_script", serial, script, timeout_sec, check))
        return ""

    def push(self, serial, local_path, remote_path):
        self.calls.append(("push", serial, local_path, remote_path))

    def get_device_info(self, serial):
        return self.info


class _FakeMumuCaptureBackend:
    def ensure_ready(self):
        return None

    def is_healthy(self):
        return True

    def close(self):
        return None

    def self_check(self):
        return {"ok": True}


class _FakeMumuInputBackend:
    def ensure_ready(self):
        return None

    def is_healthy(self):
        return True

    def capabilities(self):
        return {"absolute_pointer": True, "keyboard": True}

    def close(self):
        return None

    def self_check(self):
        return {"ok": True}


class _FakeAdbForLaunch:
    def __init__(self, *, fail_monkey: bool = False):
        self.fail_monkey = fail_monkey
        self.calls = []
        self.info = SimpleNamespace(manufacturer="MuMu", model="emulator", abi="x86_64")

    def get_device_info(self, serial):
        return self.info

    def shell(self, serial, args, timeout_sec=None):
        self.calls.append((serial, list(args), timeout_sec))
        if args[:1] == ["monkey"] and self.fail_monkey:
            raise TargetRuntimeError("adb_command_failed", "monkey failed")
        if args[:1] == ["monkey"]:
            return "Events injected: 1"
        if args[:3] == ["cmd", "package", "resolve-activity"]:
            return "priority=0 preferredOrder=0 match=0x108000\ncom.hermes.goda/.MainActivity"
        if args[:2] == ["am", "start"]:
            return "Status: ok"
        return ""


class _InspectableAdbController(AdbController):
    def __init__(self):
        super().__init__(executable="adb")
        self.last_args = None

    def run(self, args, *, timeout_sec=None, check=True):
        self.last_args = list(args)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()


class TestAuraBaseRuntime(unittest.TestCase):
    def test_runtime_requires_supported_provider(self):
        runtime = TargetRuntimeService(_FakeConfig({}))

        with self.assertRaises(TargetRuntimeError) as cm:
            runtime._get_or_create_session()

        self.assertEqual(cm.exception.code, "provider_unsupported")

    def test_runtime_auto_selects_first_healthy_serial(self):
        runtime = TargetRuntimeService(
            _FakeConfig(
                {
                    "runtime": {
                        "family": "android_emulator",
                        "provider": "mumu",
                        "target": {"mode": "auto", "adb_serial": "auto"},
                        "capture": {"backend": "scrcpy_stream"},
                        "input": {"backend": "android_touch"},
                    }
                }
            )
        )

        class _FakeAdb:
            def __init__(self, *args, **kwargs):
                pass

            def connect(self, serial):
                return True

            def list_devices(self):
                return ["bad-serial", "good-serial"]

        runtime._make_mumu_session = lambda serial, adb, resolved: (_FakeSession(serial) if serial == "good-serial" else (_ for _ in ()).throw(RuntimeError("bad serial")))  # type: ignore[assignment]

        from unittest.mock import patch

        with patch("plans.aura_base.src.services.target_runtime_service.AdbController", _FakeAdb):
            session = runtime._get_or_create_session()

        self.assertEqual(session.serial, "good-serial")

    def test_runtime_prefers_unified_runtime_config_over_legacy_target(self):
        runtime = TargetRuntimeService(
            _FakeConfig(
                {
                    "runtime": {
                        "family": "windows_desktop",
                        "provider": "windows",
                        "target": {"mode": "hwnd", "hwnd": 100},
                        "capture": {"backend": "gdi"},
                        "input": {"backend": "sendinput"},
                    },
                    "target": {
                        "provider": "mumu",
                    },
                }
            )
        )
        fake_adapter = _FakeRuntimeAdapter(provider="windows")
        runtime._build_session = lambda resolved: fake_adapter  # type: ignore[assignment]

        session = runtime._get_or_create_session()

        self.assertIs(session, fake_adapter)
        self.assertEqual(runtime.self_check()["provider"], "windows")

    def test_list_capture_backends_uses_current_provider_support_matrix(self):
        runtime = TargetRuntimeService(
            _FakeConfig(
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
        )

        backends = runtime.list_capture_backends()

        self.assertEqual(backends["available"], ["wgc", "dxgi", "gdi", "printwindow"])
        self.assertEqual(backends["default"], "gdi")

    def test_set_capture_backend_rejects_runtime_switch(self):
        runtime = TargetRuntimeService(
            _FakeConfig(
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
        )

        with self.assertRaises(TargetRuntimeError) as cm:
            runtime.set_capture_backend("dxgi")

        self.assertEqual(cm.exception.code, "backend_runtime_switch_unsupported")

    def test_android_touch_click_and_drag_sequences(self):
        helper = _FakeHelperManager()
        backend = MuMuAndroidTouchInputBackend(
            helper_manager=helper,
            viewport_provider=lambda: (0, 0, 200, 100),
            config={"path_fps": 10},
        )

        backend.click(20, 30)
        backend.move_to(40, 50, duration=0.0)
        backend.mouse_down()
        backend.move_to(80, 90, duration=0.1)
        backend.mouse_up()

        self.assertEqual(helper.batches[0][0]["type"], "down")
        self.assertEqual(helper.batches[0][0]["x"], 20)
        self.assertEqual(helper.batches[0][0]["y"], 30)
        self.assertEqual(helper.batches[1][0]["type"], "down")
        self.assertEqual(helper.batches[1][0]["x"], 40)
        self.assertEqual(helper.batches[1][0]["y"], 50)
        self.assertEqual(helper.batches[2][0]["type"], "move")
        self.assertEqual(helper.batches[3][0]["type"], "up")

    def test_android_touch_maps_landscape_capture_to_portrait_touch_axes(self):
        helper = _FakeHelperManager()
        backend = MuMuAndroidTouchInputBackend(
            helper_manager=helper,
            viewport_provider=lambda: (0, 0, 1920, 1080),
            touch_physical_size=(1080, 1920),
            display_rotation=1,
            config={"path_fps": 10},
        )

        backend.click(521, 895)

        self.assertEqual(helper.batches[0][0]["x"], 184)
        self.assertEqual(helper.batches[0][0]["y"], 521)

    def test_scrcpy_capture_uses_cached_frame(self):
        backend = MuMuScrcpyCaptureBackend("serial-1", {"max_stale_ms": 1000})
        backend._client = object()
        backend._frame = np.arange(3 * 4 * 3, dtype=np.uint8).reshape((3, 4, 3))
        backend._frame_ts = time.monotonic()

        capture = backend.capture((1, 1, 2, 1))

        self.assertTrue(capture.success)
        self.assertEqual(capture.backend, "scrcpy_stream")
        self.assertEqual(capture.relative_rect, (1, 1, 2, 1))
        self.assertEqual(capture.image.shape[:2], (1, 2))

    def test_focus_window_with_input_delegates_to_app_method(self):
        app = _FakeApp()

        result = focus_window_with_input(app, click_delay=0.75)

        self.assertTrue(result)
        self.assertEqual(app.focus_calls, [0.75])

    def test_android_touch_healthcheck_requires_http_success(self):
        helper = AndroidTouchHelperManager(_FakeAdbForHelper(), "serial-1", {"helper_path": "android_touch"})

        from unittest.mock import patch

        with patch.object(helper, "_post_json", side_effect=urllib.error.HTTPError(helper.base_url, 502, "Bad Gateway", hdrs=None, fp=None)):
            self.assertFalse(helper.is_healthy())

        with patch.object(helper, "_post_json", return_value=b""):
            self.assertTrue(helper.is_healthy())

    def test_android_touch_start_helper_uses_background_shell_launch(self):
        adb = _FakeAdbForHelper()
        helper = AndroidTouchHelperManager(adb, "serial-1", {"remote_dir": "/data/local/tmp/aura"})

        helper._start_helper()

        shell_calls = [call for call in adb.calls if call[0] == "shell_script"]
        self.assertEqual(len(shell_calls), 1)
        script = shell_calls[0][2]
        self.assertIn("mkdir -p /data/local/tmp/aura", script)
        self.assertIn("/data/local/tmp/aura/touch", script)
        self.assertIn("/data/local/tmp/aura/android_touch.log", script)
        self.assertIn("</dev/null &", script)

    def test_android_touch_start_helper_tolerates_adb_timeout_for_detached_process(self):
        class _TimeoutAdb(_FakeAdbForHelper):
            def shell_script(self, serial, script, timeout_sec=None, check=True):
                raise subprocess.TimeoutExpired(cmd=["adb"], timeout=timeout_sec or 5.0)

        helper = AndroidTouchHelperManager(_TimeoutAdb(), "serial-1", {"remote_dir": "/data/local/tmp/aura"})

        helper._start_helper()

    def test_android_touch_prefers_builtin_helper_asset_by_abi(self):
        import tempfile
        from unittest.mock import patch

        helper = AndroidTouchHelperManager(_FakeAdbForHelper(), "serial-1", {"helper_path": "android_touch"})

        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "touch").write_bytes(b"touch")
            with patch(
                "plans.aura_base.src.platform.mumu.helper_manager.resolve_android_touch_helper_path",
                return_value=Path(temp_dir) / "touch",
            ):
                resolved = helper._resolve_helper_path()

        self.assertEqual(resolved, (Path(temp_dir) / "touch").resolve())

    def test_scrcpy_capture_defaults_to_builtin_compat_module(self):
        backend = MuMuScrcpyCaptureBackend("serial-1", {})

        self.assertEqual(backend.module_name, "plans.aura_base.src.platform.mumu.scrcpy_compat")
        self.assertTrue(str(resolve_scrcpy_server_jar_path()).endswith("scrcpy-server-v1.24.jar"))

    def test_scrcpy_capture_retries_initial_start(self):
        backend = MuMuScrcpyCaptureBackend("serial-1", {"reconnect_backoff_ms": [0, 0]})
        attempts = {"count": 0}

        def fake_start():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TargetRuntimeError("scrcpy_stream_unavailable", "temporary EOF")
            backend._client = object()
            backend._frame = np.zeros((10, 10, 3), dtype=np.uint8)
            backend._frame_ts = time.monotonic()

        backend._start = fake_start

        backend.ensure_ready()

        self.assertEqual(attempts["count"], 2)
        self.assertTrue(backend.is_healthy())

    def test_adb_shell_script_wraps_whole_script_as_single_shell_command(self):
        adb = _InspectableAdbController()

        adb.shell_script("serial-1", "mkdir -p /data/local/tmp/aura")

        self.assertEqual(
            adb.last_args,
            ["-s", "serial-1", "shell", "sh -c 'mkdir -p /data/local/tmp/aura'"],
        )

    def test_mumu_launch_app_uses_monkey_by_default(self):
        adb = _FakeAdbForLaunch()
        session = MuMuSession("serial-1", adb, _FakeMumuCaptureBackend(), _FakeMumuInputBackend())

        result = session.launch_app("com.hermes.goda")

        self.assertEqual(result["method"], "monkey")
        self.assertTrue(result["launched"])
        self.assertEqual(adb.calls[-1][1], ["monkey", "-p", "com.hermes.goda", "-c", "android.intent.category.LAUNCHER", "1"])

    def test_mumu_launch_app_falls_back_to_resolved_activity(self):
        adb = _FakeAdbForLaunch(fail_monkey=True)
        session = MuMuSession("serial-1", adb, _FakeMumuCaptureBackend(), _FakeMumuInputBackend())

        result = session.launch_app("com.hermes.goda")

        self.assertEqual(result["method"], "am_start")
        self.assertEqual(result["activity"], "com.hermes.goda/.MainActivity")
        self.assertIn(["cmd", "package", "resolve-activity", "--brief", "com.hermes.goda"], [call[1] for call in adb.calls])
        self.assertIn(["am", "start", "-W", "-n", "com.hermes.goda/.MainActivity"], [call[1] for call in adb.calls])

    def test_mumu_force_stop_app_uses_adb_am_force_stop(self):
        adb = _FakeAdbForLaunch()
        session = MuMuSession("serial-1", adb, _FakeMumuCaptureBackend(), _FakeMumuInputBackend())

        result = session.force_stop_app("com.hermes.goda", timeout_sec=3)

        self.assertTrue(result["stopped"])
        self.assertEqual(result["method"], "am_force_stop")
        self.assertEqual(result["package"], "com.hermes.goda")
        self.assertEqual(adb.calls[-1], ("serial-1", ["am", "force-stop", "com.hermes.goda"], 3))

    def test_adb_display_info_parses_size_and_orientation(self):
        class _DisplayInfoAdb(AdbController):
            def __init__(self):
                super().__init__(executable="adb")

            def run(self, args, *, timeout_sec=None, check=True):
                class _Result:
                    def __init__(self, stdout="", stderr="", returncode=0):
                        self.stdout = stdout
                        self.stderr = stderr
                        self.returncode = returncode

                if args[-2:] == ["wm", "size"]:
                    return _Result(stdout="Physical size: 1080x1920\n")
                if args[-2:] == ["dumpsys", "display"]:
                    return _Result(stdout="mCurrentOrientation=1\n")
                return _Result()

        info = _DisplayInfoAdb().get_display_info("serial-1")

        self.assertEqual(info.physical_width, 1080)
        self.assertEqual(info.physical_height, 1920)
        self.assertEqual(info.current_orientation, 1)


class TestWindowsProvider(unittest.TestCase):
    def test_window_target_binds_hwnd_process_and_title_modes(self):
        from unittest.mock import patch
        from plans.aura_base.src.platform.windows import window_target as target_mod
        from plans.aura_base.src.platform.windows.window_selector import WindowCandidate

        common_patches = [
            patch.object(target_mod.win32gui, "IsWindow", return_value=True),
            patch.object(target_mod.win32gui, "IsWindowVisible", return_value=True),
            patch.object(target_mod.win32gui, "GetClientRect", return_value=(0, 0, 1280, 720)),
            patch.object(target_mod.win32gui, "ClientToScreen", return_value=(10, 20)),
            patch.object(target_mod.win32gui, "GetForegroundWindow", return_value=101),
            patch.object(target_mod.win32gui, "GetWindowLong", return_value=win32con.WS_CAPTION),
        ]

        for patcher in common_patches:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(common_patches)])

        candidate = WindowCandidate(
            hwnd=101,
            pid=5001,
            process_name="game.exe",
            exe_path="C:/Games/Game/game.exe",
            title="My Game",
            class_name="Notepad",
            visible=True,
            enabled=True,
            is_child=False,
            parent_hwnd=None,
            foreground=True,
            client_rect=(0, 0, 1280, 720),
            client_rect_screen=(10, 20, 1280, 720),
            window_rect_screen=(0, 0, 1300, 760),
            monitor_index=0,
            process_create_time=1.0,
        )
        with patch.object(target_mod, "resolve_window_candidate", return_value=candidate):
            hwnd_target = WindowTarget.create(RuntimeTargetConfig(mode="hwnd", hwnd=101))
            process_target = WindowTarget.create(RuntimeTargetConfig(mode="process", process_name="game.exe"))
            title_target = WindowTarget.create(RuntimeTargetConfig(mode="title", title="Game"))

        self.assertEqual(hwnd_target.hwnd, 101)
        self.assertEqual(process_target.hwnd, 101)
        self.assertEqual(title_target.hwnd, 101)

    def test_window_selector_prefers_non_launcher_and_largest_window(self):
        from unittest.mock import patch
        from plans.aura_base.src.platform.windows.window_selector import WindowCandidate, resolve_window_candidate

        candidates = [
            WindowCandidate(
                hwnd=101,
                pid=2001,
                process_name="launcher.exe",
                exe_path="C:/Games/Launcher/launcher.exe",
                title="My Game Launcher",
                class_name="LauncherWnd",
                visible=True,
                enabled=True,
                is_child=False,
                parent_hwnd=None,
                foreground=False,
                client_rect=(0, 0, 800, 600),
                client_rect_screen=(10, 20, 800, 600),
                window_rect_screen=(0, 0, 820, 640),
                monitor_index=0,
                process_create_time=10.0,
            ),
            WindowCandidate(
                hwnd=202,
                pid=2002,
                process_name="game.exe",
                exe_path="C:/Games/Game/game.exe",
                title="My Game",
                class_name="GameWnd",
                visible=True,
                enabled=True,
                is_child=False,
                parent_hwnd=None,
                foreground=False,
                client_rect=(0, 0, 1280, 720),
                client_rect_screen=(20, 30, 1280, 720),
                window_rect_screen=(0, 0, 1300, 760),
                monitor_index=0,
                process_create_time=20.0,
            ),
            WindowCandidate(
                hwnd=303,
                pid=2003,
                process_name="game.exe",
                exe_path="C:/Games/Game/game.exe",
                title="My Game",
                class_name="GameWnd",
                visible=True,
                enabled=True,
                is_child=False,
                parent_hwnd=None,
                foreground=False,
                client_rect=(0, 0, 1600, 900),
                client_rect_screen=(20, 30, 1600, 900),
                window_rect_screen=(0, 0, 1620, 940),
                monitor_index=0,
                process_create_time=15.0,
            ),
        ]

        config = RuntimeTargetConfig(
            mode="title",
            title="My Game",
            class_name="GameWnd",
            class_exact=True,
            launcher_process_names=("launcher.exe",),
            prefer_largest_client_area=True,
        )

        with patch("plans.aura_base.src.platform.windows.window_selector.list_window_candidates", return_value=candidates):
            selected = resolve_window_candidate(config)

        self.assertEqual(selected.hwnd, 303)

    def test_window_selector_supports_title_regex_and_pid_filters(self):
        from unittest.mock import patch
        from plans.aura_base.src.platform.windows.window_selector import WindowCandidate, resolve_window_candidate

        candidates = [
            WindowCandidate(
                hwnd=101,
                pid=3001,
                process_name="game.exe",
                exe_path="C:/Games/Game/game.exe",
                title="My Game CN",
                class_name="GameWnd",
                visible=True,
                enabled=True,
                is_child=False,
                parent_hwnd=None,
                foreground=False,
                client_rect=(0, 0, 1280, 720),
                client_rect_screen=(10, 20, 1280, 720),
                window_rect_screen=(0, 0, 1300, 760),
                monitor_index=0,
                process_create_time=12.0,
            ),
            WindowCandidate(
                hwnd=202,
                pid=3002,
                process_name="game.exe",
                exe_path="C:/Games/Game/game.exe",
                title="My Game EN",
                class_name="GameWnd",
                visible=True,
                enabled=True,
                is_child=False,
                parent_hwnd=None,
                foreground=False,
                client_rect=(0, 0, 1280, 720),
                client_rect_screen=(10, 20, 1280, 720),
                window_rect_screen=(0, 0, 1300, 760),
                monitor_index=0,
                process_create_time=13.0,
            ),
        ]

        config = RuntimeTargetConfig(
            mode="title",
            title_regex=r"My Game (EN|CN)",
            pid=3002,
        )

        with patch("plans.aura_base.src.platform.windows.window_selector.list_window_candidates", return_value=candidates):
            selected = resolve_window_candidate(config)

        self.assertEqual(selected.hwnd, 202)

    def test_windows_capture_backends_initialize_by_config(self):
        from unittest.mock import patch

        fake_target = SimpleNamespace(
            ensure_valid=lambda: None,
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
        )
        fake_capture = SimpleNamespace(self_check=lambda: {"ok": True}, close=lambda: None, capture=lambda rect=None: None)
        fake_input = SimpleNamespace(self_check=lambda: {"ok": True}, close=lambda: None)

        for backend in ("dxgi", "gdi", "printwindow"):
            with self.subTest(backend=backend):
                with (
                    patch("plans.aura_base.src.platform.windows.desktop_adapter.WindowTarget.create", return_value=fake_target),
                    patch("plans.aura_base.src.platform.windows.desktop_adapter.build_capture_backend", return_value=fake_capture) as build_capture,
                    patch("plans.aura_base.src.platform.windows.desktop_adapter.build_input_backend", return_value=fake_input),
                ):
                    WindowsDesktopAdapter(
                        target_config=RuntimeTargetConfig(mode="hwnd", hwnd=100),
                        capture_config=RuntimeCaptureConfig(backend=backend),
                        input_config=RuntimeInputConfig(backend="sendinput"),
                    )

                build_capture.assert_called_once()
                self.assertEqual(build_capture.call_args.args[0], backend)

    def test_windows_input_backends_initialize_by_config(self):
        from unittest.mock import patch

        fake_target = SimpleNamespace(
            ensure_valid=lambda: None,
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
        )
        fake_capture = SimpleNamespace(self_check=lambda: {"ok": True}, close=lambda: None)
        fake_input = SimpleNamespace(self_check=lambda: {"ok": True}, close=lambda: None)

        for backend in ("sendinput", "window_message"):
            with self.subTest(backend=backend):
                with (
                    patch("plans.aura_base.src.platform.windows.desktop_adapter.WindowTarget.create", return_value=fake_target),
                    patch("plans.aura_base.src.platform.windows.desktop_adapter.build_capture_backend", return_value=fake_capture),
                    patch("plans.aura_base.src.platform.windows.desktop_adapter.build_input_backend", return_value=fake_input) as build_input,
                ):
                    WindowsDesktopAdapter(
                        target_config=RuntimeTargetConfig(mode="hwnd", hwnd=100),
                        capture_config=RuntimeCaptureConfig(backend="gdi"),
                        input_config=RuntimeInputConfig(backend=backend),
                    )

                build_input.assert_called_once()
                self.assertEqual(build_input.call_args.args[0], backend)

    def test_windows_adapter_fails_fast_when_selected_backend_init_fails(self):
        from unittest.mock import patch

        fake_target = SimpleNamespace(
            ensure_valid=lambda: None,
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
        )

        with (
            patch("plans.aura_base.src.platform.windows.desktop_adapter.WindowTarget.create", return_value=fake_target),
            patch(
                "plans.aura_base.src.platform.windows.desktop_adapter.build_capture_backend",
                side_effect=TargetRuntimeError("windows_capture_init_failed", "boom"),
            ),
            patch("plans.aura_base.src.platform.windows.desktop_adapter.build_input_backend") as build_input,
        ):
            with self.assertRaises(TargetRuntimeError) as cm:
                WindowsDesktopAdapter(
                    target_config=RuntimeTargetConfig(mode="hwnd", hwnd=100),
                    capture_config=RuntimeCaptureConfig(backend="dxgi"),
                    input_config=RuntimeInputConfig(backend="sendinput"),
                )

        self.assertEqual(cm.exception.code, "windows_capture_init_failed")
        build_input.assert_not_called()

    def test_gdi_capture_translates_client_roi_to_screen_bbox(self):
        from unittest.mock import patch
        from PIL import Image

        fake_target = SimpleNamespace(
            ensure_valid=lambda: None,
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
        )

        image = Image.fromarray(np.full((7, 10, 3), 255, dtype=np.uint8), mode="RGB")

        with patch("plans.aura_base.src.platform.windows.capture_backends.ImageGrab.grab", return_value=image) as grab:
            backend = WindowsGdiCaptureBackend(fake_target, {})
            capture = backend.capture((5, 6, 10, 7))

        self.assertTrue(capture.success)
        self.assertEqual(capture.relative_rect, (5, 6, 10, 7))
        self.assertEqual(capture.window_rect, (0, 0, 100, 60))
        self.assertEqual(grab.call_args.kwargs["bbox"], (15, 26, 25, 33))

    def test_sendinput_requires_focus_when_configured(self):
        fake_target = SimpleNamespace(
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: False,
            to_summary=lambda: {"hwnd": 100},
        )

        backend = WindowsSendInputBackend(fake_target, {"focus_before_input": True})

        with self.assertRaises(TargetRuntimeError) as cm:
            backend.click(10, 10)

        self.assertEqual(cm.exception.code, "window_focus_required")

    def test_execute_activation_focus_click_sleep_clicks_center(self):
        from unittest.mock import Mock
        from plans.aura_base.src.platform.runtime_config import RuntimeActivationConfig
        from plans.aura_base.src.platform.windows.activation import execute_activation

        fake_target = SimpleNamespace(
            focus=Mock(return_value=True),
            get_client_rect=lambda: (0, 0, 800, 600),
        )
        fake_input = SimpleNamespace(click=Mock())

        result = execute_activation(
            target=fake_target,
            input_backend=fake_input,
            activation=RuntimeActivationConfig(mode="focus_click_sleep", sleep_ms=0),
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["clicked"])
        self.assertEqual(result["click_point"], [400, 300])
        fake_input.click.assert_called_once_with(x=400, y=300, button="left", clicks=1, interval=0.0)

    def test_window_message_fails_fast_for_flutter_windows(self):
        fake_target = SimpleNamespace(
            binding=SimpleNamespace(
                class_name="FLUTTER_RUNNER_WIN32_WINDOW",
                process_name="localsend_app",
                title="LocalSend",
            ),
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
        )

        with self.assertRaises(TargetRuntimeError) as cm:
            WindowsWindowMessageInputBackend(fake_target, {})

        self.assertEqual(cm.exception.code, "input_backend_unsupported_for_window")

    def test_sendinput_capabilities_include_relative_look(self):
        fake_target = SimpleNamespace(
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
        )

        backend = WindowsSendInputBackend(fake_target, {})

        self.assertTrue(backend.capabilities()["relative_look"])
        self.assertFalse(backend.capabilities()["background_input"])

    def test_sendinput_fails_fast_for_higher_integrity_target(self):
        fake_target = SimpleNamespace(
            binding=SimpleNamespace(
                pid=73364,
                process_name="HTGame.exe",
                title="雷索纳斯  ",
            ),
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
        )

        def fake_integrity(pid):
            if pid == 73364:
                return {"pid": 73364, "rid": 0x3000, "label": "high"}
            return {"pid": int(pid or 0), "rid": 0x2000, "label": "medium"}

        backend = WindowsSendInputBackend(fake_target, {})

        with patch(
            "plans.aura_base.src.platform.windows.input_backends._get_process_integrity_level",
            side_effect=fake_integrity,
        ):
            with self.assertRaises(TargetRuntimeError) as cm:
                backend.click(10, 10)

        self.assertEqual(cm.exception.code, "input_integrity_mismatch")
        self.assertEqual(cm.exception.detail["backend"], "sendinput")
        self.assertEqual(cm.exception.detail["target_process_integrity"], "high")

    def test_window_message_capabilities_disable_relative_look(self):
        fake_target = SimpleNamespace(
            binding=SimpleNamespace(
                class_name="Notepad",
                process_name="game.exe",
                title="My Game",
            ),
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
            hwnd=100,
        )

        backend = WindowsWindowMessageInputBackend(fake_target, {"window_message_allow_unsupported": True})

        self.assertFalse(backend.capabilities()["relative_look"])
        self.assertTrue(backend.capabilities()["background_input"])

    def test_window_message_fails_fast_for_higher_integrity_target(self):
        fake_target = SimpleNamespace(
            binding=SimpleNamespace(
                pid=73364,
                class_name="UnrealWindow",
                process_name="HTGame.exe",
                title="雷索纳斯  ",
            ),
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
            hwnd=100,
        )

        def fake_integrity(pid):
            if pid == 73364:
                return {"pid": 73364, "rid": 0x3000, "label": "high"}
            return {"pid": int(pid or 0), "rid": 0x2000, "label": "medium"}

        with patch(
            "plans.aura_base.src.platform.windows.input_backends._get_process_integrity_level",
            side_effect=fake_integrity,
        ):
            with self.assertRaises(TargetRuntimeError) as cm:
                WindowsWindowMessageInputBackend(fake_target, {})

        self.assertEqual(cm.exception.code, "input_integrity_mismatch")
        self.assertEqual(cm.exception.detail["backend"], "window_message")
        self.assertEqual(cm.exception.detail["target_process_integrity"], "high")

    def test_sendinput_look_delta_sends_relative_mouse_move(self):
        from unittest.mock import patch

        fake_target = SimpleNamespace(
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
        )

        backend = WindowsSendInputBackend(fake_target, {"look": {"scale_x": 1.0, "scale_y": 1.0}})

        with patch("plans.aura_base.src.platform.windows.input_backends._send_mouse_input") as send_mouse:
            backend.look_delta(12, -5)

        send_mouse.assert_called_once_with(flags=win32con.MOUSEEVENTF_MOVE, dx=12, dy=-5)

    def test_sendinput_look_hold_expands_to_multiple_relative_moves(self):
        from unittest.mock import patch

        fake_target = SimpleNamespace(
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
        )

        backend = WindowsSendInputBackend(
            fake_target,
            {"look": {"base_delta": 10, "tick_ms": 10, "scale_x": 1.0, "scale_y": 1.0}},
        )

        with (
            patch("plans.aura_base.src.platform.windows.input_backends._send_mouse_input") as send_mouse,
            patch("plans.aura_base.src.platform.windows.input_backends.time.sleep"),
        ):
            backend.look_hold(0.5, 0.0, duration_ms=25, tick_ms=10)

        self.assertEqual(send_mouse.call_count, 3)
        for call in send_mouse.call_args_list:
            self.assertEqual(call.kwargs, {"flags": win32con.MOUSEEVENTF_MOVE, "dx": 5, "dy": 0})

    def test_window_message_look_delta_raises_unsupported(self):
        fake_target = SimpleNamespace(
            binding=SimpleNamespace(
                class_name="Notepad",
                process_name="game.exe",
                title="My Game",
            ),
            get_client_rect=lambda: (0, 0, 100, 60),
            get_client_rect_screen=lambda: (10, 20, 100, 60),
            focus=lambda: True,
            to_summary=lambda: {"hwnd": 100},
            hwnd=100,
        )

        backend = WindowsWindowMessageInputBackend(fake_target, {"window_message_allow_unsupported": True})

        with self.assertRaises(TargetRuntimeError) as cm:
            backend.look_delta(20, 0)

        self.assertEqual(cm.exception.code, "input_capability_unsupported")

    def test_target_runtime_rebinds_after_window_lost(self):
        runtime = TargetRuntimeService(
            _FakeConfig(
                {
                    "runtime": {
                        "family": "windows_desktop",
                        "provider": "windows",
                        "target": {"mode": "hwnd", "hwnd": 100},
                        "capture": {"backend": "gdi"},
                        "input": {"backend": "sendinput"},
                        "rebind": {
                            "enabled": True,
                            "max_attempts": 1,
                            "retry_delay_ms": 0,
                            "error_codes": ["window_target_lost"],
                        },
                    }
                }
            )
        )

        class _BrokenAdapter:
            def close(self):
                return None

            def focus(self):
                raise TargetRuntimeError("window_target_lost", "lost")

        class _HealthyAdapter:
            def close(self):
                return None

            def focus(self):
                return True

        adapters = [_BrokenAdapter(), _HealthyAdapter()]
        runtime._build_session = lambda resolved: adapters.pop(0)  # type: ignore[assignment]

        self.assertTrue(runtime.focus())

    def test_windows_diagnostics_probe_capture_backend_uses_requested_backend(self):
        from unittest.mock import patch
        from plans.aura_base.src.services.windows_diagnostics_service import WindowsDiagnosticsService

        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "title", "title": "My Game"},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                }
            }
        )
        runtime = TargetRuntimeService(config)
        diagnostics = WindowsDiagnosticsService(config, runtime)

        fake_target = SimpleNamespace(
            hwnd=100,
            to_summary=lambda: {"hwnd": 100, "title": "My Game"},
            get_client_rect=lambda: (0, 0, 100, 60),
        )
        fake_capture = SimpleNamespace(
            image_size=(100, 60),
            relative_rect=(0, 0, 100, 60),
            quality_flags=[],
        )
        fake_backend = SimpleNamespace(
            capture=lambda rect=None: fake_capture,
            self_check=lambda: {"ok": True, "backend": "wgc"},
            close=lambda: None,
        )

        with (
            patch("plans.aura_base.src.services.windows_diagnostics_service.resolve_window_candidate") as resolve_candidate,
            patch("plans.aura_base.src.services.windows_diagnostics_service.WindowTarget.create", return_value=fake_target),
            patch("plans.aura_base.src.services.windows_diagnostics_service.build_capture_backend", return_value=fake_backend) as build_capture,
        ):
            resolve_candidate.return_value = SimpleNamespace(
                hwnd=100,
                client_rect=(0, 0, 100, 60),
                to_dict=lambda: {"hwnd": 100},
            )
            result = diagnostics.probe_capture_backend(backend="wgc")

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "wgc")
        self.assertEqual(build_capture.call_args.args[0], "wgc")

    def test_windows_diagnostics_transform_reference_rect_scales_to_client(self):
        from unittest.mock import patch
        from plans.aura_base.src.services.windows_diagnostics_service import WindowsDiagnosticsService

        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "title", "title": "My Game"},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                    "coordinates": {
                        "mode": "reference_client",
                        "reference_resolution": [1600, 900],
                    },
                }
            }
        )
        diagnostics = WindowsDiagnosticsService(config, TargetRuntimeService(config))

        with patch("plans.aura_base.src.services.windows_diagnostics_service.resolve_window_candidate") as resolve_candidate:
            resolve_candidate.return_value = SimpleNamespace(
                client_rect=(0, 0, 800, 450),
            )
            result = diagnostics.transform_reference_rect((160, 90, 320, 180))

        self.assertEqual(result["client_rect"], [80, 45, 160, 90])

    def test_windows_diagnostics_show_dpi_info_uses_current_target(self):
        from unittest.mock import patch
        from plans.aura_base.src.services.windows_diagnostics_service import WindowsDiagnosticsService

        config = _FakeConfig(
            {
                "runtime": {
                    "family": "windows_desktop",
                    "provider": "windows",
                    "target": {"mode": "title", "title": "My Game"},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                }
            }
        )
        diagnostics = WindowsDiagnosticsService(config, TargetRuntimeService(config))

        with (
            patch("plans.aura_base.src.services.windows_diagnostics_service.resolve_window_candidate") as resolve_candidate,
            patch("plans.aura_base.src.services.windows_diagnostics_service.ensure_process_dpi_awareness", return_value={"ok": True, "mode": "per_monitor_v2"}),
            patch("plans.aura_base.src.services.windows_diagnostics_service.get_window_dpi", return_value=144),
            patch("plans.aura_base.src.services.windows_diagnostics_service.get_window_scale_factor", return_value=1.5),
            patch("plans.aura_base.src.services.windows_diagnostics_service.get_monitor_scale_factor", return_value=1.5),
        ):
            resolve_candidate.return_value = SimpleNamespace(
                hwnd=100,
                monitor_index=0,
                to_dict=lambda: {"hwnd": 100, "title": "My Game"},
            )
            result = diagnostics.show_dpi_info()

        self.assertEqual(result["window_dpi"], 144)
        self.assertEqual(result["window_scale_factor"], 1.5)
        self.assertEqual(result["monitor_scale_factor"], 1.5)

    def test_navigation_service_rotate_camera_uses_look_delta(self):
        from plans.aura_base.src.services.navigation_service import NavigationService

        look_calls = []
        service = NavigationService.__new__(NavigationService)
        service.app = SimpleNamespace(look_delta=lambda dx, dy: look_calls.append((dx, dy)))

        service._rotate_camera_dynamic(85.0)
        service._rotate_camera_dynamic(-12.0)
        service._rotate_camera_dynamic(3.0)

        self.assertEqual(look_calls, [(150, 0), (-10, 0)])

    def test_doctor_includes_runtime_target_summary(self):
        from unittest.mock import patch

        runner = EmbeddedGameRunner()
        runner._ensure_runtime = lambda: SimpleNamespace(actions=[], get_all_services_for_api=lambda: [])  # type: ignore[assignment]
        runner.list_games = lambda include_shared=True: []  # type: ignore[assignment]
        runner.status = lambda: {"ready": True}  # type: ignore[assignment]

        class _FakeScreen:
            def self_check(self):
                return {
                    "ok": True,
                    "provider": "windows",
                    "target": {"mode": "title", "hwnd": 100},
                    "capture": {"backend": "gdi"},
                    "input": {"backend": "sendinput"},
                }

        with patch("packages.aura_game.runner.service_registry.get_service_instance", return_value=_FakeScreen()):
            result = runner.doctor()

        self.assertIn("runtime_target", result)
        self.assertEqual(result["runtime_target"]["provider"], "windows")


if __name__ == "__main__":
    unittest.main()
