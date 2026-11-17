"""Resonance PC capture and geometry routed through its input bridge session."""
from dataclasses import replace
import time

import win32gui

from packages.aura_core.api import service_info
from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.observability.runtime_trace import activity, current_root_cid
from packages.aura_core.scheduler.operation_scope import target_operation_guard

from ....aura_base.src.platform.contracts import TargetRuntimeError
from ....aura_base.src.platform.windows.window_selector import describe_window, resolve_window_candidate
from ....aura_base.src.services.target_runtime_service import TargetRuntimeService
from ..bridge.frame_stream import BACKEND
from .input_bridge_service import seconds


@service_info(alias="target_runtime", replace="plans/aura_base/target_runtime", public=False,
              deps={"config": "core/config", "bridge": "resonance_pc_input_bridge"})
class ResonancePcTargetRuntimeService(TargetRuntimeService):
    def __init__(self, config, bridge):
        self.bridge = bridge
        bridge.target_runtime = self
        super().__init__(config)

    @property
    def _use_bridge(self):
        return current_plan_name.get() == "resonance_pc" and self.bridge.enabled

    def open_frame_stream(self):
        if not self._use_bridge:
            return super().open_frame_stream()
        with target_operation_guard(self):
            return self.bridge.open_frame_stream()

    def capture(self, rect=None, backend=None):
        if not self._use_bridge:
            return super().capture(rect=rect, backend=backend)
        if backend is not None:
            self.set_capture_backend(backend)
        with target_operation_guard(self), activity("capture", "bridge.capture"):
            stream = self.bridge.open_frame_stream()
            if rect is not None:
                x, y, width, height = map(int, rect)
                w, h = self.bridge._viewport["width"], self.bridge._viewport["height"]
                if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > w or y + height > h:
                    raise TargetRuntimeError("capture_rect_invalid", "Capture rect is outside the current window client area.",
                                             {"rect": [x, y, width, height], "viewport": [0, 0, w, h]})
            requested_at = time.monotonic()
            generation = -1
            while time.monotonic() - requested_at < 1.0:
                with target_operation_guard(self):
                    packet = stream.capture_stream_frame(after_generation=generation)
                if packet is not None:
                    generation = packet["generation"]
                    if packet["arrived_at_monotonic"] <= requested_at:
                        continue
                    capture = packet["capture"]
                    if rect is None:
                        return capture
                    image = capture.image[y:y + height, x:x + width].copy()
                    return replace(capture, image=image, relative_rect=(x, y, width, height))
                time.sleep(.005)
            raise TargetRuntimeError("bridge_capture_timeout", "Unity did not publish a fresh frame within one second")

    def get_client_rect(self):
        if not self._use_bridge:
            return super().get_client_rect()
        with target_operation_guard(self):
            return self.bridge.viewport()

    def get_pixel_color_at(self, x, y):
        if not self._use_bridge:
            return super().get_pixel_color_at(x, y)
        return tuple(self.capture((int(x), int(y), 1, 1)).image[0, 0].tolist())

    def focus(self):
        if not self._use_bridge:
            return super().focus()
        with target_operation_guard(self):
            self.bridge.ensure_ready()
        return True

    def focus_with_input(self, click_delay=.3):
        if not self._use_bridge:
            return super().focus_with_input(click_delay)
        delay = int(seconds(click_delay) * 1000.0) / 1000.0
        self.focus()
        time.sleep(delay)
        return True

    def target_summary(self):
        if not self._use_bridge:
            return super().target_summary()
        # The GUI polls this outside the task. It must not acquire input.
        identity = self.bridge._identity
        viewport = self.bridge._viewport
        candidate = (describe_window(identity[1]) if identity else
                     resolve_window_candidate(self._resolve_runtime_config().target))
        summary = candidate.to_dict()
        summary["minimized"] = bool(win32gui.IsIconic(candidate.hwnd))
        if identity and candidate.pid == identity[0] and viewport.get("width", 0) > 0:
            summary["client_rect"] = [0, 0, viewport["width"], viewport["height"]]
        return summary

    def list_capture_backends(self):
        if not self._use_bridge:
            return super().list_capture_backends()
        return {"available": [BACKEND], "enabled": [BACKEND], "default": BACKEND, "configured": True}

    def set_capture_backend(self, backend):
        if not self._use_bridge:
            return super().set_capture_backend(backend)
        if str(backend).strip().lower() != BACKEND:
            raise TargetRuntimeError("backend_runtime_switch_unsupported", "Bridge mode uses Unity capture")

    def self_check(self):
        if not self._use_bridge:
            return super().self_check()
        try:
            target = self.target_summary()
        except TargetRuntimeError as error:
            return {"ok": False, **error.to_dict()}
        return {"ok": True, "provider": "windows", "family": "windows_desktop", "target": target,
                "capture": {"backend": BACKEND, "health": {"stream_open": self.bridge._frame_stream is not None}},
                "input": {"backend": "bridge", "capabilities": dict(self.bridge._capabilities)}}

    def _capture_failure_png(self, request):
        if request.get("plan") != "resonance_pc" or self.bridge._mode != "bridge":
            return super()._capture_failure_png(request)
        stream = self.bridge.active_frame_stream(request.get("root_cid") or current_root_cid())
        packet = stream.capture_stream_frame() if stream is not None else None
        if packet is None:
            raise TargetRuntimeError("diagnostic_capture_failed", "No fresh frame from the active bridge session")
        return self._encode_failure_capture(packet["capture"])
