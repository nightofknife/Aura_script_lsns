"""Only the current production staffing chain may be packaged and registered."""
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"


@pytest.mark.parametrize("relative", [
    "plans/resonance_pc/src/actions/product_workshop_pc_actions.py",
    "plans/resonance_pc/src/actions/_product_workshop_ui.py",
    "tools/build_product_workshop_templates.py",
    "plans/resonance_pc/data/meta/product_workshop_templates_pc.json",
    "plans/resonance_pc/templates/workshop/production/controls/editor",
])
def test_old_production_staffing_implementation_is_absent(relative):
    assert not (ROOT / relative).exists()


def test_manifest_only_exports_the_current_production_staffing_chain():
    manifest = yaml.safe_load((PLAN / "manifest.yaml").read_text(encoding="utf-8"))
    actions = {item["name"]: item for item in manifest["exports"]["actions"]}
    assert "resonance_pc.product_workshop_quick_fill" not in actions
    for operation in ("assets", "step", "summarize"):
        action = actions[f"resonance_pc.product_workshop_staff_{operation}"]
        assert action["module"] == "plans.resonance_pc.src.actions.product_workshop_staff_pc_actions"


def test_registered_staffing_task_does_not_reference_the_retired_entry():
    content = (PLAN / "tasks/fill_product_workshop_staff_pc.yaml").read_text(encoding="utf-8")
    task = yaml.safe_load(content)["fill_product_workshop_staff_pc"]
    assert task["meta"]["entry_point"] is True
    assert task["steps"]["assets"]["action"] == "resonance_pc.product_workshop_staff_assets"
    assert task["steps"]["finish"]["action"] == "resonance_pc.product_workshop_staff_summarize"
    assert "product_workshop_quick_fill" not in content
