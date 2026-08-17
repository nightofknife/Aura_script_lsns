from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_combined_commerce_task_exports_nested_input_contract():
    task_path = (
        REPO_ROOT
        / "plans"
        / "resonance_pc"
        / "tasks"
        / "auto_combined_commerce_pc.yaml"
    )
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))[
        "auto_combined_commerce_pc"
    ]
    inputs = {row["name"]: row for row in task["meta"]["inputs"]}

    assert task["meta"]["entry_point"] is True
    assert inputs["order"]["enum"] == ["trade_first", "passenger_first"]
    assert inputs["total_fatigue_budget"]["default"] == 700
    assert inputs["trade_inputs"]["type"] == "dict"
    assert inputs["passenger_inputs"]["type"] == "dict"
    assert task["steps"]["run"]["action"] == "resonance_pc.auto_combined_commerce_flow"
    assert task["returns"]["trade"] == "{{ nodes.run.output.trade }}"
    assert task["returns"]["passenger"] == "{{ nodes.run.output.passenger }}"


def test_combined_task_does_not_replace_independent_task_files():
    tasks = REPO_ROOT / "plans" / "resonance_pc" / "tasks"

    assert (tasks / "auto_cycle_trade_pc.yaml").is_file()
    assert (tasks / "auto_passenger_trips_pc.yaml").is_file()
