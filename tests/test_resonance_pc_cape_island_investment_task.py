from pathlib import Path

import yaml

from packages.aura_core.config.validator import validate_task_definition


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "cape_island_investment_pc.yaml"


def test_cape_island_investment_task_reuses_city_entry_and_return_actions():
    data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    task = data["cape_island_investment_pc"]

    ok, error = validate_task_definition(data)

    assert ok is True, error
    assert task["meta"]["entry_point"] is True
    assert list(task["steps"]) == ["open_city", "invest", "return_main"]
    assert task["steps"]["open_city"]["action"] == "resonance_pc.open_city_panel_from_main"
    assert task["steps"]["invest"] == {
        "action": "resonance_pc.execute_cape_island_investment_from_city_panel",
        "depends_on": "open_city",
    }
    assert task["steps"]["return_main"] == {
        "action": "resonance_pc.go_city_main_direct",
        "depends_on": "invest",
    }
    assert task["returns"]["page_state"] == "{{ nodes.return_main.output.page_state }}"
