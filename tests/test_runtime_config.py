from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import yaml

from packages.aura_core.config.service import ConfigService
from packages.aura_core.context.plan import current_plan_name
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.runtime_config import resolve_runtime_config


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


class TestRuntimeConfig(unittest.TestCase):
    def test_plan_config_overrides_global_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            with open(base / "config.yaml", "w", encoding="utf-8") as handle:
                yaml.safe_dump({"runtime": {"provider": "windows", "capture": {"backend": "gdi"}}}, handle)

            service = ConfigService()
            service.load_environment_configs(base)
            service.register_plan_config("demo", {"runtime": {"provider": "mumu", "capture": {"backend": "scrcpy_stream"}}})

            token = current_plan_name.set("demo")
            try:
                self.assertEqual(service.get("runtime.provider"), "mumu")
                self.assertEqual(service.get("runtime.capture.backend"), "scrcpy_stream")
            finally:
                current_plan_name.reset(token)

    def test_environment_overrides_plan_and_global_config(self):
        previous = os.environ.get("AURA_RUNTIME_PROVIDER")
        os.environ["AURA_RUNTIME_PROVIDER"] = "windows"
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                base = Path(temp_dir)
                with open(base / "config.yaml", "w", encoding="utf-8") as handle:
                    yaml.safe_dump({"runtime": {"provider": "mumu"}}, handle)

                service = ConfigService()
                service.load_environment_configs(base)
                service.register_plan_config("demo", {"runtime": {"provider": "mumu"}})

                token = current_plan_name.set("demo")
                try:
                    self.assertEqual(service.get("runtime.provider"), "windows")
                finally:
                    current_plan_name.reset(token)
        finally:
            if previous is None:
                os.environ.pop("AURA_RUNTIME_PROVIDER", None)
            else:
                os.environ["AURA_RUNTIME_PROVIDER"] = previous

    def test_unified_windows_runtime_config_parses(self):
        resolved = resolve_runtime_config(
            _FakeConfig(
                {
                    "runtime": {
                        "family": "windows_desktop",
                        "provider": "windows",
                        "target": {
                            "mode": "title",
                            "title": "My Game",
                            "title_exact": True,
                        },
                        "capture": {
                            "backend": "printwindow",
                            "crop_to_client": True,
                            "candidates": [
                                {"backend": "wgc"},
                                {"backend": "dxgi"},
                                {"backend": "gdi"},
                            ],
                        },
                        "input": {
                            "backend": "window_message",
                            "focus_before_input": False,
                            "activation": {
                                "mode": "focus_click_sleep",
                                "sleep_ms": 420,
                                "click_point": [321, 222],
                                "click_button": "right",
                            },
                            "look": {
                                "tick_ms": 12,
                                "base_delta": 32,
                                "max_delta_per_tick": 88,
                                "scale_x": 1.25,
                                "scale_y": 0.75,
                                "invert_y": True,
                            },
                        },
                        "rebind": {
                            "enabled": True,
                            "max_attempts": 2,
                            "retry_delay_ms": 450,
                            "error_codes": ["window_target_lost", "window_not_found"],
                        },
                        "coordinates": {
                            "mode": "reference_client",
                            "reference_resolution": [1600, 900],
                        },
                        "window_spec": {
                            "mode": "require_exact",
                            "client_size": [1600, 900],
                            "position": [120, 80],
                            "monitor_index": 0,
                        },
                        "gamepad": {
                            "enabled": True,
                            "backend": "vgamepad",
                            "device_type": "xbox360",
                            "auto_connect": False,
                            "update_delay_ms": 8,
                        },
                        "debug": {
                            "capture_on_error": True,
                            "dump_window_summary_on_error": True,
                            "save_ocr_artifacts": False,
                            "input_trace_size": 16,
                            "artifact_dir": "logs/test_artifacts",
                        },
                    }
                }
            )
        )

        self.assertEqual(resolved.provider, "windows")
        self.assertEqual(resolved.target.mode, "title")
        self.assertEqual(resolved.target.title, "My Game")
        self.assertEqual(resolved.capture.backend, "printwindow")
        self.assertEqual(
            resolved.capture.candidates,
            (
                {"backend": "wgc"},
                {"backend": "dxgi"},
                {"backend": "gdi"},
            ),
        )
        self.assertEqual(resolved.input.backend, "window_message")
        self.assertEqual(resolved.input.look.tick_ms, 12)
        self.assertEqual(resolved.input.look.base_delta, 32)
        self.assertEqual(resolved.input.look.max_delta_per_tick, 88)
        self.assertEqual(resolved.input.look.scale_x, 1.25)
        self.assertEqual(resolved.input.look.scale_y, 0.75)
        self.assertTrue(resolved.input.look.invert_y)
        self.assertEqual(resolved.input.activation.mode, "focus_click_sleep")
        self.assertEqual(resolved.input.activation.sleep_ms, 420)
        self.assertEqual(resolved.input.activation.click_point, (321, 222))
        self.assertEqual(resolved.input.activation.click_button, "right")
        self.assertTrue(resolved.rebind.enabled)
        self.assertEqual(resolved.rebind.max_attempts, 2)
        self.assertEqual(resolved.rebind.retry_delay_ms, 450)
        self.assertEqual(resolved.rebind.error_codes, ("window_target_lost", "window_not_found"))
        self.assertEqual(resolved.coordinates.mode, "reference_client")
        self.assertEqual(resolved.coordinates.reference_resolution, (1600, 900))
        self.assertEqual(resolved.window_spec.mode, "require_exact")
        self.assertEqual(resolved.window_spec.client_size, (1600, 900))
        self.assertEqual(resolved.window_spec.position, (120, 80))
        self.assertEqual(resolved.window_spec.monitor_index, 0)
        self.assertTrue(resolved.gamepad.enabled)
        self.assertEqual(resolved.gamepad.device_type, "xbox360")
        self.assertFalse(resolved.gamepad.auto_connect)
        self.assertEqual(resolved.gamepad.update_delay_ms, 8)
        self.assertTrue(resolved.debug.capture_on_error)
        self.assertTrue(resolved.debug.dump_window_summary_on_error)
        self.assertEqual(resolved.debug.input_trace_size, 16)
        self.assertEqual(resolved.debug.artifact_dir, "logs/test_artifacts")

    def test_unified_mumu_runtime_config_parses(self):
        resolved = resolve_runtime_config(
            _FakeConfig(
                {
                    "runtime": {
                        "family": "android_emulator",
                        "provider": "mumu",
                        "target": {
                            "mode": "adb_serial",
                            "adb_serial": "127.0.0.1:16384",
                            "connect_on_start": True,
                        },
                        "capture": {
                            "backend": "scrcpy_stream",
                            "mumu": {"module_name": "scrcpy"},
                        },
                        "input": {
                            "backend": "android_touch",
                            "mumu": {
                                "remote_dir": "/data/local/tmp/aura",
                                "helper_path": "android_touch",
                                "path_fps": 15,
                                "key_input_provider": "adb",
                                "text_input_provider": "adb",
                            },
                        },
                    }
                }
            )
        )

        self.assertEqual(resolved.provider, "mumu")
        self.assertEqual(resolved.target.mode, "adb_serial")
        self.assertEqual(resolved.target.adb_serial, "127.0.0.1:16384")
        self.assertEqual(resolved.capture.backend, "scrcpy_stream")
        self.assertEqual(resolved.input.backend, "android_touch")
        self.assertEqual(resolved.input.mumu["path_fps"], 15)

    def test_legacy_target_config_maps_to_unified_runtime_with_warning(self):
        resolved = resolve_runtime_config(
            _FakeConfig(
                {
                    "target": {
                        "provider": "mumu",
                        "mumu": {
                            "adb": {
                                "serial": "127.0.0.1:16384",
                                "connect_on_start": True,
                            },
                            "capture": {"module_name": "scrcpy"},
                            "input": {"path_fps": 12},
                            "key_input": {"provider": "adb"},
                            "text_input": {"provider": "adb"},
                        },
                    }
                }
            )
        )

        self.assertEqual(resolved.provider, "mumu")
        self.assertEqual(resolved.target.mode, "adb_serial")
        self.assertEqual(resolved.target.adb_serial, "127.0.0.1:16384")
        self.assertEqual(resolved.capture.backend, "scrcpy_stream")
        self.assertEqual(resolved.input.backend, "android_touch")
        self.assertTrue(any("Legacy target.provider" in item for item in resolved.warnings))
        self.assertTrue(any("Legacy target.mumu" in item for item in resolved.warnings))

    def test_invalid_provider_capture_combination_raises(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {"backend": "scrcpy_stream"},
                            "input": {"backend": "sendinput"},
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "capture_backend_invalid_for_provider")

    def test_invalid_provider_input_combination_raises(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "android_emulator",
                            "provider": "mumu",
                            "target": {"mode": "auto", "adb_serial": "auto"},
                            "capture": {"backend": "scrcpy_stream"},
                            "input": {"backend": "sendinput"},
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "input_backend_invalid_for_provider")

    def test_invalid_look_tick_raises(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {"backend": "gdi"},
                            "input": {
                                "backend": "sendinput",
                                "look": {"tick_ms": 0},
                            },
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "look_tick_invalid")

    def test_invalid_look_scale_raises(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {"backend": "gdi"},
                            "input": {
                                "backend": "sendinput",
                                "look": {"scale_x": 0},
                            },
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "look_scale_invalid")

    def test_invalid_activation_mode_raises(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {"backend": "gdi"},
                            "input": {
                                "backend": "sendinput",
                                "activation": {"mode": "unknown"},
                            },
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "activation_mode_invalid")

    def test_invalid_capture_candidate_requires_backend(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {
                                "backend": "gdi",
                                "candidates": [{"module_name": "foo"}],
                            },
                            "input": {"backend": "sendinput"},
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "capture_candidate_invalid")

    def test_reference_client_mode_requires_resolution(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {"backend": "gdi"},
                            "input": {"backend": "sendinput"},
                            "coordinates": {"mode": "reference_client"},
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "coordinate_reference_required")

    def test_invalid_window_spec_mode_raises(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {"backend": "gdi"},
                            "input": {"backend": "sendinput"},
                            "window_spec": {"mode": "unknown"},
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "window_spec_mode_invalid")

    def test_invalid_gamepad_device_type_raises(self):
        with self.assertRaises(TargetRuntimeError) as cm:
            resolve_runtime_config(
                _FakeConfig(
                    {
                        "runtime": {
                            "family": "windows_desktop",
                            "provider": "windows",
                            "target": {"mode": "hwnd", "hwnd": 100},
                            "capture": {"backend": "gdi"},
                            "input": {"backend": "sendinput"},
                            "gamepad": {"enabled": True, "device_type": "weird"},
                        }
                    }
                )
            )

        self.assertEqual(cm.exception.code, "gamepad_device_type_invalid")


if __name__ == "__main__":
    unittest.main()
