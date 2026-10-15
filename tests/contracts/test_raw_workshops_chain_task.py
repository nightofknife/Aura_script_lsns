"""Offline four-room orchestration with the real DAG and business summary."""
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
from plans.resonance_pc.src.actions.raw_workshop_chain_pc_actions import (
    ROOM_ORDER, resonance_pc_raw_workshops_summarize,
)

ROOT = Path(__file__).resolve().parents[2]
TASK = "maintain_raw_material_workshops_pc"
STEPS = ("plant", "animal", "tools", "synthetic")


def task():
    return yaml.safe_load((ROOT / f"plans/resonance_pc/tasks/{TASK}.yaml").read_text(encoding="utf-8"))[TASK]


def outcome(room, status="completed", **fields):
    return {"room_name": room, "success": status == "completed", "status": status,
            "reason": "" if status == "completed" else "staff_not_full",
            "returned_to_overview": True, "page_after": "reforming_center_overview",
            "snapshot": {"room_name": room, "production": "running"}, **fields}


def run_chain(overrides=None, inputs=None, missing_summary=None):
    async def run():
        calls = []
        engine = ExecutionEngine(SimpleNamespace(services={}, debug_mode=False), None)

        async def execute(node, context):
            node_id = next(key for key, value in engine.nodes.items() if value is node)
            params = await TemplateRenderer(context, None).render(node.get("params", {}))
            calls.append((node_id, params))
            await asyncio.sleep(0)
            if node_id == "finish":
                return resonance_pc_raw_workshops_summarize(**params)
            if node_id == missing_summary:
                return {"nodes": {}}
            room = params["inputs"]["room_name"]
            business = deepcopy((overrides or {}).get(node_id, outcome(room)))
            # The child's framework status is successful even for business failures.
            return {"nodes": {"finish": {"output": business, "run_state": {"status": "SUCCESS"}}}}

        engine.node_executor.execute_single_action = execute
        ok, values = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], inputs or {})
        assert ok, values
        result = await engine.run(task(), TASK, ExecutionContext(inputs=values))
        summary = result.data["nodes"]["finish"]["output"]
        exported = await TemplateRenderer(engine.root_context, None).render(task()["returns"])
        assert exported == summary
        return calls, engine.step_states, summary
    return asyncio.run(run())


def test_task_schema_public_title_inputs_and_child_reuse():
    spec = task()
    valid, reason = validate_task_definition({TASK: spec})
    assert valid, reason
    assert spec["meta"]["title"] == "原料车间维护"
    assert spec["meta"]["entry_point"] and spec["meta"]["concurrency"] == "exclusive"
    assert list(spec["steps"]) == [*STEPS, "finish"]
    for step, room in zip(STEPS, ROOM_ORDER):
        node = spec["steps"][step]
        assert node["action"] == "aura.run_task"
        assert node["params"]["task_ref"] == "tasks:maintain_raw_material_workshop_pc.yaml:maintain_raw_material_workshop_pc"
        assert node["params"]["inputs"]["room_name"] == room
        assert "outputs" in node
        assert not {"retry", "loop", "on_exception"}.intersection(node)
    assert not any(row["name"] == "room_name" for row in spec["meta"]["inputs"])


def test_four_rooms_execute_in_order_with_forwarded_limits_and_public_result():
    calls, states, result = run_chain(inputs={"max_reserve_rounds": 3, "max_search_steps": 6, "settle_sec": .5})
    assert [name for name, _ in calls] == [*STEPS, "finish"]
    assert all(states[name] == StepState.SUCCESS for name in STEPS)
    for (_, params), room in zip(calls, ROOM_ORDER):
        assert params["inputs"] == {"room_name": room, "max_reserve_rounds": 3,
                                    "max_search_steps": 6, "settle_sec": .5}
    assert result["success"] and result["status"] == "completed"
    assert [row["room_name"] for row in result["results"]] == list(ROOM_ORDER)
    assert result["completed_count"] == result["visited_count"] == 4
    assert result["skipped_count"] == 0 and result["stopped_room"] is None
    assert result["returned_to_overview"]


def test_generated_manifest_exports_chain_and_summary_with_requested_title():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    tasks = {row["id"]: row for row in manifest["exports"]["tasks"]}
    for key in (TASK, f"{TASK}/{TASK}"):
        assert tasks[key]["source"] == f"tasks/{TASK}.yaml"
        assert tasks[key]["title"] == "原料车间维护"
    actions = {row["name"]: row for row in manifest["exports"]["actions"]}
    assert actions["resonance_pc.raw_workshops_summarize"]["function"] == "resonance_pc_raw_workshops_summarize"
    single = yaml.safe_load((ROOT / "plans/resonance_pc/tasks/maintain_raw_material_workshop_pc.yaml").read_text(encoding="utf-8"))
    rooms = next(row["enum"] for row in single["maintain_raw_material_workshop_pc"]["meta"]["inputs"] if row["name"] == "room_name")
    assert set(ROOM_ORDER) == set(rooms)


@pytest.mark.parametrize("position", range(4))
@pytest.mark.parametrize("status", ["partial", "blocked"])
def test_known_incomplete_room_returns_to_overview_and_continues(position, status):
    business = outcome(ROOM_ORDER[position], status=status, reason="no_available_prisoners")
    calls, _, result = run_chain(overrides={STEPS[position]: business})
    assert [name for name, _ in calls] == [*STEPS, "finish"]
    assert result["status"] == "partial" and not result["success"]
    assert result["completed_count"] == 3 and result["visited_count"] == 4
    assert result["results"][position] == business


@pytest.mark.parametrize("position", range(4))
def test_failed_room_stops_remaining_workshops_and_keeps_previous_results(position):
    failure = outcome(ROOM_ORDER[position], "failed", reason="click_transition_not_confirmed",
                      returned_to_overview=False, page_after=None, snapshot=None)
    calls, states, result = run_chain(overrides={STEPS[position]: failure})
    assert [name for name, _ in calls] == [*STEPS[:position + 1], "finish"]
    assert all(states[name] == StepState.SKIPPED for name in STEPS[position + 1:])
    assert result["status"] == "failed" and result["reason"] == failure["reason"]
    assert result["visited_count"] == position + 1 and result["completed_count"] == position
    assert result["skipped_count"] == 3 - position
    assert result["stopped_room"] == ROOM_ORDER[position]
    assert not result["returned_to_overview"] and result["page_after"] is None


@pytest.mark.parametrize("patch", [
    {"returned_to_overview": False}, {"page_after": "raw_material_workshop"},
    {"room_name": "错误车间"}, {"status": "unknown"}, {"success": False},
])
def test_framework_node_success_is_insufficient_for_next_room(patch):
    calls, _, result = run_chain(overrides={"plant": outcome(ROOM_ORDER[0], **patch)})
    assert [name for name, _ in calls] == ["plant", "finish"]
    assert result["status"] == "failed" and not result["success"]


def test_missing_child_summary_stops_and_reports_missing_result():
    calls, _, result = run_chain(missing_summary="plant")
    assert [name for name, _ in calls] == ["plant", "finish"]
    assert result["status"] == "failed" and not result["success"]
    assert result["visited_count"] == 1 and result["skipped_count"] == 3


@pytest.mark.parametrize("business", [None, {}, [], "unexpected"])
def test_malformed_business_result_does_not_start_next_workshop(business):
    calls, _, result = run_chain(overrides={"plant": business})
    assert [name for name, _ in calls] == ["plant", "finish"]
    assert result["status"] == "failed"
    assert result["visited_count"] == 1 and result["skipped_count"] == 3


@pytest.mark.parametrize("inputs", [{"max_reserve_rounds": 0}, {"max_reserve_rounds": 9},
                                    {"max_search_steps": 25}, {"settle_sec": 0}])
def test_invalid_limits_rejected_before_any_navigation(inputs):
    ok, _ = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], inputs)
    assert not ok


def test_summary_is_pure_and_does_not_mutate_child_results():
    values = [outcome(room) for room in ROOM_ORDER]
    before = deepcopy(values)
    assert resonance_pc_raw_workshops_summarize(values)["success"]
    assert values == before
