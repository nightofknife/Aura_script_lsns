"""High-level complete gestures for the Resonance bridge."""
import time

from packages.aura_core.api import service_info
from plans.aura_base.src.services.app_provider_service import AppProviderService
from .input_bridge_service import finite


@service_info(alias="app", replace="app", public=True,
              deps={"screen": "plans/aura_base/screen", "controller": "controller"})
class ResonancePcAppService(AppProviderService):
    def __init__(self, screen, controller):
        super().__init__(screen, controller, screen.target_runtime)

    def drag(self, start_x, start_y, end_x, end_y, button="left", duration=None,
             hold_before_release_sec=0.0, *, stop_inertia=None):
        if self.controller.bridge.enabled:
            coordinates = (start_x, start_y, end_x, end_y)
            for value in coordinates:
                finite(value)
            coordinates = tuple(int(value) for value in coordinates)
            self.controller.bridge.drag(*coordinates, button, duration,
                                        hold_before_release_sec, stop_inertia=stop_inertia)
        else:
            self.controller._system_inertia(stop_inertia)
            with self.controller.bridge.system_scope():
                super().drag(start_x, start_y, end_x, end_y, button, duration, hold_before_release_sec)

    async def drag_async(self, start_x, start_y, end_x, end_y, button="left", duration=None,
                         hold_before_release_sec=0.0, *, stop_inertia=None):
        if self.controller.bridge.enabled:
            await self.controller.bridge.run_async(self.drag, start_x, start_y, end_x, end_y,
                                                  button, duration, hold_before_release_sec,
                                                  stop_inertia=stop_inertia)
        else:
            self.controller._system_inertia(stop_inertia)
            with self.controller.bridge.system_scope():
                await super().drag_async(start_x, start_y, end_x, end_y, button, duration, hold_before_release_sec)

    def focus_with_input(self, click_delay=0.3):
        if not self.controller.bridge.enabled:
            return super().focus_with_input(click_delay)
        delay = max(int(finite(click_delay) * 1000), 0) / 1000.0
        self.controller.bridge.ensure_ready()
        time.sleep(delay)
        return True

    def cancel_input(self):
        self.controller.cancel_input()

    async def cancel_input_async(self):
        await self.controller.cancel_input_async()
