from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_intercity_departure_uses_fatigue_aware_controller():
    task_path = REPO_ROOT / "plans" / "resonance" / "tasks" / "city_travel.yaml"
    task_data = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    task = task_data["intercity_select_destination"]
    input_names = {item["name"] for item in task["meta"]["inputs"]}
    steps = task_data["intercity_select_destination"]["steps"]

    assert (REPO_ROOT / "plans" / "resonance" / "templates" / "go_destination_button.png").is_file()
    assert (REPO_ROOT / "plans" / "resonance" / "templates" / "fatigue_recovery_panel_title.png").is_file()
    assert (REPO_ROOT / "plans" / "resonance" / "templates" / "fatigue_medicine_birch_stone_button.png").is_file()
    assert (REPO_ROOT / "plans" / "resonance" / "templates" / "fatigue_medicine_confirm_button.png").is_file()

    assert "use_fatigue_medicine" in input_names
    assert "allowed_fatigue_medicines" in input_names
    assert "fatigue_medicine_max_uses" in input_names
    assert task_data["intercity_select_destination"]["meta"]["inputs"][1]["default"] == 0

    controller = steps["depart_and_wait"]
    assert controller["action"] == "resonance.intercity_depart_and_wait"
    assert controller["params"]["to_city_name"] == "{{ inputs.to_city_name }}"
    assert controller["params"]["enter_station_timeout_seconds"] == "{{ inputs.enter_station_timeout_seconds }}"
    assert controller["params"]["use_fatigue_medicine"] == "{{ inputs.use_fatigue_medicine | default(false) }}"
    assert controller["params"]["allowed_fatigue_medicines"] == "{{ inputs.allowed_fatigue_medicines | default([]) }}"
    assert controller["params"]["fatigue_medicine_max_uses"] == "{{ inputs.fatigue_medicine_max_uses | default(4) }}"

    returns = task["returns"]
    assert returns["status"] == "{{ nodes.depart_and_wait.output.status }}"
    assert returns["reason"] == "{{ nodes.depart_and_wait.output.reason }}"
    assert returns["blocked_at"] == "{{ nodes.depart_and_wait.output.blocked_at }}"
    assert returns["fatigue_medicine_used"] == "{{ nodes.depart_and_wait.output.fatigue_medicine_used }}"
    assert "wait_enter_station_text" not in steps
    assert "click_enter_station" not in steps
