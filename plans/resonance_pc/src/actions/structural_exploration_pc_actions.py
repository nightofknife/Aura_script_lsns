"""Configure and supervise one native structural-auto session, then stop it."""
from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from ._structural_exploration_runtime import run_structural


@action_info(
    name="resonance_pc.run_structural_exploration", public=True, read_only=False,
    description="Configure native structural-auto type priority, confirm its priority badges, start once, supervise observed outcomes and confirm native stop before returning.",
)
@requires_services(
    app="plans/aura_base/app", ocr="plans/aura_base/ocr",
    interaction="plans/aura_base/interaction",
)
async def resonance_pc_run_structural_exploration(
    types: list, app: Any = None, ocr: Any = None, interaction: Any = None,
    engine: Any = None, context: Any = None,
    control_cid: str | None = None,
) -> dict:
    """Return observed native-session results; counts are not a complete ledger.

    The public inputs remain unchanged. The native session owns event selection
    and continuation; this action configures, observes and stops that session.
    Native stop must be confirmed before a ready handoff. User cancellation
    requests native stop and ends the workflow; an unconfirmed stop is failure.
    control_cid binds soft-stop requests to the root run; users do not configure it.
    """
    return await run_structural(types, app=app, ocr=ocr, interaction=interaction,
                                engine=engine, context=context, control_cid=control_cid)
