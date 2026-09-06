"""Public action exports for the task-orchestrated Eternal Scuffle workflow."""
from __future__ import annotations
from typing import Any
from packages.aura_core.api import action_info, requires_services
from . import _eternal_scuffle_runtime as runtime


@action_info(name="resonance_pc.eternal_scuffle_initialize", public=True, read_only=False, description="Validate inputs and initialize a CID-isolated Scuffle session at its home page.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def initialize_eternal_scuffle(coins_per_run: int = 1, run_count: int = 1, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None, context: Any = None) -> dict:
    return await runtime.initialize(coins_per_run, run_count, app, vision, state_store, event_bus, engine, context)


@action_info(name="resonance_pc.eternal_scuffle_begin_round", public=True, read_only=False, description="Begin one round only after the preceding round has returned home.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_begin_round(session_key: str, run_index: int, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "begin_round", app, vision, state_store, event_bus, engine, run_index=run_index)


@action_info(name="resonance_pc.eternal_scuffle_coin", public=True, read_only=False, description="Set the coin count by counted clicks and confirm entry to drafting.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_coin(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "coin", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_choose_role", public=True, read_only=False, description="Choose a ranked identified character and confirm the equipment page.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_choose_role(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "choose_role", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_choose_initial_equipment", public=True, read_only=False, description="Choose initial equipment and confirm the next draft stage.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_choose_initial_equipment(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "choose_initial_equipment", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_captain", public=True, read_only=False, description="Reconcile the five members and confirm the default leader.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_captain(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "captain", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_advance", public=True, read_only=False, description="Advance one battle-page transition without using stage numbers.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_advance(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "advance", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_choose_loot", public=True, read_only=False, description="Select ranked battle loot and confirm the assignment page.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_choose_loot(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "choose_loot", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_assign_loot", public=True, read_only=False, description="Reidentify member positions, assign the selected equipment and confirm.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_assign_loot(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "assign_loot", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_open_box", public=True, read_only=False, description="Open one remaining masked reward box or confirm all boxes are open.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_open_box(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "open_box", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_return_home", public=True, read_only=False, description="Return from a fully opened reward page and confirm the activity home.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_return_home(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "return_home", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_complete_round", public=True, read_only=False, description="Commit a completed clear or abandonment only after verified return home.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_complete_round(session_key: str, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "complete_round", app, vision, state_store, event_bus, engine)


@action_info(name="resonance_pc.eternal_scuffle_checkpoint", public=True, read_only=False, description="Require child framework_data to contain a confirmed business completion node.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", state_store="core/state_store", event_bus="core/event_bus")
async def eternal_scuffle_checkpoint(session_key: str, phases: list | None = None, expected_pairs: int = 0, child_result: dict | None = None, child_results: list | None = None, require_child: bool = False, expected_children: int = 0, label: str = "子任务", collect_round_result: bool = False, app: Any = None, vision: Any = None, state_store: Any = None, event_bus: Any = None, engine: Any = None) -> dict:
    return await runtime.invoke(session_key, "checkpoint", app, vision, state_store, event_bus, engine, phases=phases, expected_pairs=expected_pairs, child_result=child_result, child_results=child_results, require_child=require_child, expected_children=expected_children, label=label, collect_round_result=collect_round_result)


@action_info(name="resonance_pc.eternal_scuffle_finish", public=True, read_only=False, description="Return confirmed child-task results and remove only this completed session state.")
@requires_services(state_store="core/state_store", event_bus="core/event_bus")
async def finish_eternal_scuffle(session_key: str, state_store: Any = None, event_bus: Any = None, round_results: list | None = None) -> dict:
    return await runtime.finish(session_key, state_store, event_bus, round_results=round_results)
