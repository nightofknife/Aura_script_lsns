"""Try one native sweep before ordinary combat, preserving cancellation."""
from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from ._battle_sweep_runtime import try_battle_sweep


@action_info(name="resonance_pc.try_battle_sweep", public=True, read_only=False,
             description="Execute one native sweep when its entry is positively available; only a recognized native unavailable layout permits combat fallback.")
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", interaction="plans/aura_base/interaction")
async def resonance_pc_try_battle_sweep(
    app: Any = None, ocr: Any = None, interaction: Any = None,
    engine: Any = None, context: Any = None, capture_count: int | None = None,
    formation_index: int | None = None, control_cid: str | None = None,
) -> dict:
    return await try_battle_sweep(app=app, ocr=ocr, interaction=interaction,
                                  engine=engine, context=context, capture_count=capture_count,
                                  formation_index=formation_index, control_cid=control_cid)
