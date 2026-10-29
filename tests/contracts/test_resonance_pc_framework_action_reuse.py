from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions
from plans.resonance_pc.src.actions import city_travel_pc_actions
from plans.resonance_pc.src.actions import combined_commerce_pc_actions
from plans.resonance_pc.src.actions import passenger_flow_pc_actions
from plans.resonance_pc.src.actions import passenger_pc_actions
from plans.resonance_pc.src.actions import reforming_center_pc_actions
from plans.resonance_pc.src.actions import _battle_dispatch_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]
PC_ROOT = REPO_ROOT / "plans" / "resonance_pc"


def _load_task(name: str) -> dict:
    return yaml.safe_load((PC_ROOT / "tasks" / name).read_text(encoding="utf-8"))


def test_combat_start_uses_framework_multi_target_text_click() -> None:
    task = _load_task("auto_battle_combat_pc.yaml")["auto_battle_combat_pc"]
    step = task["steps"]["click_start_entry"]

    assert step["action"] == "plans/aura_base/find_text_and_click"
    assert step["params"]["text_to_find"] == ["开始", "作战", "START BATTLE"]
    assert step["params"]["normalize"] is True
    assert step["params"]["move_duration"] == 0.0
    assert step["params"]["required"] is True
    assert "assert_start_battle_clicked" not in task["steps"]
    assert task["steps"]["detect_stamina_after_start"]["depends_on"] == "click_start_entry"


def test_simple_battle_back_steps_use_framework_image_click() -> None:
    tasks = _load_task("auto_battle_dispatch_pc.yaml")
    simple_step_names = {
        "click_back_after_first_batch",
        "click_back_after_single_batch",
        "click_back_after_second_batch",
        "click_back_to_action_summary",
        "click_back_after_first_gp_batch",
        "click_back_after_single_gp_batch",
        "click_back_after_second_gp_batch",
    }
    simple_steps = []
    complex_steps = []
    for task in tasks.values():
        for name, step in (task.get("steps") or {}).items():
            if name in simple_step_names:
                simple_steps.append(step)
            if step.get("action") == "resonance_pc.wait_and_click_back_button":
                complex_steps.append(step)

    assert len(simple_steps) == 7
    for step in simple_steps:
        assert step["action"] == "plans/aura_base/find_image_and_click"
        assert step["params"]["stable_scans"] == 2
        assert step["params"]["stable_center_tolerance_px"] == 2
        assert step["params"]["move_duration"] == 0.1
        assert step["params"]["required"] is True
    # The ordered daily dispatcher owns the single main-page handoff now.
    assert not complex_steps
    assert tasks["auto_battle_dispatch_pc"]["steps"]["execute"]["action"] == "resonance_pc.execute_battle_plan"
    calls = []

    class Execution:
        async def call_action(self, action, params, **kwargs):
            calls.append((action, params))
            return {"already_at_target": True}

    asyncio.run(_battle_dispatch_runtime._return_to_main(Execution()))
    assert len(calls) == 1
    assert calls[0][0] == "resonance_pc.wait_and_click_back_button"
    assert calls[0][1]["repeat_until_target"] is True
    assert calls[0][1]["target_stable_scans"] == 2


def test_removed_pc_action_and_drag_controller_are_not_exported() -> None:
    manifest = yaml.safe_load((PC_ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    actions = {item["name"]: item for item in manifest["exports"]["actions"]}

    assert "resonance_pc.wait_and_click_any_text" not in actions
    for action in (
        city_trade_flow_pc_actions.resonance_pc_buy_goods_on_buy_page,
        city_travel_pc_actions.resonance_pc_select_intercity_destination,
        city_travel_pc_actions.resonance_pc_intercity_depart_and_wait,
        passenger_pc_actions.resonance_pc_recruit_passengers_by_flyer,
        passenger_flow_pc_actions.resonance_pc_auto_passenger_trips_flow,
        city_trade_flow_pc_actions.resonance_pc_auto_cycle_trade_flow,
        combined_commerce_pc_actions.resonance_pc_auto_combined_commerce_flow,
    ):
        assert "controller" not in action.__aura_action__["services"]
