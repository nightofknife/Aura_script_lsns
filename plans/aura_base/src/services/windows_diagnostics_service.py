# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from typing import Any, Mapping, Optional

import psutil

from packages.aura_core.api import service_info
from packages.aura_core.config.service import ConfigService

from ..platform.contracts import TargetRuntimeError
from ..platform.runtime_config import (
    ResolvedRuntimeConfig,
    RuntimeCaptureConfig,
    resolve_runtime_config,
)
from ..platform.windows.capture_backends import build_capture_backend
from ..platform.windows.coordinate_transform import CoordinateTransformConfig, ReferenceCoordinateTransformer
from ..platform.windows.dpi import ensure_process_dpi_awareness, get_monitor_scale_factor, get_window_dpi, get_window_scale_factor
from ..platform.windows.window_selector import (
    describe_window,
    list_window_candidates,
    resolve_window_candidate,
)
from ..platform.windows.window_target import WindowTarget
from ..platform.windows.window_spec import ensure_window_spec, evaluate_window_spec
from .target_runtime_service import TargetRuntimeService


class _OverlayConfig:
    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)

    def get(self, key: str, default: Any = None) -> Any:
        current: Any = self.payload
        for part in str(key).split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current


@service_info(
    alias="windows_diagnostics",
    public=True,
    singleton=True,
    deps={"config": "core/config", "target_runtime": "target_runtime"},
)
class WindowsDiagnosticsService:
    def __init__(self, config: ConfigService, target_runtime: TargetRuntimeService):
        self.config = config
        self.target_runtime = target_runtime

    def list_candidate_windows(
        self,
        *,
        require_visible: bool = True,
        include_children: bool = False,
        include_empty_title: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        candidates = list_window_candidates(
            require_visible=require_visible,
            allow_child_window=include_children,
            allow_empty_title=include_empty_title,
        )
        max_items = max(int(limit), 1)
        return [candidate.to_dict() for candidate in candidates[:max_items]]

    def describe_target_window(self, hwnd: int) -> dict[str, Any]:
        return describe_window(int(hwnd)).to_dict()

    def resolve_target_preview(self, target_overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        resolved = self._resolve_with_target_overrides(target_overrides or {})
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "windows_diagnostics only supports runtime.provider='windows'.",
                {"provider": resolved.provider},
            )
        candidate = resolve_window_candidate(resolved.target)
        return {
            "provider": resolved.provider,
            "family": resolved.family,
            "target": candidate.to_dict(),
            "target_config": resolved.target.to_dict(),
        }

    def test_focus_activation(
        self,
        *,
        mode: str | None = None,
        sleep_ms: int | None = None,
        click_point: tuple[int, int] | None = None,
        click_button: str | None = None,
    ) -> dict[str, Any]:
        resolved = resolve_runtime_config(self.config)
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "windows_diagnostics only supports runtime.provider='windows'.",
                {"provider": resolved.provider},
            )
        result = self.target_runtime.test_focus_activation(
            mode=mode,
            sleep_ms=sleep_ms,
            click_point=click_point,
            click_button=click_button,
        )
        result["capabilities"] = self.target_runtime.input_capabilities()
        return result

    def show_runtime_capabilities(self) -> dict[str, Any]:
        resolved = resolve_runtime_config(self.config)
        return {
            "provider": resolved.provider,
            "family": resolved.family,
            "target": resolved.target.to_dict(),
            "capture": resolved.capture.to_dict(),
            "input": resolved.input.to_dict(),
            "rebind": resolved.rebind.to_dict(),
            "coordinates": resolved.coordinates.to_dict(),
            "window_spec": resolved.window_spec.to_dict(),
            "gamepad": resolved.gamepad.to_dict(),
            "debug": resolved.debug.to_dict(),
            "capabilities": self.target_runtime.input_capabilities(),
        }

    def show_dpi_info(self) -> dict[str, Any]:
        resolved = resolve_runtime_config(self.config)
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "windows_diagnostics only supports runtime.provider='windows'.",
                {"provider": resolved.provider},
            )
        candidate = resolve_window_candidate(resolved.target)
        return {
            "process": ensure_process_dpi_awareness(),
            "window": candidate.to_dict(),
            "window_dpi": get_window_dpi(candidate.hwnd),
            "window_scale_factor": get_window_scale_factor(candidate.hwnd),
            "monitor_scale_factor": get_monitor_scale_factor(candidate.monitor_index),
            "coordinates": resolved.coordinates.to_dict(),
        }

    def transform_reference_point(self, x: int, y: int) -> dict[str, Any]:
        transformer = self._current_transformer()
        client_point = transformer.point_to_client(int(x), int(y))
        reference_point = transformer.point_to_reference(client_point[0], client_point[1])
        return {
            "input_point": [int(x), int(y)],
            "client_point": list(client_point),
            "roundtrip_reference_point": list(reference_point),
            "transform": transformer.describe(),
        }

    def transform_reference_rect(self, rect: tuple[int, int, int, int]) -> dict[str, Any]:
        transformer = self._current_transformer()
        client_rect = transformer.rect_to_client(rect)
        reference_rect = transformer.rect_to_reference(client_rect)
        return {
            "input_rect": list(rect),
            "client_rect": list(client_rect),
            "roundtrip_reference_rect": list(reference_rect),
            "transform": transformer.describe(),
        }

    def probe_capture_backend(
        self,
        *,
        backend: str | None = None,
        rect: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any]:
        resolved = resolve_runtime_config(self.config)
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "windows_diagnostics only supports runtime.provider='windows'.",
                {"provider": resolved.provider},
            )

        requested_backend = str(backend or resolved.capture.backend).strip().lower()
        capture_config = RuntimeCaptureConfig(
            backend=requested_backend,
            max_stale_ms=resolved.capture.max_stale_ms,
            crop_to_client=resolved.capture.crop_to_client,
            capture_cursor=resolved.capture.capture_cursor,
            candidates=resolved.capture.candidates,
            windows=resolved.capture.windows,
            mumu=resolved.capture.mumu,
        )
        target = WindowTarget.create(resolved.target)
        try:
            backend_impl = build_capture_backend(
                requested_backend,
                target,
                capture_config.provider_options("windows"),
            )
        except TargetRuntimeError as exc:
            return {
                "ok": False,
                "backend": requested_backend,
                "error": exc.to_dict(),
                "target": target.to_summary(),
            }

        try:
            capture = backend_impl.capture(rect=rect)
            image_size = list(capture.image_size) if capture.image_size else None
            return {
                "ok": True,
                "backend": requested_backend,
                "target": target.to_summary(),
                "client_rect": list(target.get_client_rect()),
                "capture_rect": list(capture.relative_rect) if capture.relative_rect else None,
                "image_size": image_size,
                "quality_flags": list(capture.quality_flags),
                "health": backend_impl.self_check(),
            }
        except TargetRuntimeError as exc:
            return {
                "ok": False,
                "backend": requested_backend,
                "error": exc.to_dict(),
                "target": target.to_summary(),
                "health": backend_impl.self_check(),
            }
        finally:
            try:
                backend_impl.close()
            except Exception:
                pass

    def stress_capture_backend(
        self,
        *,
        backend: str | None = None,
        iterations: int = 100,
        interval_ms: int = 0,
        settle_after_close_ms: int = 500,
    ) -> dict[str, Any]:
        resolved = resolve_runtime_config(self.config)
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "windows_diagnostics only supports runtime.provider='windows'.",
                {"provider": resolved.provider},
            )

        requested_backend = str(backend or resolved.capture.backend).strip().lower()
        capture_config = RuntimeCaptureConfig(
            backend=requested_backend,
            max_stale_ms=resolved.capture.max_stale_ms,
            crop_to_client=resolved.capture.crop_to_client,
            capture_cursor=resolved.capture.capture_cursor,
            candidates=resolved.capture.candidates,
            windows=resolved.capture.windows,
            mumu=resolved.capture.mumu,
        )
        target = WindowTarget.create(resolved.target)
        process = psutil.Process()
        baseline_private_mb = self._current_process_private_mb(process)
        max_iterations = max(int(iterations), 1)
        sleep_interval_sec = max(int(interval_ms), 0) / 1000.0
        settle_after_close_sec = max(int(settle_after_close_ms), 0) / 1000.0
        samples: list[dict[str, Any]] = []
        peak_private_mb = baseline_private_mb
        end_private_mb = baseline_private_mb
        after_close_private_mb = baseline_private_mb
        backend_health: dict[str, Any] = {}

        try:
            backend_impl = build_capture_backend(
                requested_backend,
                target,
                capture_config.provider_options("windows"),
            )
        except TargetRuntimeError as exc:
            return {
                "ok": False,
                "backend": requested_backend,
                "target": target.to_summary(),
                "samples": samples,
                "baseline_private_mb": baseline_private_mb,
                "peak_private_mb": peak_private_mb,
                "end_private_mb": end_private_mb,
                "after_close_private_mb": after_close_private_mb,
                "delta_mb": round(end_private_mb - baseline_private_mb, 3),
                "backend_health": backend_health,
                "error": exc.to_dict(),
            }

        try:
            started_at = time.monotonic()
            for index in range(max_iterations):
                capture = backend_impl.capture()
                current_private_mb = self._current_process_private_mb(process)
                peak_private_mb = max(peak_private_mb, current_private_mb)
                end_private_mb = current_private_mb
                samples.append(
                    {
                        "iteration": index + 1,
                        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                        "private_mb": current_private_mb,
                        "image_size": list(capture.image_size) if capture.image_size is not None else None,
                        "relative_rect": list(capture.relative_rect) if capture.relative_rect is not None else None,
                    }
                )
                if sleep_interval_sec > 0:
                    time.sleep(sleep_interval_sec)
            backend_health = backend_impl.self_check()
        except TargetRuntimeError as exc:
            backend_health = backend_impl.self_check()
            return {
                "ok": False,
                "backend": requested_backend,
                "target": target.to_summary(),
                "samples": samples,
                "baseline_private_mb": baseline_private_mb,
                "peak_private_mb": peak_private_mb,
                "end_private_mb": end_private_mb,
                "after_close_private_mb": after_close_private_mb,
                "delta_mb": round(end_private_mb - baseline_private_mb, 3),
                "backend_health": backend_health,
                "error": exc.to_dict(),
            }
        finally:
            try:
                backend_impl.close()
            except Exception:
                pass

        if settle_after_close_sec > 0:
            time.sleep(settle_after_close_sec)
        after_close_private_mb = self._current_process_private_mb(process)
        return {
            "ok": True,
            "backend": requested_backend,
            "target": target.to_summary(),
            "samples": samples,
            "baseline_private_mb": baseline_private_mb,
            "peak_private_mb": peak_private_mb,
            "end_private_mb": end_private_mb,
            "after_close_private_mb": after_close_private_mb,
            "delta_mb": round(end_private_mb - baseline_private_mb, 3),
            "backend_health": backend_health,
        }

    def probe_capture_candidates(self) -> list[dict[str, Any]]:
        resolved = resolve_runtime_config(self.config)
        configured = [dict(candidate) for candidate in resolved.capture.candidates]
        if not configured:
            configured = [{"backend": resolved.capture.backend}]
        results: list[dict[str, Any]] = []
        for candidate in configured:
            backend = str(candidate.get("backend") or "").strip().lower()
            if not backend:
                continue
            results.append(
                {
                    "candidate": candidate,
                    "probe": self.probe_capture_backend(backend=backend),
                }
            )
        return results

    def capture_preview(
        self,
        *,
        backend: str | None = None,
        rect: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any]:
        return self.probe_capture_backend(backend=backend, rect=rect)

    def check_window_spec(self) -> dict[str, Any]:
        resolved = resolve_runtime_config(self.config)
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "windows_diagnostics only supports runtime.provider='windows'.",
                {"provider": resolved.provider},
            )
        target = WindowTarget.create(resolved.target)
        return evaluate_window_spec(target, resolved.window_spec).to_dict()

    def ensure_window_spec(self) -> dict[str, Any]:
        resolved = resolve_runtime_config(self.config)
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "windows_diagnostics only supports runtime.provider='windows'.",
                {"provider": resolved.provider},
            )
        target = WindowTarget.create(resolved.target)
        return ensure_window_spec(target, resolved.window_spec).to_dict()

    def show_gamepad_info(self) -> dict[str, Any]:
        from .gamepad_service import GamepadService

        # Avoid constructor dependency here; diagnostics should still work even if gamepad is disabled.
        service = GamepadService(self.config)
        return service.self_check()

    def _resolve_with_target_overrides(self, overrides: Mapping[str, Any]) -> ResolvedRuntimeConfig:
        resolved = resolve_runtime_config(self.config)
        runtime_payload = resolved.to_dict()
        target_payload = dict(runtime_payload.get("target", {}) or {})
        target_payload.update(dict(overrides or {}))
        runtime_payload["target"] = target_payload
        overlay = _OverlayConfig({"runtime": runtime_payload})
        return resolve_runtime_config(overlay)

    def _current_transformer(self) -> ReferenceCoordinateTransformer:
        resolved = resolve_runtime_config(self.config)
        if resolved.provider != "windows":
            raise TargetRuntimeError(
                "provider_unsupported",
                "Coordinate transforms require runtime.provider='windows'.",
                {"provider": resolved.provider},
            )
        candidate = resolve_window_candidate(resolved.target)
        client_rect = candidate.client_rect or (0, 0, 1, 1)
        transform_config = CoordinateTransformConfig(
            mode=resolved.coordinates.mode,
            reference_resolution=resolved.coordinates.reference_resolution,
        )
        return ReferenceCoordinateTransformer(
            client_size=(int(client_rect[2]), int(client_rect[3])),
            config=transform_config,
        )

    @staticmethod
    def _current_process_private_mb(process: psutil.Process) -> float:
        info = process.memory_info()
        private_bytes = getattr(info, "private", None)
        if private_bytes is None:
            private_bytes = getattr(info, "private_usage", None)
        if private_bytes is None:
            private_bytes = int(info.rss)
        return round(float(private_bytes) / (1024.0 * 1024.0), 3)
