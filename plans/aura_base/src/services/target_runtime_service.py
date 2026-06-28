# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from packages.aura_core.api import service_info
from packages.aura_core.config.service import ConfigService
from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.observability.logging.core_logger import logger

from ..platform.contracts import CaptureResult, RuntimeAdapter, TargetRuntimeError
from ..platform.windows.debug_artifacts import DebugArtifactsManager
from ..platform.mumu.adb_discovery import AdbController
from ..platform.mumu.android_touch_input import MuMuAndroidTouchInputBackend
from ..platform.mumu.helper_manager import AndroidTouchHelperManager
from ..platform.mumu.scrcpy_capture import MuMuScrcpyCaptureBackend
from ..platform.mumu.session import MuMuSession
from ..platform.runtime_config import (
    ResolvedRuntimeConfig,
    resolve_runtime_config,
    supported_capture_backends,
)
from ..platform.windows import WindowsDesktopAdapter


@service_info(alias="target_runtime", public=False, singleton=True, deps={"config": "core/config"})
class TargetRuntimeService:
    def __init__(self, config: ConfigService):
        self.config = config
        self._lock = threading.RLock()
        self._session: Optional[RuntimeAdapter] = None
        self._session_key: Optional[str] = None
        self._emitted_runtime_warnings: set[str] = set()
        self._debug_artifacts = DebugArtifactsManager(config)

    def list_capture_backends(self) -> dict[str, Any]:
        resolved = self._resolve_runtime_config()
        return {
            "available": supported_capture_backends(resolved.provider),
            "enabled": [resolved.capture.backend],
            "default": resolved.capture.backend,
            "configured": True,
        }

    def set_capture_backend(self, backend: str):
        resolved = self._resolve_runtime_config()
        normalized = str(backend or "").strip().lower()
        if normalized != resolved.capture.backend:
            raise TargetRuntimeError(
                "backend_runtime_switch_unsupported",
                "Runtime capture backend switching is unsupported; update config.yaml and rebuild the runtime.",
                {"requested": normalized or None, "configured": resolved.capture.backend},
            )
        return self._call_session("set_capture_backend", normalized)

    def capture(self, rect: tuple[int, int, int, int] | None = None, backend: Optional[str] = None) -> CaptureResult:
        if backend is not None:
            self.set_capture_backend(backend)
        return self._call_session("capture", rect)

    def get_client_rect(self) -> tuple[int, int, int, int] | None:
        return self._call_session("get_client_rect")

    def target_summary(self) -> dict[str, Any]:
        resolved = self._resolve_runtime_config()
        session = self._get_or_create_session(resolved=resolved)
        target = getattr(session, "target", None)
        if target is not None and hasattr(target, "to_summary"):
            return dict(target.to_summary())
        try:
            session_check = dict(session.self_check() or {})
        except Exception:
            session_check = {}
        target_payload = session_check.get("target")
        if isinstance(target_payload, dict):
            return dict(target_payload)
        client_rect = self.get_client_rect()
        return {"client_rect": list(client_rect) if client_rect is not None else None}

    def get_pixel_color_at(self, x: int, y: int) -> tuple[int, int, int]:
        return self._call_session("get_pixel_color_at", x, y)

    def focus(self) -> bool:
        return bool(self._call_session("focus"))

    def focus_with_input(self, click_delay: float = 0.3) -> bool:
        self._record_debug_input_event("focus_with_input", {"click_delay": click_delay})
        return bool(self._call_session("focus_with_input", click_delay))

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ):
        self._record_debug_input_event(
            "click",
            {"x": x, "y": y, "button": button, "clicks": clicks, "interval": interval},
        )
        return self._call_session("click", x, y, button=button, clicks=clicks, interval=interval)

    def move_to(self, x: int, y: int, *, duration: float | None = None):
        self._record_debug_input_event("move_to", {"x": x, "y": y, "duration": duration})
        return self._call_session("move_to", x, y, duration=duration)

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None):
        self._record_debug_input_event("move_relative", {"dx": dx, "dy": dy, "duration": duration})
        return self._call_session("move_relative", dx, dy, duration=duration)

    def mouse_down(self, *, button: str = "left"):
        self._record_debug_input_event("mouse_down", {"button": button})
        return self._call_session("mouse_down", button=button)

    def mouse_up(self, *, button: str = "left"):
        self._record_debug_input_event("mouse_up", {"button": button})
        return self._call_session("mouse_up", button=button)

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None):
        self._record_debug_input_event("drag_to", {"x": x, "y": y, "button": button, "duration": duration})
        return self._call_session("drag_to", x, y, button=button, duration=duration)

    def look_delta(self, dx: int, dy: int):
        self._record_debug_input_event("look_delta", {"dx": dx, "dy": dy})
        return self._call_session("look_delta", dx, dy)

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ):
        self._record_debug_input_event(
            "look_hold",
            {"vx": vx, "vy": vy, "duration_ms": duration_ms, "tick_ms": tick_ms},
        )
        return self._call_session("look_hold", vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)

    def scroll(self, amount: int, direction: str = "down"):
        self._record_debug_input_event("scroll", {"amount": amount, "direction": direction})
        return self._call_session("scroll", amount, direction)

    def press_key(self, key: str, presses: int = 1, interval: float | None = None):
        self._record_debug_input_event("press_key", {"key": key, "presses": presses, "interval": interval})
        return self._call_session("press_key", key, presses, interval)

    def key_down(self, key: str):
        self._record_debug_input_event("key_down", {"key": key})
        return self._call_session("key_down", key)

    def key_up(self, key: str):
        self._record_debug_input_event("key_up", {"key": key})
        return self._call_session("key_up", key)

    def type_text(self, text: str, interval: float | None = None):
        self._record_debug_input_event("type_text", {"text": text, "interval": interval})
        return self._call_session("type_text", text, interval)

    def release_all(self):
        self._record_debug_input_event("release_all", {})
        return self._call_session("release_all")

    def input_capabilities(self) -> dict[str, Any]:
        return dict(self._call_session("capabilities") or {})

    def test_focus_activation(
        self,
        *,
        mode: str | None = None,
        sleep_ms: int | None = None,
        click_point: tuple[int, int] | None = None,
        click_button: str | None = None,
    ) -> dict[str, Any]:
        return dict(
            self._call_session(
                "test_focus_activation",
                mode=mode,
                sleep_ms=sleep_ms,
                click_point=click_point,
                click_button=click_button,
            )
            or {}
        )

    def self_check(self) -> dict[str, Any]:
        try:
            resolved = self._resolve_runtime_config()
            session_check = self._get_or_create_session(resolved=resolved).self_check()
        except TargetRuntimeError as exc:
            return {
                "ok": False,
                "provider": None,
                "code": exc.code,
                "message": exc.message,
                "detail": exc.detail,
            }

        target_summary = dict(resolved.target.to_dict())
        if isinstance(session_check.get("target"), dict):
            target_summary = dict(session_check["target"])
        elif resolved.provider == "mumu" and session_check.get("serial"):
            target_summary["resolved_serial"] = session_check.get("serial")

        return {
            "ok": bool(session_check.get("ok", True)),
            "provider": resolved.provider,
            "family": resolved.family,
            "target": target_summary,
            "capture": {
                "backend": resolved.capture.backend,
                "health": session_check.get("capture"),
            },
            "input": {
                "backend": resolved.input.backend,
                "health": session_check.get("input"),
                "capabilities": session_check.get("capabilities") or self.input_capabilities(),
            },
            "warnings": list(resolved.warnings),
            "session": session_check,
        }

    def _call_session(self, method_name: str, *args, **kwargs):
        resolved = self._resolve_runtime_config()
        session = self._get_or_create_session(resolved=resolved)
        last_error: TargetRuntimeError | None = None
        try:
            return getattr(session, method_name)(*args, **kwargs)
        except TargetRuntimeError as exc:
            if not self._should_rebind(exc, resolved):
                self._debug_artifacts.capture_error_artifacts(
                    method_name=method_name,
                    exc=exc,
                    session=session,
                    extra={"args": list(args), "kwargs": dict(kwargs)},
                )
                raise
            last_error = exc
        attempts = max(int(resolved.rebind.max_attempts), 0)
        for attempt in range(attempts):
            if resolved.rebind.retry_delay_ms > 0:
                time.sleep(float(resolved.rebind.retry_delay_ms) / 1000.0)
            logger.warning(
                "Retrying runtime session after %s for method '%s' (attempt %s/%s).",
                last_error.code,
                method_name,
                attempt + 1,
                attempts,
            )
            rebuilt = self._get_or_create_session(resolved=resolved, force_rebuild=True)
            try:
                return getattr(rebuilt, method_name)(*args, **kwargs)
            except TargetRuntimeError as retry_exc:
                last_error = retry_exc
                if not self._should_rebind(retry_exc, resolved):
                    self._debug_artifacts.capture_error_artifacts(
                        method_name=method_name,
                        exc=retry_exc,
                        session=rebuilt,
                        extra={"args": list(args), "kwargs": dict(kwargs), "rebind_attempted": True},
                    )
                    raise
        assert last_error is not None
        self._debug_artifacts.capture_error_artifacts(
            method_name=method_name,
            exc=last_error,
            session=self._session,
            extra={"args": list(args), "kwargs": dict(kwargs), "rebind_attempted": True},
        )
        raise last_error

    def _record_debug_input_event(self, event_name: str, payload: dict[str, Any]) -> None:
        self._debug_artifacts.record_input_event(event_name, payload)

    def _should_rebind(self, exc: TargetRuntimeError, resolved: ResolvedRuntimeConfig) -> bool:
        if not resolved.rebind.enabled:
            return False
        if resolved.provider != "windows":
            return False
        return str(exc.code or "") in set(resolved.rebind.error_codes)

    def _resolve_runtime_config(self) -> ResolvedRuntimeConfig:
        resolved = resolve_runtime_config(self.config)
        for message in resolved.warnings:
            if message in self._emitted_runtime_warnings:
                continue
            self._emitted_runtime_warnings.add(message)
            logger.warning(message)
        return resolved

    def _get_or_create_session(self, *, resolved: ResolvedRuntimeConfig | None = None, force_rebuild: bool = False):
        resolved = resolved or self._resolve_runtime_config()
        session_key = self._build_session_key(resolved)
        with self._lock:
            if force_rebuild or self._session is None or self._session_key != session_key:
                self._close_session_locked()
                self._session = self._build_session(resolved)
                self._session_key = session_key
            return self._session

    def _close_session_locked(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self._session_key = None

    def _build_session_key(self, resolved: ResolvedRuntimeConfig) -> str:
        payload = {
            "game_scope": current_plan_name.get() or "__global__",
            "runtime": resolved.to_dict(),
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)

    def _build_session(self, resolved: ResolvedRuntimeConfig) -> RuntimeAdapter:
        if resolved.provider == "windows":
            return WindowsDesktopAdapter(
                target_config=resolved.target,
                capture_config=resolved.capture,
                input_config=resolved.input,
                window_spec_config=resolved.window_spec,
            )
        if resolved.provider == "mumu":
            return self._build_mumu_session(resolved)
        raise TargetRuntimeError(
            "provider_unsupported",
            "Supported runtime providers are 'windows' and 'mumu'.",
            {"provider": resolved.provider},
        )

    def _build_mumu_session(self, resolved: ResolvedRuntimeConfig) -> MuMuSession:
        preferred_serial = resolved.target.adb_serial if resolved.target.mode == "adb_serial" else "auto"
        adb = AdbController(executable="adb", default_timeout_sec=15.0)
        if preferred_serial != "auto" and resolved.target.connect_on_start and ":" in preferred_serial:
            adb.connect(preferred_serial)

        candidates = adb.list_devices() if preferred_serial == "auto" else [preferred_serial]
        if not candidates:
            raise TargetRuntimeError(
                "adb_device_not_found",
                "No usable adb device is connected for MuMu session.",
                {"serial": preferred_serial},
            )

        errors = []
        for serial in candidates:
            session = None
            try:
                session = self._make_mumu_session(serial, adb, resolved)
                session.ensure_ready()
                return session
            except Exception as exc:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                errors.append({"serial": serial, "error": str(exc)})

        raise TargetRuntimeError(
            "mumu_session_init_failed",
            "Failed to initialize a healthy MuMu session from adb candidates.",
            {"candidates": candidates, "errors": errors},
        )

    def _make_mumu_session(self, serial: str, adb: AdbController, resolved: ResolvedRuntimeConfig) -> MuMuSession:
        capture_cfg = resolved.capture.provider_options("mumu")
        input_cfg = resolved.input.provider_options("mumu")
        display_info = adb.get_display_info(serial)

        capture_backend = MuMuScrcpyCaptureBackend(serial, capture_cfg)
        helper_manager = AndroidTouchHelperManager(adb, serial, input_cfg)
        input_backend = MuMuAndroidTouchInputBackend(
            helper_manager=helper_manager,
            viewport_provider=capture_backend.get_client_rect,
            touch_physical_size=(display_info.physical_width, display_info.physical_height),
            display_rotation=display_info.current_orientation,
            config=input_cfg,
        )
        return MuMuSession(
            serial=serial,
            adb=adb,
            capture_backend=capture_backend,
            input_backend=input_backend,
            key_input_provider=str(input_cfg.get("key_input_provider") or "adb"),
            text_input_provider=str(input_cfg.get("text_input_provider") or "adb"),
            capture_backend_name=resolved.capture.backend,
            input_backend_name=resolved.input.backend,
            timing_config=input_cfg,
        )
