"""Offline task/DAG contracts: no App, runner, capture, or game input is used."""

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
from plans.resonance_pc.src.actions.raw_workshop_pc_actions import (
    resonance_pc_raw_workshop_summarize,
)


ROOT = Path(__file__).resolve().parents[2]
TASK_NAME = "maintain_raw_material_workshop_pc"
ROOM = "动物纤维原料车间"
ROUNDS = [f"reserve_round_{index}" for index in range(1, 9)]


def task():
    path = ROOT / "plans/resonance_pc/tasks" / f"{TASK_NAME}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))[TASK_NAME]


def atom(production="idle", safe=True, status="completed", **fields):
    return {
        "success": status == "completed",
        "status": status,
        "reason": "" if safe else "unknown_state",
        "safe_to_continue": safe,
        "snapshot": {
            "production": production,
            "start_enabled": production == "idle",
            "work": {"current": 7, "capacity": 7, "full": True},
            "reserve": {"current": 2, "capacity": 2, "full": True},
        },
        "data": {"clicks": []},
        **fields,
    }


def run_dag(production="idle", overrides=None, budget=8, continue_rounds=False):
    """Use the real scheduler/rendering; replace only UI action invocation."""
    async def run():
        calls = []
        summaries = []
        overrides_by_node = deepcopy(overrides or {})
        engine = ExecutionEngine(SimpleNamespace(services={}, debug_mode=False), None)

        async def execute(node, context):
            node_id = next(key for key, value in engine.nodes.items() if value is node)
            params = await TemplateRenderer(context, None).render(node.get("params", {}))
            calls.append((node_id, params))
            if node_id in overrides_by_node:
                return overrides_by_node[node_id]
            if node_id == "navigate":
                return {"nodes": {"open_room": {"output": {
                    "success": True, "room_entered": True, "room_name": ROOM,
                    "status": "completed", "reason": "", "data": {},
                }}}}
            if node_id == "finish":
                summaries.append(params)
                return resonance_pc_raw_workshop_summarize(**params)
            if node_id == "return_overview":
                return {"success": True, "safe_to_continue": True, "returned_to_overview": True,
                        "status": "completed", "reason": "", "page_after": "reforming_center_overview"}
            if node_id in ROUNDS:
                return atom(production, continue_reserve=continue_rounds, round=node_id)
            if node_id == "start":
                return atom("running")
            if node_id == "final_observe":
                source = context.data["nodes"]["start"]["output"]
                if source is None:
                    source = context.data["nodes"]["before_start"]["output"]
                return atom(source["snapshot"]["production"], snapshot=deepcopy(source["snapshot"]))
            return atom(production)

        engine.node_executor.execute_single_action = execute
        inputs = {
            "room_name": ROOM, "max_reserve_rounds": budget,
            "max_search_steps": 12, "settle_sec": 0.8,
        }
        result = await engine.run(task(), TASK_NAME, ExecutionContext(inputs=inputs))
        return calls, summaries, engine.step_states, result

    return asyncio.run(run())


def test_public_task_schema_and_metadata():
    spec = task()
    valid, reason = validate_task_definition({TASK_NAME: spec})
    assert valid, reason
    assert spec["meta"]["entry_point"] is True
    assert spec["meta"]["concurrency"] == "exclusive"
    steps = spec["steps"]
    assert list(steps) == [
        "navigate", "observe", "deposit", "work", *ROUNDS,
        "before_start", "start", "final_observe", "return_overview", "finish",
    ]
    assert steps["navigate"]["action"] == "aura.run_task"
    assert steps["navigate"]["params"]["task_ref"] == (
        "tasks:open_reforming_center_room_pc.yaml:open_reforming_center_room_pc"
    )
    assert "room_entered" in steps["observe"]["when"]
    assert "nodes.open_room.output" in steps["observe"]["when"]


def test_generated_manifest_exports_public_task_and_all_maintenance_actions():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    exports = {row["id"]: row for row in manifest["exports"]["tasks"]}
    for key in (TASK_NAME, f"{TASK_NAME}/{TASK_NAME}"):
        assert exports[key]["source"] == f"tasks/{TASK_NAME}.yaml"
    actions = {row["name"] for row in manifest["exports"]["actions"]}
    assert {node["action"] for node in task()["steps"].values()} - {"aura.run_task"} <= actions


def test_scope_has_no_implicit_retry_shared_state_or_out_of_scope_action():
    steps = task()["steps"]
    allowed = {
        "aura.run_task", "resonance_pc.raw_workshop_observe",
        "resonance_pc.raw_workshop_deposit_max", "resonance_pc.raw_workshop_fill_list_once",
        "resonance_pc.raw_workshop_reserve_round", "resonance_pc.raw_workshop_start_once",
        "resonance_pc.raw_workshop_summarize",
        "resonance_pc.raw_workshop_return_overview",
    }
    assert {node["action"] for node in steps.values()} == allowed
    for node in steps.values():
        assert not {"retry", "on_exception", "on_result", "loop"}.intersection(node)
        assert "state." not in node.get("when", "")
    for index, key in enumerate(ROUNDS, 1):
        previous = "work" if index == 1 else ROUNDS[index - 2]
        assert steps[key]["depends_on"] == {previous: "success|skipped"}
        if index > 1:
            assert f"inputs.max_reserve_rounds >= {index}" in steps[key]["when"]
            assert f"nodes.{previous}.output.safe_to_continue" in steps[key]["when"]
            assert f"nodes.{previous}.output.continue_reserve" in steps[key]["when"]


def test_public_defaults_and_four_room_boundary():
    specs = task()["meta"]["inputs"]
    ok, values = InputValidator(None).validate_inputs_against_meta(specs, {"room_name": ROOM})
    assert ok, values
    assert values == {
        "room_name": ROOM, "max_reserve_rounds": 8,
        "max_search_steps": 12, "settle_sec": 0.8,
    }
    room_spec = next(row for row in specs if row["name"] == "room_name")
    assert set(room_spec["enum"]) == {
        "植物纤维原料车间", "动物纤维原料车间", "缝纫工具原料车间", "人造纤维原料车间",
    }


@pytest.mark.parametrize("provided", [
    {}, {"room_name": "餐厅"},
    {"room_name": ROOM, "max_reserve_rounds": 0},
    {"room_name": ROOM, "max_reserve_rounds": 9},
    {"room_name": ROOM, "max_reserve_rounds": 1.5},
    {"room_name": ROOM, "max_search_steps": 0},
    {"room_name": ROOM, "settle_sec": 0},
])
def test_bad_inputs_rejected_before_any_navigation(provided):
    ok, _ = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], provided)
    assert not ok


def test_idle_orders_deposit_work_reserve_start_then_final_observation():
    calls, summaries, states, result = run_dag()
    assert [name for name, _ in calls] == [
        "navigate", "observe", "deposit", "work", "reserve_round_1",
        "before_start", "start", "final_observe", "return_overview", "finish",
    ]
    assert all(states[name] == StepState.SKIPPED for name in ROUNDS[1:])
    assert summaries[0]["work"]["safe_to_continue"]
    assert len(summaries[0]["reserve_rounds"]) == 8
    assert summaries[0]["reserve_rounds"][1:] == [None] * 7
    assert result.data["nodes"]["finish"]["output"]["success"]


@pytest.mark.parametrize("budget", [1, 3, 8])
def test_running_uses_ordered_finite_rounds_without_idle_writes(budget):
    calls, summaries, _, _ = run_dag("running", budget=budget, continue_rounds=True)
    names = [name for name, _ in calls]
    assert "deposit" not in names and "work" not in names and "start" not in names
    assert [name for name in names if name in ROUNDS] == ROUNDS[:budget]
    rounds = summaries[0]["reserve_rounds"]
    assert [row["round"] for row in rounds if row is not None] == ROUNDS[:budget]
    assert rounds[budget:] == [None] * (8 - budget)


def test_running_stops_on_first_reported_no_progress():
    calls, summaries, _, _ = run_dag("running", overrides={
        "reserve_round_2": atom("running", status="partial", continue_reserve=False),
    }, continue_rounds=True)
    assert [name for name, _ in calls if name in ROUNDS] == ROUNDS[:2]
    assert summaries[0]["reserve_rounds"][1]["status"] == "partial"


def test_enum_accepted_integral_float_budget_is_normalized_before_real_summary():
    ok, values = InputValidator(None).validate_inputs_against_meta(
        task()["meta"]["inputs"], {"room_name": ROOM, "max_reserve_rounds": 8.0},
    )
    assert ok, values
    calls, summaries, states, result = run_dag(
        "running", budget=values["max_reserve_rounds"], continue_rounds=True,
    )
    assert [name for name, _ in calls if name in ROUNDS] == ROUNDS
    assert type(summaries[0]["max_reserve_rounds"]) is int
    assert summaries[0]["max_reserve_rounds"] == 8
    assert states["finish"] == StepState.SUCCESS
    assert result.data["nodes"]["finish"]["output"]["status"] == "completed"


@pytest.mark.parametrize("failed_node", ["observe", "deposit", "work", *ROUNDS])
def test_unknown_atomic_result_blocks_later_writes_but_still_summarizes(failed_node):
    production = "running" if failed_node in ROUNDS else "idle"
    failure = atom(production, safe=False, status="failed", continue_reserve=True)
    calls, summaries, _, result = run_dag(production, overrides={failed_node: failure}, continue_rounds=True)
    names = [name for name, _ in calls]
    assert failed_node in names
    assert names[names.index(failed_node) + 1:] == ["finish"]
    assert len(summaries) == 1
    assert result.data["nodes"]["finish"]["output"]["status"] == "failed"
    assert result.data["nodes"]["finish"]["output"]["reason"] == "unknown_state"
    assert result.data["nodes"]["finish"]["output"]["snapshot"] is None
    if failed_node in ROUNDS:
        assert failure in summaries[0]["reserve_rounds"]
    elif failed_node in ("deposit", "work"):
        assert summaries[0][failed_node] == failure
    else:
        assert summaries[0]["observation"] == failure


@pytest.mark.parametrize("failed_node", ["before_start", "start", "final_observe"])
def test_final_unknown_result_preserved_and_not_retried(failed_node):
    failure = atom(safe=False, status="failed")
    calls, summaries, _, result = run_dag(overrides={failed_node: failure})
    names = [name for name, _ in calls]
    assert names.count(failed_node) == 1
    assert names[names.index(failed_node) + 1:] == ["finish"]
    assert summaries[0][failed_node] == failure
    assert result.data["nodes"]["finish"]["output"]["status"] == "failed"
    assert result.data["nodes"]["finish"]["output"]["snapshot"] is None


def test_known_staffing_shortage_still_allows_safe_start():
    calls, summaries, _, _ = run_dag(overrides={
        "work": atom(status="partial"),
        "reserve_round_1": atom(status="partial", continue_reserve=False),
    })
    assert "start" in [name for name, _ in calls]
    assert summaries[0]["work"]["success"] is False
    assert summaries[0]["work"]["safe_to_continue"] is True


def test_disabled_start_reports_known_block_without_an_implicit_retry():
    disabled = atom()
    disabled["snapshot"]["start_enabled"] = False
    blocked = atom(status="blocked", reason="start_disabled")
    calls, summaries, states, result = run_dag(overrides={
        "before_start": disabled, "start": blocked,
    })
    assert [name for name, _ in calls].count("start") == 1
    assert states["start"] == StepState.SUCCESS
    assert summaries[0]["start"] == blocked
    assert summaries[0]["before_start"] == disabled
    outcome = result.data["nodes"]["finish"]["output"]
    assert outcome["status"] == "blocked"
    assert outcome["reason"] == "start_disabled"


def test_summary_receives_distinct_ordered_observations_without_snapshot_overwrite():
    initial = atom("idle", label="initial")
    before = atom("idle", label="before_start")
    final = atom("running", label="final")
    _, summaries, _, result = run_dag(overrides={
        "observe": initial, "before_start": before, "final_observe": final,
    })
    summary = summaries[0]
    assert summary["observation"] == initial
    assert summary["before_start"] == before
    assert summary["final_observe"] == final
    outcome = result.data["nodes"]["finish"]["output"]
    assert outcome["snapshot"] == final["snapshot"]
    assert outcome["status"] == "completed"


@pytest.mark.parametrize(("success", "entered"), [(False, False), (True, False), (False, True)])
def test_child_node_success_does_not_authorize_writes(success, entered):
    navigation = {"success": success, "room_entered": entered, "reason": "not_entered"}
    calls, summaries, _, _ = run_dag(overrides={
        "navigate": {"nodes": {"open_room": {"output": navigation}}},
    })
    assert [name for name, _ in calls] == ["navigate", "finish"]
    assert summaries[0]["navigation"] == navigation
    assert summaries[0]["observation"] is None


def test_returns_are_business_summary_not_execution_status():
    async def render():
        business = {
            "success": False, "status": "partial", "reason": "no_candidates",
            "room_name": ROOM, "snapshot": {"production": "running"},
            "data": {"reserve_rounds": 1},
            "returned_to_overview": True, "page_after": "reforming_center_overview",
        }
        context = ExecutionContext()
        context.add_node_result("finish", {"output": business, "run_state": {"status": "SUCCESS"}})
        assert await TemplateRenderer(context, None).render(task()["returns"]) == business
    asyncio.run(render())


def test_return_overview_runs_once_after_final_observation_without_outline_actions():
    calls, summaries, _, result = run_dag("running")
    names = [name for name, _ in calls]
    assert names[-3:] == ["final_observe", "return_overview", "finish"]
    assert names.count("return_overview") == 1
    assert summaries[0]["require_return"] is True
    assert result.data["nodes"]["finish"]["output"]["returned_to_overview"]
    assert task()["steps"]["return_overview"]["params"] == {"room_name": "{{ inputs.room_name }}"}


def test_known_partial_maintenance_still_returns_without_masking_shortage():
    partial = atom("running")
    partial["snapshot"]["reserve"]["current"] = 0
    partial["snapshot"]["reserve"]["full"] = False
    calls, _, _, result = run_dag("running", overrides={"final_observe": partial})
    assert "return_overview" in [name for name, _ in calls]
    outcome = result.data["nodes"]["finish"]["output"]
    assert outcome["status"] == "partial" and outcome["returned_to_overview"]
    assert outcome["data"]["maintenance"]["status"] == "partial"


def test_return_unknown_result_is_reported_without_retry_or_background_click():
    failure = {"success": False, "returned_to_overview": False, "safe_to_continue": False,
               "reason": "click_transition_not_confirmed"}
    calls, _, _, result = run_dag(overrides={"return_overview": failure})
    names = [name for name, _ in calls]
    assert names[names.index("return_overview"):] == ["return_overview", "finish"]
    outcome = result.data["nodes"]["finish"]["output"]
    assert outcome["status"] == "failed" and not outcome["returned_to_overview"]
    assert outcome["reason"] == "click_transition_not_confirmed"
    assert outcome["data"]["maintenance"]["status"] == "completed"
