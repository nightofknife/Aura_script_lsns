"""Public refresh-only entry points; matching candidates are kept for the user."""
from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from . import _affix_refresh_runtime as runtime


@action_info(name="resonance_pc.affix_refresh_read", public=True, read_only=True,
             description="Read the current affix replacement window without input or spending.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision",
                   ocr="plans/aura_base/ocr", state_store="core/state_store", event_bus="core/event_bus")
async def read_affix_refresh(config: Any = None, app: Any = None, vision: Any = None,
                             ocr: Any = None, state_store: Any = None, event_bus: Any = None,
                             engine: Any = None, context: Any = None) -> dict:
    return await runtime.execute(mode="read", config=config, app=app, vision=vision, ocr=ocr,
                                 state_store=state_store, event_bus=event_bus, engine=engine, context=context)


@action_info(name="resonance_pc.affix_refresh_run", public=True, read_only=False,
             description="Refresh within a film budget and stop on a target; replacement is disabled.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision",
                   ocr="plans/aura_base/ocr", state_store="core/state_store", event_bus="core/event_bus")
async def run_affix_refresh(config: Any = None, app: Any = None, vision: Any = None,
                            ocr: Any = None, state_store: Any = None, event_bus: Any = None,
                            engine: Any = None, context: Any = None) -> dict:
    return await runtime.execute(mode="run", config=config, app=app, vision=vision, ocr=ocr,
                                 state_store=state_store, event_bus=event_bus, engine=engine, context=context)
