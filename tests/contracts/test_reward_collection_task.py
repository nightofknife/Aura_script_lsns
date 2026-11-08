"""Exercise the reward sequence with the production DAG and simulated UI actions."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from packages.aura_core.config.template import TemplateRenderer
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine.execution_engine import ExecutionEngine


TASK = "collect_rewards_pc"
COMPONENTS = ("daily_mission", "battle_pass")


@pytest.mark.parametrize("selected, confirmed, expected", [
    (("daily_mission",), True, ("daily_mission",)),
    (("battle_pass",), True, ("battle_pass",)),
    (COMPONENTS, True, COMPONENTS),
    (COMPONENTS, False, ("daily_mission",)),
    ((), True, ()),
])
def test_reward_collection_executes_only_selected_stages_in_order(selected, confirmed, expected):
    plan = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
    spec = yaml.safe_load((plan / "tasks/daily_rewards_pc.yaml").read_text(encoding="utf-8"))[TASK]

    async def run():
        engine = ExecutionEngine(SimpleNamespace(services={}, debug_mode=False), None)
        calls = []
        receipts = {
            component: {"component": component, "success": True, "status": "skipped_no_reminder",
                        "returned_to_main": confirmed if component == "daily_mission" else True}
            for component in COMPONENTS
        }
        action_components = {
            "resonance_pc.collect_daily_mission_rewards": "daily_mission",
            "resonance_pc.collect_battle_pass_rewards": "battle_pass",
        }

        async def execute(node, _context):
            component = action_components[node["action"]]
            calls.append(component)
            return receipts[component]

        engine.node_executor.execute_single_action = execute
        context = ExecutionContext(inputs={component: component in selected for component in COMPONENTS})
        await engine.run(spec, TASK, context)
        result = await TemplateRenderer(engine.root_context, None).render(spec["returns"])
        assert tuple(calls) == expected
        assert result == {component: receipts[component] if component in expected else None
                          for component in COMPONENTS}

    asyncio.run(run())
