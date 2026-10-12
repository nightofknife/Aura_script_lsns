"""Real task loader, input validation, DAG and templates with offline UI dispatch.

No registry, target runtime, captures or game inputs are instantiated here.
"""
import asyncio
from copy import deepcopy
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from packages.aura_core.config.template import TemplateRenderer
from packages.aura_core.config.validator import get_task_schema_validator
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.context.persistence import PersistentDataService
from packages.aura_core.engine.execution_engine import ExecutionEngine
from packages.aura_core.packaging.core.task_loader import TaskLoader
from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import _black_moon_local_shop_state as ledger
from plans.resonance_pc.src.actions import black_moon_local_visit_pc_actions as visit


ROOT = Path(__file__).resolve().parents[2]
NAME = "black_moon_local_purchase_pc"
ITEM = "inferior_birch_stone"
WEEK = "2026-10-12"


class StateStore:
    _initialized = True

    def __init__(self):
        self.data = {}

    async def get(self, key):
        return deepcopy(self.data.get(key))

    async def set(self, key, value):
        self.data[key] = deepcopy(value)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_all_data(self):
        return deepcopy(self.data)


class City:
    def resolve_city(self, name):
        return {"city_key": name, "city_name": name}


@pytest.fixture
def task():
    assert get_task_schema_validator() is not None
    loader = TaskLoader("resonance_pc", ROOT / "plans/resonance_pc")
    result = loader.get_task_data(NAME)
    assert result is not None and loader.get_task_load_errors() == []
    return result


@pytest.fixture
def harness(monkeypatch, tmp_path, task):
    store = PersistentDataService(tmp_path)
    state = StateStore()
    calls, submitted, exits = [], [], []
    city_key = visit.load_black_moon_catalog()["eligible_cities"][0]
    monkeypatch.setattr(visit, "resolve_purchase_week", lambda _: WEEK)
    config = {"count": 1, "fail_payment": False, "fail_exit": False}
    latest = {}

    def city_record():
        return ledger.read_city_record(store, scope="default", week_key=WEEK, city_key=city_key)

    async def submit(selection, app, before_submit):
        assert app is None
        if selection["selected_new_count"] == 0:
            return {"success": True, "status": "skipped", "purchase_submitted": False}
        before_submit()
        submitted.append("click")
        return {"success": True, "status": "purchase_submitted", "purchase_submitted": True}

    async def wait(submission, app):
        assert app is None
        if config["fail_payment"]:
            raise RuntimeError("payment did not settle")
        return {"success": True, "ready_to_exit": True, "purchase_verified": True,
                "page_state": "local_shop"}

    async def exit_shop(completion, target, app):
        assert app is None
        if latest["visit"]["run"]:
            assert city_record()["completed"] is True
        exits.append(target)
        if config["fail_exit"]:
            raise RuntimeError("exit failed after completion was persisted")
        return {"success": True, "page_state": target, "exited": True, "click_count": 1}

    monkeypatch.setattr(visit, "execute_black_moon_submit", submit)
    monkeypatch.setattr(visit, "execute_black_moon_wait_purchase", wait)
    monkeypatch.setattr(visit, "execute_black_moon_exit", exit_shop)

    async def run(table=None, target="rest_menu"):
        ok, inputs = InputValidator(None).validate_inputs_against_meta(task["meta"]["inputs"], {
            "city_name": city_key, "purchase_table": {ITEM: True} if table is None else table,
            "exit_target": target,
        })
        assert ok, inputs
        engine = ExecutionEngine(SimpleNamespace(services={"state_store": state}, debug_mode=False), None)

        async def dispatch(node, context):
            params = await TemplateRenderer(context, state).render(node.get("params", {}))
            action = node["action"]
            calls.append(action)
            if action == "resonance_pc.require_client_resolution":
                return True
            if action == "resonance_pc.prepare_black_moon_local_shop_step":
                return {"success": True, "step": params["step"]}
            if action == "resonance_pc.select_black_moon_local_goods":
                assert params["require_empty_selection"] is True
                assert params["purchase_table"][ITEM] is True
                count = config["count"]
                return {"success": True, "selected_new_count": count,
                        "selected_items": [{"item_id": ITEM, "score": .97} for _ in range(count)],
                        "bottom_confirmed": True, "started_empty": True}
            if action == "resonance_pc.begin_black_moon_local_visit":
                result = visit.resonance_pc_begin_black_moon_local_visit(
                    **params, resonance_pc_city_shop_data=City(), persistent_data=store)
                latest["visit"] = result
                return result
            functions = {
                "resonance_pc.submit_black_moon_local_visit": visit.resonance_pc_submit_black_moon_local_visit,
                "resonance_pc.complete_black_moon_local_visit": visit.resonance_pc_complete_black_moon_local_visit,
                "resonance_pc.finish_black_moon_local_visit": visit.resonance_pc_finish_black_moon_local_visit,
            }
            assert action in functions, action
            return await functions[action](**params, app=None, persistent_data=store)

        engine.node_executor.execute_single_action = dispatch
        context = await engine.run(task, NAME, ExecutionContext(inputs=inputs))
        return context, state

    return SimpleNamespace(run=run, calls=calls, submitted=submitted, exits=exits,
                           config=config, record=city_record, store=store)


def returned(task, context, state):
    return asyncio.run(TemplateRenderer(context, state).render(task["returns"]))


def test_nonzero_full_pipeline_renders_real_dependencies_params_and_returns(harness, task):
    context, state = asyncio.run(harness.run(target="main"))
    result = returned(task, context, state)
    assert result["success"] is True and result["status"] == "completed"
    assert result["page_state"] == "main" and result["purchase_verified"] is True
    assert result["selected_card_count"] == 1 and harness.submitted == ["click"]
    assert harness.exits == ["main"]
    assert harness.calls == [
        "resonance_pc.begin_black_moon_local_visit", "resonance_pc.require_client_resolution",
        *["resonance_pc.prepare_black_moon_local_shop_step"] * 3,
        "resonance_pc.select_black_moon_local_goods", "resonance_pc.submit_black_moon_local_visit",
        "resonance_pc.complete_black_moon_local_visit", "resonance_pc.finish_black_moon_local_visit",
    ]
    finish = context.data["nodes"]["finish_visit"]["output"]
    assert all(result[key] == finish[key] for key in result)


def test_zero_match_pipeline_marks_complete_and_exits_without_payment(harness, task):
    harness.config["count"] = 0
    context, state = asyncio.run(harness.run())
    result = returned(task, context, state)
    assert result["reason"] == "no_matching_goods" and result["selected_card_count"] == 0
    assert result["purchase_verified"] is False and result["page_state"] == "rest_menu"
    assert harness.submitted == [] and harness.exits == ["rest_menu"]
    assert harness.record()["completed"] is True


@pytest.mark.parametrize("target", ["rest_menu", "main"])
def test_all_false_skips_shop_but_finish_receives_default_empty_completion(harness, task, target):
    context, state = asyncio.run(harness.run({ITEM: False}, target))
    result = returned(task, context, state)
    assert result["status"] == "skipped" and result["reason"] == "empty_selection"
    assert result["page_state"] == target
    assert harness.calls == ["resonance_pc.begin_black_moon_local_visit",
                             "resonance_pc.finish_black_moon_local_visit"]
    assert harness.submitted == []
    assert harness.exits == ([] if target == "rest_menu" else ["main"])
    assert not harness.store.exists(ledger.PURCHASES_FILE)


def test_weekly_skip_uses_skipped_dependency_chain_without_reopening_shop(harness, task):
    harness.config["count"] = 0
    asyncio.run(harness.run())
    harness.calls.clear()
    harness.exits.clear()
    context, state = asyncio.run(harness.run())
    result = returned(task, context, state)
    assert result["reason"] == "completed_this_week" and result["completed_this_week"] is True
    assert harness.calls == ["resonance_pc.begin_black_moon_local_visit",
                             "resonance_pc.finish_black_moon_local_visit"]
    assert harness.submitted == [] and harness.exits == []


def test_failed_settlement_blocks_finish_and_retains_unresolved_batch(harness):
    harness.config["fail_payment"] = True
    context, _ = asyncio.run(harness.run())
    assert harness.submitted == ["click"] and harness.exits == []
    assert "resonance_pc.finish_black_moon_local_visit" not in harness.calls
    assert context.data["nodes"]["complete_visit"]["run_state"]["status"] == "FAILED"
    saved = harness.record()
    assert saved["completed"] is False
    assert [row["status"] for row in saved["batches"].values()] == ["pending"]


def test_failed_exit_keeps_completion_already_persisted(harness):
    harness.config["fail_exit"] = True
    context, _ = asyncio.run(harness.run())
    assert harness.exits == ["rest_menu"]
    assert harness.record()["completed"] is True
    assert context.data["nodes"]["finish_visit"]["run_state"]["status"] == "FAILED"


def test_manifest_exports_and_injected_service_aliases_match_callable_signature(task):
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text("utf-8"))
    exports = {row["name"]: row for row in manifest["exports"]["actions"]}
    services = {row["name"] for row in manifest["exports"]["services"]}
    for action, function in (
        ("resonance_pc.begin_black_moon_local_visit", visit.resonance_pc_begin_black_moon_local_visit),
        ("resonance_pc.submit_black_moon_local_visit", visit.resonance_pc_submit_black_moon_local_visit),
        ("resonance_pc.complete_black_moon_local_visit", visit.resonance_pc_complete_black_moon_local_visit),
        ("resonance_pc.finish_black_moon_local_visit", visit.resonance_pc_finish_black_moon_local_visit),
    ):
        assert exports[action]["function"] == function.__name__
        dependencies = function._service_dependencies
        assert set(dependencies) <= set(inspect.signature(function).parameters)
        assert all("/" in value or value in services for value in dependencies.values())
    assert visit.resonance_pc_begin_black_moon_local_visit._service_dependencies[
        "resonance_pc_city_shop_data"] == "resonance_pc_city_shop_data"
    assert task["steps"]["finish_visit"]["params"]["completion"] == (
        "{{ nodes.complete_visit.output | default({}) }}")
