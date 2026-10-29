"""Public action entry for an ordered daily battle plan."""

from typing import Any

from packages.aura_core.api import action_info, requires_services

from ._battle_dispatch_runtime import execute_battle_plan


@action_info(
    name="resonance_pc.execute_battle_plan",
    public=True,
    read_only=False,
    description="Resolve the current game day and run ordinary battles in order, followed by structural exploration.",
)
@requires_services(state_store="core/state_store", event_bus="core/event_bus")
async def resonance_pc_execute_battle_plan(
    jobs: Any = None,
    structural_types: Any = None,
    weekly_plan: Any = None,
    state_store: Any = None,
    event_bus: Any = None,
    context: Any = None,
    engine: Any = None,
) -> dict:
    return await execute_battle_plan(
        jobs=jobs, structural_types=structural_types, weekly_plan=weekly_plan,
        context=context, engine=engine,
        state_store=state_store, event_bus=event_bus,
    )
