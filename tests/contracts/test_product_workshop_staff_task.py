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


def run_staffing(overrides=None, inputs=None, selection_action="selected", missing_navigation=False):
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
                    return {}
                value = {"success": True, "room_entered": True,
                         "room_name": params["inputs"]["room_name"], "reason": ""}
            else:
                value = deepcopy(params["previous"])
                value.update(room_name=params["room_name"], step=params["step"],
                             success=True, safe_to_continue=True, reason="",
                             selection_action=selection_action,
                             confirmed=selection_action == "selected" and node_id in {"confirm", "request_save", "apply", "return_overview"},
                             applied=selection_action == "selected" and node_id in {"apply", "return_overview"},
                             returned_to_overview=node_id == "return_overview",
                             page_after="reforming_center_overview" if node_id == "return_overview" else "editor")
            value = deepcopy((overrides or {}).get(node_id, value))
            if node_id == "navigate":
                # Child operations export stable business results.
                return value
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
    assert "outputs" not in navigation
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
    assert result["selection_action"] == "selected"
    assert result["save_verified"] is False
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


def test_cancel_all_entry_returns_unchanged_without_save_claim():
    calls, states, result = run_staffing(selection_action="unchanged")
    assert [name for name, _ in calls] == ["assets", "navigate", *STEPS, "finish"]
    assert all(states[step] == StepState.SUCCESS for step in STEPS)
    assert result["status"] == "unchanged" and result["success"]
    assert result["selection_action"] == "unchanged"
    assert result["reason"] == ""
    assert not result["applied"] and result["returned_to_overview"]
    assert result["save_verified"] is False


@pytest.mark.parametrize("selection_action", ["selected", "unchanged"])
def test_public_result_contains_no_staff_counts_or_capacity(selection_action):
    _, _, result = run_staffing(selection_action=selection_action)
    assert set(result) == {"room_name", "success", "status", "reason", "selection_action",
                           "save_verified", "applied", "returned_to_overview", "page_after", "data"}
    assert set(task()["returns"]) == set(result)

    def assert_no_counts(value):
        if isinstance(value, dict):
            assert not any("count" in str(key).lower() or "capacity" in str(key).lower() for key in value)
            for item in value.values():
                assert_no_counts(item)
        elif isinstance(value, list):
            for item in value:
                assert_no_counts(item)
    assert_no_counts(result)


def test_unknown_selection_button_stops_before_select_confirm_or_apply():
    failure = {"room_name": ROOM, "step": "quick", "success": False,
               "safe_to_continue": False, "reason": "staff_selection_button_unknown",
               "page_after": "selection", "returned_to_overview": False}
    calls, states, result = run_staffing(overrides={"quick": failure})
    assert [name for name, _ in calls] == ["assets", "navigate", "edit", "quick", "finish"]
    assert all(states[step] == StepState.SKIPPED for step in STEPS[2:])
    assert result["status"] == "failed" and not result["success"]
    assert result["reason"] == "staff_selection_button_unknown"
    assert not result["applied"]


@pytest.mark.parametrize("selection_action", [None, "unknown", "cancel_all", ""])
def test_unknown_selection_action_cannot_claim_submission(selection_action):
    _, _, result = run_staffing(selection_action=selection_action)
    assert not result["success"] and result["status"] == "failed"
    assert not result["save_verified"]


def test_selected_action_without_apply_cannot_claim_submission():
    last = {"room_name": ROOM, "step": "return_overview", "success": True,
            "safe_to_continue": True, "reason": "", "selection_action": "selected",
            "confirmed": True, "applied": False, "returned_to_overview": True,
            "page_after": "reforming_center_overview"}
    _, _, result = run_staffing(overrides={"return_overview": last})
    assert result["status"] == "failed" and not result["success"]


@pytest.mark.parametrize("patch", [
    {"selection_action": "selected", "confirmed": False, "applied": True},
    {"selection_action": "unchanged", "confirmed": True, "applied": False},
    {"selection_action": "unchanged", "confirmed": False, "applied": True},
])
def test_branch_evidence_must_match_confirm_and_apply_operations(patch):
    last = {"room_name": ROOM, "step": "return_overview", "success": True,
            "safe_to_continue": True, "reason": "", "returned_to_overview": True,
            "page_after": "reforming_center_overview", **patch}
    _, _, result = run_staffing(overrides={"return_overview": last})
    assert result["status"] == "failed" and not result["success"]


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
    for key in (f"{TASK}/{TASK}",):
        assert tasks[key]["source"] == f"tasks/{TASK}.yaml"
        assert tasks[key]["title"] == "生产车间补人"
    actions = {row["name"]: row for row in manifest["exports"]["actions"]}
    for suffix in ("assets", "step", "summarize"):
        name = f"resonance_pc.product_workshop_staff_{suffix}"
        assert actions[name]["function"] == f"resonance_pc_product_workshop_staff_{suffix}"
        assert actions[name]["module"] == "plans.resonance_pc.src.actions.product_workshop_staff_pc_actions"
