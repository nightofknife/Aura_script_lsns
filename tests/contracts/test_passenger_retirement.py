"""Retired public tasks stay unavailable while attached-passenger contracts remain."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2] / "plans/resonance_pc"


def test_retired_passenger_tasks_and_actions_are_not_discoverable():
    manifest = yaml.safe_load((ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    actions = {row["name"] for row in manifest["exports"]["actions"]}
    tasks = {row["id"] for row in manifest["exports"]["tasks"]}
    for name in ("auto_passenger_trips_pc", "auto_combined_commerce_pc"):
        assert not (ROOT / "tasks" / f"{name}.yaml").exists()
        assert not any(task.startswith(name + "/") for task in tasks)
    assert "resonance_pc.auto_passenger_trips_flow" not in actions
    assert "resonance_pc.auto_combined_commerce_flow" not in actions
    assert "resonance_pc.recruit_passengers_by_flyer" in actions
    assert "resonance_pc.enter_city_and_settle_passengers" in actions


def test_freight_keeps_attached_passenger_inputs_and_shared_assets():
    for name in ("auto_cycle_trade_pc", "preview_trade_plan_pc"):
        task = yaml.safe_load((ROOT / "tasks" / f"{name}.yaml").read_text(encoding="utf-8"))[name]
        inputs = {row["name"] for row in task["meta"]["inputs"]}
        assert {"auto_passenger", "passenger_options"} <= inputs
    for path in ("data/meta/passenger_commerce_model.json",
                 "data/meta/passenger_destination_templates.json",
                 "templates/passenger/settlement/revenue.png"):
        assert (ROOT / path).is_file()
