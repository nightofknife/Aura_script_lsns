"""Production staffing orchestration with the real DAG and pure summary."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from packages.aura_core.config.template import TemplateRenderer
from packages.aura_core.config.validator import validate_task_definition
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine.execution_engine import ExecutionEngine, StepState
from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions.product_workshop_staff_pc_actions import (
    STEPS, resonance_pc_product_workshop_staff_summarize,
)

ROOT = Path(__file__).resolve().parents[2]
TASK = "fill_product_workshop_staff_pc"
ROOM = "1号生产车间"


def task():
    return yaml.safe_load((ROOT / f"plans/resonance_pc/tasks/{TASK}.yaml").read_text(encoding="utf-8"))[TASK]


def run_staffing(overrides=None, inputs=None, local_count=6, initial_count=2, missing_navigation=False):
    """Only replace input-producing UI actions; execute actual DAG/summary logic."""
    async def run():
        calls = []
        engine = ExecutionEngine(SimpleNamespace(services={}, debug_mode=False), None)

        async def execute(node, context):
            node_id = next(key for key, value in engine.nodes.items() if value is node)
            params = await TemplateRenderer(context, None).render(node.get("params", {}))
            calls.append((node_id, params))
            await asyncio.sleep(0)
            if node_id == "finish":
                return resonance_pc_product_workshop_staff_summarize(**params)
            if node_id == "assets":
                value = {"success": True, "reason": ""}
            elif node_id == "navigate":
                if missing_navigation:
                    return {"nodes": {}}
                value = {"success": True, "room_entered": True,
                         "room_name": params["inputs"]["room_name"], "reason": ""}
            else:
                value = deepcopy(params["previous"])
                value.update(room_name=params["room_name"], step=params["step"],
                             success=True, safe_to_continue=True, reason="",
                             initial_count=initial_count, local_count=local_count, capacity=6,
                             applied=local_count > initial_count and node_id in {"apply", "return_overview"},
                             returned_to_overview=node_id == "return_overview",
                             page_after="reforming_center_overview" if node_id == "return_overview" else "editor")
            value = deepcopy((overrides or {}).get(node_id, value))
            if node_id == "navigate":
                # aura.run_task returns the child's framework_data, not its returns.
                return {"nodes": {"open_room": {"output": value, "run_state": {"status": "SUCCESS"}}}}
            return value

        engine.node_executor.execute_single_action = execute
        ok, values = InputValidator(None).validate_inputs_against_meta(
            task()["meta"]["inputs"], {"room_name": ROOM, **(inputs or {})})
        assert ok, values
        result = await engine.run(task(), TASK, ExecutionContext(inputs=values))
        summary = result.data["nodes"]["finish"]["output"]
        exported = await TemplateRenderer(engine.root_context, None).render(task()["returns"])
        assert exported == summary
        return calls, engine.step_states, summary
    return asyncio.run(run())


def test_schema_inputs_and_native_workflow_order():
    spec = task()
    valid, reason = validate_task_definition({TASK: spec})
    assert valid, reason
    assert spec["meta"]["title"] == "生产车间补人"
    assert spec["meta"]["entry_point"] and spec["meta"]["concurrency"] == "exclusive"
    assert list(spec["steps"]) == ["assets", "navigate", *STEPS, "finish"]
    assert STEPS == ("edit", "quick", "select_all", "confirm", "request_save", "apply", "return_overview")
    room = next(row for row in spec["meta"]["inputs"] if row["name"] == "room_name")
    assert room["required"] and room["type"] == "enum"
    assert room["enum"] == ["1号生产车间", "2号生产车间"]
    navigation = spec["steps"]["navigate"]
    assert navigation["action"] == "aura.run_task"
    assert navigation["params"]["task_ref"] == "tasks:open_reforming_center_room_pc.yaml:open_reforming_center_room_pc"
    assert "outputs" in navigation
    for step in STEPS:
        node = spec["steps"][step]
        assert node["action"] == "resonance_pc.product_workshop_staff_step"
        assert node["params"]["step"] == step
        assert not {"loop", "retry", "on_exception"}.intersection(node)


@pytest.mark.parametrize("room", ["1号生产车间", "2号生产车间"])
def test_preflight_navigation_and_staffing_execute_in_strict_order(room):
    calls, states, result = run_staffing(inputs={"room_name": room, "max_search_steps": 4, "settle_sec": .2})
    assert [name for name, _ in calls] == ["assets", "navigate", *STEPS, "finish"]
    assert all(state == StepState.SUCCESS for state in states.values())
    assert calls[1][1]["inputs"] == {"room_name": room, "max_search_steps": 4, "settle_sec": .2}
    # The edit node must receive open_room's projected business evidence.
    assert calls[2][1]["previous"] == {"success": True, "room_entered": True, "room_name": room, "reason": ""}
    assert result["success"] and result["status"] == "submitted"
    assert result["reason"] == "save_persistence_unverified"
    assert result["room_name"] == room
    assert (result["initial_count"], result["selected_count"], result["capacity"]) == (2, 6, 6)
    assert result["selected_added_count"] == 4 and result["remaining_count"] == 0
    assert result["count_scope"] == "local_formation"
    assert result["save_verified"] is False
    assert result["final_count"] is None and result["added_count"] is None
    assert result["applied"] and result["returned_to_overview"]
    assert result["page_after"] == "reforming_center_overview"
    assert [row["step"] for row in result["data"]["steps"]] == list(STEPS)


@pytest.mark.parametrize("position", range(len(STEPS)))
def test_business_failure_stops_all_later_ui_steps_but_still_summarizes(position):
    failure = {"room_name": ROOM, "step": STEPS[position], "success": False,
               "safe_to_continue": False, "reason": "staff_click_not_confirmed",
               "page_after": "editor", "returned_to_overview": False}
    calls, states, result = run_staffing(overrides={STEPS[position]: failure})
    assert [name for name, _ in calls] == ["assets", "navigate", *STEPS[:position + 1], "finish"]
    assert all(states[step] == StepState.SKIPPED for step in STEPS[position + 1:])
    # UI business failure is still a successful framework action result.
    assert states[STEPS[position]] == StepState.SUCCESS
    assert result["status"] == "failed" and not result["success"]
    assert result["reason"] == failure["reason"]
    assert result["data"]["steps"][-1] == failure


def test_invalid_assets_prevent_navigation_and_all_ui_input():
    preflight = {"success": False, "reason": "staff_templates_invalid", "error": "missing template"}
    calls, states, result = run_staffing(overrides={"assets": preflight})
    assert [name for name, _ in calls] == ["assets", "finish"]
    assert states["navigate"] == StepState.SKIPPED
    assert all(states[step] == StepState.SKIPPED for step in STEPS)
    assert result["status"] == "failed" and result["reason"] == "staff_templates_invalid"
    assert result["data"]["preflight"] == preflight


@pytest.mark.parametrize("patch", [
    {"success": False, "room_entered": False, "reason": "destination_not_confirmed"},
    {"success": True, "room_entered": False, "reason": "room_unavailable"},
])
def test_navigation_business_failure_never_starts_editing(patch):
    navigation = {"room_name": ROOM, **patch}
    calls, states, result = run_staffing(overrides={"navigate": navigation})
    assert [name for name, _ in calls] == ["assets", "navigate", "finish"]
    assert all(states[step] == StepState.SKIPPED for step in STEPS)
    assert result["status"] == "failed" and result["reason"] == patch["reason"]
    assert result["data"]["navigation"] == navigation


def test_missing_child_framework_business_output_is_a_failed_result():
    calls, states, result = run_staffing(missing_navigation=True)
    assert [name for name, _ in calls] == ["assets", "navigate", "finish"]
    assert all(states[step] == StepState.SKIPPED for step in STEPS)
    assert not result["success"] and result["status"] == "failed"
    assert result["reason"] == "staff_navigation_not_confirmed"


def test_submitted_but_incomplete_staffing_returns_truthful_partial_result():
    calls, _, result = run_staffing(local_count=4)
    assert [name for name, _ in calls] == ["assets", "navigate", *STEPS, "finish"]
    assert result["status"] == "partial" and not result["success"]
    assert result["reason"] == "staff_not_full"
    assert result["selected_added_count"] == result["remaining_count"] == 2
    assert result["final_count"] is None and result["added_count"] is None
    assert result["save_verified"] is False
    assert result["applied"] and result["returned_to_overview"]


def test_full_local_formation_cannot_claim_completed_or_persisted_final_count():
    _, _, result = run_staffing(local_count=6)
    assert result["selected_count"] == result["capacity"] == 6
    assert result["status"] == "submitted" and result["status"] != "completed"
    assert result["applied"] and result["returned_to_overview"]
    assert not result["save_verified"]
    assert result["final_count"] is None and result["added_count"] is None


def test_unchanged_full_roster_can_report_existing_count_without_save_claim():
    _, _, result = run_staffing(local_count=6, initial_count=6)
    assert result["success"] and result["status"] == "completed"
    assert result["final_count"] == result["selected_count"] == 6
    assert result["added_count"] == result["selected_added_count"] == 0
    assert not result["applied"] and not result["save_verified"]


@pytest.mark.parametrize("inputs", [
    {}, {"room_name": "3号生产车间"}, {"room_name": "动物纤维原料车间"},
    {"room_name": ROOM, "max_search_steps": 25}, {"room_name": ROOM, "settle_sec": 0},
])
def test_invalid_or_missing_public_inputs_rejected_before_navigation(inputs):
    ok, _ = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], inputs)
    assert not ok


def test_generated_manifest_exports_public_task_and_actions():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    tasks = {row["id"]: row for row in manifest["exports"]["tasks"]}
    for key in (TASK, f"{TASK}/{TASK}"):
        assert tasks[key]["source"] == f"tasks/{TASK}.yaml"
        assert tasks[key]["title"] == "生产车间补人"
    actions = {row["name"]: row for row in manifest["exports"]["actions"]}
    for suffix in ("assets", "step", "summarize"):
        name = f"resonance_pc.product_workshop_staff_{suffix}"
        assert actions[name]["function"] == f"resonance_pc_product_workshop_staff_{suffix}"
        assert actions[name]["module"] == "plans.resonance_pc.src.actions.product_workshop_staff_pc_actions"
