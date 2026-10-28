"""City-to-city product workflow through the real DAG, without UI or game input.

The parent request/gates/finalizer are production code. Existing complete child
tasks and navigation calls return explicit fake outcomes at the action boundary.
"""
from __future__ import annotations

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
from plans.resonance_pc.src.actions import product_workflow_pc_actions as workflow


ROOT = Path(__file__).resolve().parents[2]
TASK = "collect_and_withdraw_products_pc"
TASK_PATH = ROOT / f"plans/resonance_pc/tasks/{TASK}.yaml"
CITY_KEY = "yunxiuqiao_base"
CHILD_TASKS = {
    "collect": "tasks:collect_product_workshops_pc.yaml:collect_product_workshops_pc",
    "withdraw": "tasks:withdraw_product_warehouse_to_train_pc.yaml:withdraw_product_warehouse_to_train_pc",
}


def task():
    return yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))[TASK]


def collected(**updates):
    return {
        "success": True, "status": "completed", "reason": "",
        "returned_to_overview": True, "page_after": "reforming_center_overview",
        "visited_count": 2, "collected_count": 2, "skipped_count": 4,
        "results": [], "unlocks": {}, "stopped_room": None, **updates,
    }


def withdrawn(**updates):
    return {
        "success": True, "status": "completed", "reason": "no_eligible_goods",
        "stop_reason": "no_eligible_goods",
        "goods_taken": [{"goods": "涤纶", "goods_id": "113", "quantity": 20}],
        "total_taken": 20, "unknown_goods": [], "reconcile_complete": True,
        "remaining_targets": [], "scan_complete": True, "unresolved": [], "issues": [],
        "returned_to_overview": True, "page_after": "overview",
        "submission_state": "return_observed", "server_readback_confirmed": False, **updates,
    }


def run_workflow(inputs=None, outcomes=None):
    async def run():
        calls = []
        engine = ExecutionEngine(SimpleNamespace(services={}, debug_mode=False), None)
        defaults = {
            "require_city": {"success": True, "page_state": "city_panel", "city_key": CITY_KEY},
            "enter": {"success": True, "status": "entered", "page_state": "reforming_center_overview",
                      "city_key": CITY_KEY},
            "collect": collected(), "withdraw": withdrawn(),
            "return_city": {"success": True, "page_state": "city_panel", "city_key": CITY_KEY},
        }
        defaults.update(outcomes or {})

        async def execute(node, context):
            node_id = next(key for key, value in engine.nodes.items() if value is node)
            params = await TemplateRenderer(context, None).render(node.get("params", {}))
            calls.append((node_id, deepcopy(params)))
            await asyncio.sleep(0)
            if node_id == "prepare":
                return workflow.resonance_pc_product_workflow_prepare(**params)
            if node_id.endswith("_gate"):
                return workflow.resonance_pc_product_workflow_evaluate(**params)
            if node_id == "finish":
                return workflow.resonance_pc_product_workflow_finish(**params, engine=engine, context=context)
            if node_id in {"enter", "return_city"}:
                assert params == {"city_key": CITY_KEY}
            value = defaults[node_id]
            if isinstance(value, BaseException):
                raise value
            return deepcopy(value)

        engine.node_executor.execute_single_action = execute
        ok, values = InputValidator(None).validate_inputs_against_meta(
            task()["meta"]["inputs"], {"goods": ["涤纶"], "quantity_threshold": 1, **(inputs or {})})
        assert ok, values
        context = ExecutionContext(inputs=values)
        try:
            context = await engine.run(task(), TASK, context)
        except asyncio.CancelledError:
            # The engine propagates cancellation rather than running dependency
            # failure handlers. Inspect the preserved root context without input.
            context = engine.root_context
        final = context.data.get("product_workflow_result")
        if engine.step_states.get("finish") == StepState.SUCCESS:
            final = context.data["nodes"]["finish"]["output"]
            exported = await TemplateRenderer(context, None).render(task()["returns"])
            assert exported == final
        return calls, engine.step_states, final, context

    return asyncio.run(run())


def called_stages(calls):
    return [name for name, _ in calls if name in {"require_city", "enter", "collect", "withdraw", "return_city"}]


def test_public_task_schema_and_complete_child_references():
    spec = task()
    valid, reason = validate_task_definition({TASK: spec})
    assert valid, reason
    assert spec["meta"]["title"] == "领取成品并取货"
    assert spec["meta"]["domain"] == "整顿中心"
    assert spec["meta"]["role"] == "user_task"
    assert spec["meta"]["entry_point"] is True and spec["meta"]["concurrency"] == "exclusive"
    assert [row["name"] for row in spec["meta"]["inputs"]] == ["operations", "goods", "quantity_threshold"]
    for name, reference in CHILD_TASKS.items():
        assert spec["steps"][name]["action"] == "aura.run_task"
        assert spec["steps"][name]["params"]["task_ref"] == reference
    assert spec["steps"]["enter"]["action"] == "resonance_pc.product_workflow_enter_from_city"
    assert spec["steps"]["enter"]["params"] == {"city_key": "{{ nodes.city_gate.output.city_key }}"}
    assert spec["steps"]["return_city"]["params"] == {"city_key": "{{ nodes.withdraw_gate.output.city_key }}"}
    assert not any({"retry", "loop", "on_exception"}.intersection(node) for node in spec["steps"].values())


def test_generated_manifest_exports_the_new_public_task_and_handoff_actions():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    tasks = {row["id"]: row for row in manifest["exports"]["tasks"]}
    entry = tasks[f"{TASK}/{TASK}"]
    assert entry["source"] == f"tasks/{TASK}.yaml"
    assert entry["title"] == "领取成品并取货"
    actions = {row["name"] for row in manifest["exports"]["actions"]}
    assert {f"resonance_pc.product_workflow_{name}" for name in (
        "prepare", "require_city", "enter_from_city", "evaluate", "return_city", "finish")} <= actions


@pytest.mark.parametrize("operations,expected", [
    (["collect_products", "withdraw_products"], ["require_city", "enter", "collect", "withdraw", "return_city"]),
    (["withdraw_products", "collect_products"], ["require_city", "enter", "collect", "withdraw", "return_city"]),
    (["collect_products"], ["require_city", "enter", "collect", "return_city"]),
    (["withdraw_products"], ["require_city", "enter", "withdraw", "return_city"]),
    ([], ["require_city"]),
])
def test_operation_switches_keep_fixed_order_and_city_contract(operations, expected):
    calls, states, result, _ = run_workflow(inputs={"operations": operations})
    assert called_stages(calls) == expected
    assert result["success"] is True
    assert result["status"] == ("completed" if operations else "skipped")
    assert result["returned_to_city"] is True and result["page_after"] == "city_panel"
    assert result["city_key"] == CITY_KEY
    if "withdraw_products" in operations:
        assert result["goods_taken"] == withdrawn()["goods_taken"]
        params = next(params for name, params in calls if name == "withdraw")
        assert params["inputs"] == {"goods": ["113"], "quantity_threshold": 1}
    else:
        assert result["goods_taken"] == [] and result["total_taken"] == 0
        assert states["withdraw"] == StepState.SKIPPED


def test_collect_only_does_not_require_withdrawal_parameters():
    calls, _, result, _ = run_workflow(inputs={
        "operations": ["collect_products"], "goods": None, "quantity_threshold": None})
    assert called_stages(calls) == ["require_city", "enter", "collect", "return_city"]
    assert result["success"] is True


@pytest.mark.parametrize("goods", [None, []])
def test_unfiltered_selection_reaches_the_complete_withdrawal_task_as_null(goods):
    rows = [{"goods": "涤纶", "goods_id": "113", "quantity": 20},
            {"goods": "发动机", "goods_id": "1", "quantity": 3}]
    calls, _, result, _ = run_workflow(
        inputs={"operations": ["withdraw_products"], "goods": goods, "quantity_threshold": 1},
        outcomes={"withdraw": withdrawn(goods_taken=rows, total_taken=23)})
    assert result["success"] is True and result["goods_taken"] == rows
    forwarded = next(params for name, params in calls if name == "withdraw")
    assert forwarded["inputs"] == {"goods": None, "quantity_threshold": 1}
    assert "collect" not in called_stages(calls)


def test_omitted_goods_defaults_to_unfiltered_in_public_input_validation():
    ok, values = InputValidator(None).validate_inputs_against_meta(
        task()["meta"]["inputs"], {"operations": ["withdraw_products"], "quantity_threshold": 1})
    assert ok, values
    assert values["goods"] is None
    assert workflow.resonance_pc_product_workflow_prepare(**values)["goods"] is None


@pytest.mark.parametrize("missing", [{"quantity_threshold": None}])
def test_conditional_required_withdrawal_inputs_fail_before_any_navigation(missing):
    calls, states, _, _ = run_workflow(inputs=missing)
    assert states["prepare"] == StepState.FAILED
    assert called_stages(calls) == []


def test_full_warehouse_cancels_collection_then_permits_withdrawal_without_recollecting():
    blocked = collected(success=False, status="blocked", reason="warehouse_capacity_blocked", collected_count=0)
    calls, _, result, _ = run_workflow(outcomes={"collect": blocked})
    assert called_stages(calls) == ["require_city", "enter", "collect", "withdraw", "return_city"]
    assert result["success"] is False and result["status"] == "partial"
    assert result["collection_result"]["reason"] == "warehouse_capacity_blocked"
    assert result["withdrawal_result"]["success"] is True and result["total_taken"] == 20
    assert result["returned_to_city"] is True
    assert sum(name == "collect" for name, _ in calls) == 1


def test_ordinary_collection_failure_stops_withdrawal_but_can_return_from_confirmed_overview():
    failure = collected(success=False, status="failed", reason="collection_room_failed", collected_count=0)
    calls, states, result, _ = run_workflow(outcomes={"collect": failure})
    assert called_stages(calls) == ["require_city", "enter", "collect", "return_city"]
    assert states["withdraw"] == StepState.SKIPPED
    assert result["success"] is False and result["failure_stage"] == "collect"
    assert result["returned_to_city"] is True


def test_unsafe_collection_end_does_not_withdraw_or_force_return_city():
    failure = collected(success=False, status="failed", reason="collection_room_failed",
                        returned_to_overview=False, page_after="unknown", collected_count=0)
    calls, states, result, _ = run_workflow(outcomes={"collect": failure})
    assert called_stages(calls) == ["require_city", "enter", "collect"]
    assert states["withdraw"] == states["return_city"] == StepState.SKIPPED
    assert result["success"] is False and result["returned_to_city"] is False


def test_partial_withdrawal_is_not_reported_as_completed():
    partial = withdrawn(status="partial", issues=["eligible_goods_remaining"],
                        remaining_targets=[{"goods": "涤纶", "goods_id": "113", "quantity": 2}])
    calls, _, result, _ = run_workflow(outcomes={"withdraw": partial})
    assert called_stages(calls)[-1] == "return_city"
    assert result["success"] is False and result["status"] == "partial"
    assert result["withdrawal_result"]["status"] == "partial"
    assert result["total_taken"] == 20


def test_failed_city_return_preserves_completed_goods_and_reports_failure():
    calls, _, result, _ = run_workflow(outcomes={"return_city": RuntimeError("city return unconfirmed")})
    assert called_stages(calls)[-1] == "return_city"
    assert result["success"] is False and result["returned_to_city"] is False
    assert result["failure_stage"] == "return_city"
    assert result["goods_taken"] == withdrawn()["goods_taken"] and result["total_taken"] == 20


def test_child_framework_failure_keeps_prior_collection_and_stops_unsafe_cleanup():
    calls, states, result, context = run_workflow(outcomes={"withdraw": RuntimeError("child task failed")})
    assert states["withdraw"] == StepState.FAILED
    assert states["return_city"] == StepState.SKIPPED
    assert called_stages(calls) == ["require_city", "enter", "collect", "withdraw"]
    assert result["success"] is False and result["collection_result"]["success"] is True
    assert result["withdrawal_result"]["success"] is False
    assert context.data["product_workflow_result"] == result


def test_city_precondition_failure_never_enters_reforming_center():
    calls, _, result, _ = run_workflow(outcomes={"require_city": RuntimeError("not on city panel")})
    assert called_stages(calls) == ["require_city"]
    assert result["success"] is False and result["returned_to_city"] is False
    assert result["failure_stage"] == "city"


def test_game_main_screen_is_not_accepted_as_the_building_city_start():
    calls, _, result, _ = run_workflow(outcomes={
        "require_city": {"success": True, "page_state": "city_main", "city_key": CITY_KEY}})
    assert called_stages(calls) == ["require_city"]
    assert result["success"] is False and result["returned_to_city"] is False
    assert result["failure_stage"] == "city"


@pytest.mark.parametrize("navigation", [
    {"success": True, "page_state": "city_panel", "city_key": "wrong_city"},
    {"success": True, "page_state": "city_main", "city_key": CITY_KEY},
])
def test_wrong_city_or_game_main_does_not_satisfy_building_city_return_contract(navigation):
    _, _, result, _ = run_workflow(outcomes={
        "return_city": navigation})
    assert result["success"] is False and result["returned_to_city"] is False
    assert result["failure_stage"] == "return_city"
    assert result["goods_taken"] == withdrawn()["goods_taken"]


def test_cancelled_child_does_not_start_withdrawal_or_city_cleanup():
    calls, states, result, _ = run_workflow(outcomes={"collect": asyncio.CancelledError()})
    assert states["collect"] == StepState.CANCELLED
    assert "withdraw" not in called_stages(calls) and "return_city" not in called_stages(calls)
    assert result is None
