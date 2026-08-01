from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_passenger_task_exports_fixed_roundtrip_contract():
    task_path = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "auto_passenger_roundtrip_pc.yaml"
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))["auto_passenger_roundtrip_pc"]

    assert task["meta"]["entry_point"] is True
    inputs = {row["name"]: row for row in task["meta"]["inputs"]}
    assert inputs["round_trips"]["default"] == 1
    assert inputs["preferred_start_city_id"]["default"] == "11"
    assert inputs["arrival_timeout_seconds"]["default"] == 1800
    assert task["steps"]["run"]["action"] == "resonance_pc.auto_passenger_roundtrip_flow"
    assert task["returns"]["requires_manual_completion"] == "{{ nodes.run.output.requires_manual_completion }}"


def test_manifest_registers_passenger_actions_and_task():
    manifest = yaml.safe_load(
        (REPO_ROOT / "plans" / "resonance_pc" / "manifest.yaml").read_text(encoding="utf-8")
    )
    actions = {row["name"] for row in manifest["exports"]["actions"]}
    tasks = {row["id"] for row in manifest["exports"]["tasks"]}

    assert {
        "resonance_pc.open_passenger_management",
        "resonance_pc.recruit_passengers_by_flyer",
        "resonance_pc.enter_city_and_settle_passengers",
        "resonance_pc.auto_passenger_roundtrip_flow",
    } <= actions
    assert "auto_passenger_roundtrip_pc/auto_passenger_roundtrip_pc" in tasks
