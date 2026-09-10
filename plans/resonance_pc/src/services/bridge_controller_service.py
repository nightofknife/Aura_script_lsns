"""Compatible input facade; system mode delegates unchanged to the base service."""
from packages.aura_core.api import service_info
from ....aura_base.src.platform.contracts import TargetRuntimeError
from ....aura_base.src.services.controller_service import ControllerService


@service_info(alias="controller", replace="controller", public=True,
              deps={"screen": "plans/aura_base/screen", "bridge": "resonance_pc_input_bridge"})
class ResonancePcControllerService(ControllerService):
    def __init__(self, screen, bridge):
        super().__init__(screen.target_runtime)
        self.bridge = bridge
        bridge._system_held = lambda: bool(self._held_mouse_buttons or self._held_keys)

    def _unsupported(self, capability):
        if self.bridge.enabled:
            raise TargetRuntimeError("input_capability_unsupported", f"Bridge does not support {capability}.")

    @staticmethod
    def _system_inertia(stop_inertia):
        if stop_inertia is not None and not isinstance(stop_inertia, bool):
            raise TargetRuntimeError("input_parameter_invalid", "stop_inertia must be boolean.")
        if stop_inertia:
            raise TargetRuntimeError("input_capability_unsupported", "System input cannot stop Unity inertia.")

    def click(self, x=None, y=None, button="left", clicks=1, interval=None):
        if self.bridge.enabled:
            self.bridge.click(x, y, button, clicks, interval)
        else:
            self.bridge.system_call(super().click, x, y, button, clicks, interval)

    def move_to(self, x, y, duration=None):
        if self.bridge.enabled:
            self.bridge.move_to(x, y, duration)
        else:
            self.bridge.system_call(super().move_to, x, y, duration)

    def move_relative(self, dx, dy, duration=None):
        if self.bridge.enabled:
            self.bridge.move_relative(dx, dy, duration)
        else:
            self.bridge.system_call(super().move_relative, dx, dy, duration)

    def mouse_down(self, button="left"):
        if self.bridge.enabled:
            self.bridge.button(button, True)
        else:
            self.bridge.system_call(super().mouse_down, button)

    def mouse_up(self, button="left"):
        if self.bridge.cleanup_in_bridge:
            self.bridge.button(button, False)
        else:
            self.bridge.system_call(super().mouse_up, button)

    def drag_to(self, x, y, button="left", duration=None, *, stop_inertia=None):
        if self.bridge.enabled:
            self.bridge.drag(None, None, x, y, button, duration, stop_inertia=stop_inertia)
        else:
            self._system_inertia(stop_inertia)
            self.bridge.system_call(super().drag_to, x, y, button, duration)

    def scroll(self, amount, direction="down"):
        if self.bridge.enabled:
            self.bridge.scroll(amount, direction)
        else:
            self.bridge.system_call(super().scroll, amount, direction)

    def release_all(self):
        if self.bridge.cleanup_in_bridge:
            self.bridge.release_all()
        else:
            self.bridge.system_call(super().release_all)

    def release_mouse(self):
        if self.bridge.cleanup_in_bridge:
            for button in tuple(self.bridge._held):
                self.bridge.button(button, False)
        else:
            for button in tuple(self._held_mouse_buttons):
                self.bridge.system_call(super().mouse_up, button)

    def release_key(self):
        if not self.bridge.cleanup_in_bridge:
            for key in tuple(self._held_keys):
                self.bridge.system_call(super().key_up, key)

    def cancel_input(self):
        if self.bridge.cleanup_in_bridge:
            self.bridge.cancel_input()
        else:
            super().release_all()

    def key_down(self, key):
        self._unsupported("keyboard")
        self.bridge.system_call(super().key_down, key)

    def key_up(self, key):
        if self.bridge.cleanup_in_bridge:
            raise TargetRuntimeError("input_capability_unsupported", "Bridge does not support keyboard.")
        self.bridge.system_call(super().key_up, key)

    def press_key(self, key, presses=1, interval=None):
        self._unsupported("keyboard")
        self.bridge.system_call(super().press_key, key, presses, interval)

    def type_text(self, text, interval=None):
        self._unsupported("text_input")
        self.bridge.system_call(super().type_text, text, interval)

    def look_delta(self, dx, dy):
        self._unsupported("relative_look")
        self.bridge.system_call(super().look_delta, dx, dy)

    def look_hold(self, vx, vy, *, duration_ms, tick_ms=None):
        self._unsupported("relative_look")
        self.bridge.system_call(super().look_hold, vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)

    async def click_async(self, x=None, y=None, button="left", clicks=1, interval=None):
        if self.bridge.enabled:
            await self.bridge.run_async(self.click, x, y, button, clicks, interval)
        else:
            await super().click_async(x, y, button, clicks, interval)

    async def move_to_async(self, x, y, duration=None):
        if self.bridge.enabled:
            await self.bridge.run_async(self.move_to, x, y, duration)
        else:
            await super().move_to_async(x, y, duration)

    async def move_relative_async(self, dx, dy, duration=None):
        if self.bridge.enabled:
            await self.bridge.run_async(self.move_relative, dx, dy, duration)
        else:
            await super().move_relative_async(dx, dy, duration)

    async def mouse_down_async(self, button="left"):
        if self.bridge.enabled:
            await self.bridge.run_async(self.mouse_down, button)
        else:
            await super().mouse_down_async(button)

    async def mouse_up_async(self, button="left"):
        if self.bridge.cleanup_in_bridge:
            await self.bridge.run_async(self.mouse_up, button)
        else:
            await super().mouse_up_async(button)

    async def drag_to_async(self, x, y, button="left", duration=None, *, stop_inertia=None):
        if self.bridge.enabled:
            await self.bridge.run_async(self.drag_to, x, y, button, duration, stop_inertia=stop_inertia)
        else:
            self._system_inertia(stop_inertia)
            await super().drag_to_async(x, y, button, duration)

    async def scroll_async(self, amount, direction="down"):
        if self.bridge.enabled:
            await self.bridge.run_async(self.scroll, amount, direction)
        else:
            await super().scroll_async(amount, direction)

    async def cancel_input_async(self):
        import asyncio
        await asyncio.to_thread(self.cancel_input)
