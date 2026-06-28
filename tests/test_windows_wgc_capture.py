from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.runtime_config import RuntimeCaptureConfig, RuntimeInputConfig, RuntimeTargetConfig
from plans.aura_base.src.platform.windows.capture_backends import WindowsWgcCaptureBackend
from plans.aura_base.src.platform.windows.wgc_session import PersistentWgcSession
from plans.aura_base.src.services.target_runtime_service import TargetRuntimeService
from plans.aura_base.src.services.windows_diagnostics_service import WindowsDiagnosticsService


class _FakeControl:
    def __init__(self) -> None:
        self.finished = False
        self.stop_calls = 0
        self.wait_calls = 0

    def is_finished(self) -> bool:
        return bool(self.finished)

    def stop(self) -> None:
        self.stop_calls += 1
        self.finished = True

    def wait(self) -> None:
        self.wait_calls += 1


class _FakeFrameControl:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeFrame:
    def __init__(self, frame_buffer: np.ndarray) -> None:
        self.frame_buffer = frame_buffer


class _FakeWindowsCapture:
    instances: list["_FakeWindowsCapture"] = []
    frame_factory = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.frame_handler = None
        self.closed_handler = None
        self.start_calls = 0
        self.control = _FakeControl()
        self.last_frame_control: _FakeFrameControl | None = None
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.frame_factory = None

    def event(self, handler):
        if handler.__name__ == "on_frame_arrived":
            self.frame_handler = handler
        elif handler.__name__ == "on_closed":
            self.closed_handler = handler
        else:
            raise AssertionError(f"unexpected handler {handler.__name__}")
        return handler

    def start_free_threaded(self):
        self.start_calls += 1
        if callable(type(self).frame_factory):
            self.emit_frame(type(self).frame_factory())
        return self.control

    def emit_frame(self, frame: np.ndarray) -> _FakeFrameControl:
        if self.frame_handler is None:
            raise AssertionError("frame handler is not registered")
        frame_control = _FakeFrameControl()
        self.last_frame_control = frame_control
        self.frame_handler(_FakeFrame(frame), frame_control)
        return frame_control

    def emit_closed(self) -> None:
        if self.closed_handler is None:
            raise AssertionError("closed handler is not registered")
        self.closed_handler()


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


class _FakeProcess:
    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def memory_info(self):
        value = self._values.pop(0)
        return SimpleNamespace(private_usage=int(value * 1024 * 1024), rss=int(value * 1024 * 1024))


class TestPersistentWgcSession(unittest.TestCase):
    def setUp(self) -> None:
        _FakeWindowsCapture.reset()
        self._patcher = patch(
            "plans.aura_base.src.platform.windows.wgc_session.importlib.import_module",
            return_value=SimpleNamespace(WindowsCapture=_FakeWindowsCapture),
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_reuses_single_slot_buffer_and_reallocates_on_shape_change(self):
        session = PersistentWgcSession(hwnd=100, module_name="windows_capture")
        session.start_if_needed()
        capture = _FakeWindowsCapture.instances[-1]

        frame_one = np.zeros((2, 3, 4), dtype=np.uint8)
        capture.emit_frame(frame_one)
        session.wait_for_fresh_frame(max_stale_ms=100, timeout_ms=100)
        first_snapshot = session.snapshot_full_frame()
        first_buffer_id = id(session._latest_frame)

        frame_two = np.ones((2, 3, 4), dtype=np.uint8)
        capture.emit_frame(frame_two)
        session.wait_for_fresh_frame(max_stale_ms=100, timeout_ms=100)
        second_buffer_id = id(session._latest_frame)

        frame_three = np.full((4, 5, 4), 7, dtype=np.uint8)
        capture.emit_frame(frame_three)
        session.wait_for_fresh_frame(max_stale_ms=100, timeout_ms=100)
        third_snapshot = session.snapshot_full_frame()
        third_buffer_id = id(session._latest_frame)

        self.assertEqual(capture.start_calls, 1)
        self.assertEqual(first_buffer_id, second_buffer_id)
        self.assertNotEqual(second_buffer_id, third_buffer_id)
        self.assertEqual(first_snapshot.shape, (2, 3, 4))
        self.assertEqual(third_snapshot.shape, (4, 5, 4))
        self.assertEqual(session.health()["generation"], 3)
        self.assertEqual(session.health()["latest_frame_shape"], [4, 5, 4])

    def test_closed_session_rejects_reads(self):
        session = PersistentWgcSession(hwnd=100, module_name="windows_capture")
        session.start_if_needed()
        capture = _FakeWindowsCapture.instances[-1]
        capture.emit_closed()

        with self.assertRaises(TargetRuntimeError) as cm:
            session.wait_for_fresh_frame(max_stale_ms=100, timeout_ms=50)

        self.assertEqual(cm.exception.code, "windows_capture_session_closed")

    def test_requires_windows_capture_api(self):
        with patch.object(PersistentWgcSession, "_import_module", return_value=SimpleNamespace()):
            with self.assertRaises(TargetRuntimeError) as cm:
                PersistentWgcSession(hwnd=100, module_name="windows_capture")

        self.assertEqual(cm.exception.code, "windows_capture_api_missing")


class TestWindowsWgcCaptureBackend(unittest.TestCase):
    def setUp(self) -> None:
        _FakeWindowsCapture.reset()
        _FakeWindowsCapture.frame_factory = lambda: np.full((2, 3, 4), 9, dtype=np.uint8)
        self._patcher = patch(
            "plans.aura_base.src.platform.windows.wgc_session.importlib.import_module",
            return_value=SimpleNamespace(WindowsCapture=_FakeWindowsCapture),
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _build_target(self):
        return SimpleNamespace(
            hwnd=100,
            ensure_valid=lambda: None,
            get_client_rect=lambda: (0, 0, 3, 2),
            get_client_rect_screen=lambda: (10, 20, 3, 2),
            get_window_rect_screen=lambda: (10, 20, 3, 2),
            focus=lambda: True,
        )

    def test_reuses_single_persistent_session_across_captures(self):
        target = self._build_target()
        backend = WindowsWgcCaptureBackend(target, {"frame_timeout_ms": 100, "max_stale_ms": 100})

        first = backend.capture()
        second = backend.capture()

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(len(_FakeWindowsCapture.instances), 1)
        self.assertEqual(_FakeWindowsCapture.instances[0].start_calls, 1)
        self.assertEqual(first.image.shape[:2], (2, 3))

    def test_rebuilds_session_when_target_hwnd_changes(self):
        target = self._build_target()
        backend = WindowsWgcCaptureBackend(target, {"frame_timeout_ms": 100, "max_stale_ms": 100})

        backend.capture()
        target.hwnd = 200
        backend.capture()

        self.assertEqual(len(_FakeWindowsCapture.instances), 2)
        self.assertEqual(_FakeWindowsCapture.instances[0].kwargs["window_hwnd"], 100)
        self.assertEqual(_FakeWindowsCapture.instances[1].kwargs["window_hwnd"], 200)

    def test_rebuilds_session_after_control_stops(self):
        target = self._build_target()
        backend = WindowsWgcCaptureBackend(target, {"frame_timeout_ms": 100, "max_stale_ms": 100})

        backend.capture()
        first_capture = _FakeWindowsCapture.instances[0]
        first_capture.control.finished = True
        backend.capture()

        self.assertEqual(len(_FakeWindowsCapture.instances), 2)
        self.assertEqual(_FakeWindowsCapture.instances[0].start_calls, 1)
        self.assertEqual(_FakeWindowsCapture.instances[1].start_calls, 1)


class TestWindowsCaptureDiagnostics(unittest.TestCase):
    def test_stress_capture_backend_collects_memory_samples(self):
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
        )
        fake_backend = SimpleNamespace(
            capture=lambda rect=None: SimpleNamespace(
                image_size=(100, 60),
                relative_rect=(0, 0, 100, 60),
            ),
            self_check=lambda: {"ok": True, "backend": "wgc", "session_mode": "persistent"},
            close=lambda: None,
        )
        fake_process = _FakeProcess([100.0, 110.0, 130.0, 105.0])

        with (
            patch("plans.aura_base.src.services.windows_diagnostics_service.WindowTarget.create", return_value=fake_target),
            patch("plans.aura_base.src.services.windows_diagnostics_service.build_capture_backend", return_value=fake_backend),
            patch("plans.aura_base.src.services.windows_diagnostics_service.psutil.Process", return_value=fake_process),
            patch("plans.aura_base.src.services.windows_diagnostics_service.time.sleep"),
        ):
            result = diagnostics.stress_capture_backend(
                backend="wgc",
                iterations=2,
                interval_ms=0,
                settle_after_close_ms=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "wgc")
        self.assertEqual(result["baseline_private_mb"], 100.0)
        self.assertEqual(result["peak_private_mb"], 130.0)
        self.assertEqual(result["end_private_mb"], 130.0)
        self.assertEqual(result["after_close_private_mb"], 105.0)
        self.assertEqual(result["delta_mb"], 30.0)
        self.assertEqual(len(result["samples"]), 2)
        self.assertEqual(result["backend_health"]["session_mode"], "persistent")
