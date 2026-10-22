"""Actual parent/child DAG execution consumes declared business returns only."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from packages.aura_core.api import ACTION_REGISTRY, ActionDefinition
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.observability.events import EventBus
from packages.aura_core.scheduler.orchestrator import Orchestrator
from packages.aura_core.scheduler.cancellation import clear_task_cancel, is_task_cancel_requested

UNSET = object()


@pytest.fixture(autouse=True)
def isolate_parent_cancellation():
    # These independent projects deliberately use a fixed CID for event checks.
    clear_task_cancel("parent-contract")
    try:
        yield
    finally:
        clear_task_cancel("parent-contract")


def execute_project(tmp_path, monkeypatch, *, behavior="completed", child_node="private_work",
                    child_returns=UNSET, omit_returns=False, result_assertions=UNSET, repeat=1):
    async def run():
        calls, events = [], []

        async def work():
            calls.append("work")
            if behavior == "exception":
                raise RuntimeError("offline child error")
            if behavior == "cancelled":
                await asyncio.sleep(0)
                raise asyncio.CancelledError()
            if behavior == "timeout":
                await asyncio.sleep(1)
            if behavior == "timeout_then_complete" and calls.count("work") == 1:
                try:
                    await asyncio.sleep(1)
                finally:
                    await asyncio.sleep(0)
                    calls.append("timeout_cleanup")
            if behavior == "parent_timeout":
                try:
                    await asyncio.sleep(1)
                finally:
                    await asyncio.sleep(0)
                    calls.append("child_cleanup")
            return {"success": behavior != "blocked", "status": behavior, "receipt": 17}

        async def after():
            calls.append("after")
            return True

        async def parallel_work():
            calls.append("parallel")
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                calls.append("parallel_cleanup")

        async def normal_root():
            await asyncio.sleep(0)
            calls.append("normal")
            return True

        async def retrying():
            calls.append("retrying")
            raise ValueError("retry delay must also retain the parent's timeout source")

        definitions = {name: ActionDefinition(func=func, name=name, read_only=True, public=True,
                        service_deps={}, plugin=None, is_async=True)
                       for name, func in {"work": work, "after": after, "parallel_work": parallel_work,
                                          "normal_root": normal_root, "retrying": retrying}.items()}
        monkeypatch.setattr(ACTION_REGISTRY, "get", definitions.get)
        folder = tmp_path / "plans/business_contract/tasks"
        folder.mkdir(parents=True)
        child = {"meta": {"role": "internal", "entry_point": False},
                 "steps": {child_node: {"action": "work", **({"timeout_sec": .01} if behavior == "timeout" else {})}},
                 "returns": "{{ nodes." + child_node + ".output }}"}
        if behavior == "timeout_then_complete":
            child["steps"][child_node].update(timeout_sec=.03, on_exception={
                "retry": 1, "delay": 0, "retry_on": ["TimeoutError"],
            })
        if behavior == "parent_timeout":
            child["steps"]["parallel"] = {"action": "parallel_work"}
            child["steps"]["retrying"] = {"action": "retrying", "on_exception": {
                "retry": 1, "delay": 1, "retry_on": ["ValueError"],
            }}
        if behavior == "cancelled":
            child["steps"]["parallel"] = {"action": "parallel_work"}
            child["steps"]["normal"] = {"action": "normal_root"}
            child["steps"]["normal_successor"] = {"action": "after", "depends_on": "normal"}
        if omit_returns:
            child.pop("returns")
        elif child_returns is not UNSET:
            child["returns"] = child_returns
        parent = {"meta": {"role": "user_task", "entry_point": True},
                  "steps": {"child": {"action": "aura.run_task", "params": {
                      "task_ref": "tasks:child.yaml:child"}},
                      "after": {"action": "after", "depends_on": "child",
                                "when": "{{ nodes.child.output.success }}"}},
                  "returns": {"business": "{{ nodes.child.output }}"}}
        if behavior == "parent_timeout":
            parent["steps"]["child"]["timeout_sec"] = .05
        if result_assertions is not UNSET:
            parent["steps"]["child"]["params"]["result_assertions"] = result_assertions
        if repeat > 1:
            parent["steps"]["child"]["loop"] = {"times": repeat, "parallelism": 1}
        if child_returns is True:
            parent["steps"]["after"]["when"] = "{{ nodes.child.output == true }}"
        for name, spec in (("child", child), ("parent", parent)):
            (folder / (name + ".yaml")).write_text(yaml.safe_dump({name: spec}), encoding="utf-8")
        bus = EventBus()

        async def record(event):
            events.append(event)

        await bus.subscribe("task.finished", record)
        store = StateStoreService({})
        await store.initialize()
        pause = asyncio.Event()
        pause.set()
        orchestrator = Orchestrator(str(tmp_path), "business_contract", pause,
                                   runtime_services={"event_bus": bus, "state_store": store, "config": {}})
        try:
            result = await asyncio.wait_for(orchestrator.execute_task(
                task_file_path="tasks/parent.yaml", task_key="parent", cid="parent-contract"), timeout=3)
        except asyncio.CancelledError:
            result = {"status": "CANCELLED"}
        return result, calls, events
    return asyncio.run(run())


@pytest.mark.parametrize("child_node", ["private_work", "renamed_private_work"])
def test_declared_returns_survive_child_implementation_rename(tmp_path, monkeypatch, child_node):
    result, calls, _ = execute_project(tmp_path, monkeypatch, child_node=child_node)
    assert result["status"] == "SUCCESS"
    assert result["user_data"] == {"business": {"success": True, "status": "completed", "receipt": 17}}
    assert result["framework_data"]["nodes"]["child"]["output"] == result["user_data"]["business"]
    assert calls == ["work", "after"]


def test_business_blocked_is_preserved_and_stops_dependent_work(tmp_path, monkeypatch):
    result, calls, _ = execute_project(tmp_path, monkeypatch, behavior="blocked")
    assert result["status"] == "SUCCESS"
    assert result["user_data"]["business"]["success"] is False
    assert result["user_data"]["business"]["status"] == "blocked"
    assert calls == ["work"]


@pytest.mark.parametrize("behavior", ["exception", "timeout", "cancelled"])
def test_child_failure_timeout_and_cancellation_stop_parent(tmp_path, monkeypatch, behavior):
    result, calls, events = execute_project(tmp_path, monkeypatch, behavior=behavior)
    assert result["status"] == ("CANCELLED" if behavior == "cancelled" else "FAILED")
    if behavior == "cancelled":
        assert sorted(calls) == ["normal", "parallel", "parallel_cleanup", "work"]
        parent = next(event for event in events if event.payload["cid"] == "parent-contract")
        assert parent.payload["final_result"]["framework_data"]["nodes"]["after"]["run_state"]["status"] == "CANCELLED"
    else:
        assert calls == ["work"]
        assert not is_task_cancel_requested("parent-contract")
    assert any(event.payload["parent_cid"] == "parent-contract" for event in events)


def test_file_only_named_task_alias_is_rejected(tmp_path, monkeypatch):
    from packages.aura_core.packaging.core.task_loader import TaskLoader
    execute_project(tmp_path, monkeypatch)
    loader = TaskLoader("business_contract", tmp_path / "plans/business_contract")
    assert loader.get_task_data("child/child") is not None
    assert loader.get_task_data("child") is None
    definitions = loader.get_all_task_definitions()
    assert set(definitions) == {"child/child", "parent/parent"}
    assert definitions["child/child"]["__task_ref__"] == "tasks:child.yaml:child"


def test_local_node_timeout_finishes_cleanup_then_retries_without_cancelling_parent(tmp_path, monkeypatch):
    result, calls, _ = execute_project(tmp_path, monkeypatch, behavior="timeout_then_complete")
    assert result["status"] == "SUCCESS"
    assert calls == ["work", "timeout_cleanup", "work", "after"]
    assert result["user_data"]["business"]["success"] is True
    assert not is_task_cancel_requested("parent-contract")


def test_parent_node_timeout_drains_nested_dag_actions_and_retry_delay(tmp_path, monkeypatch):
    result, calls, events = execute_project(tmp_path, monkeypatch, behavior="parent_timeout")
    assert result["status"] == "FAILED"
    assert sorted(calls) == ["child_cleanup", "parallel", "parallel_cleanup", "retrying", "work"]
    assert not is_task_cancel_requested("parent-contract")
    assert any(event.payload["parent_cid"] == "parent-contract" for event in events)


@pytest.mark.parametrize("spec", [
    {"omit_returns": True}, {"child_returns": None}, {"child_returns": {}},
    {"child_returns": {"result": {}}, "result_assertions": {"result.success": True}},
    {"child_returns": {"success": 1}, "result_assertions": {"success": True}},
])
def test_invalid_child_business_result_stops_first_loop_iteration(tmp_path, monkeypatch, spec):
    result, calls, _ = execute_project(tmp_path, monkeypatch, repeat=3, **spec)
    assert result["status"] == "FAILED"
    assert calls == ["work"]


@pytest.mark.parametrize("invalid", [False, 0, [], "", {"": True}, {"result..success": True}])
def test_invalid_result_assertions_are_rejected_before_child_execution(tmp_path, monkeypatch, invalid):
    result, calls, _ = execute_project(tmp_path, monkeypatch, result_assertions=invalid)
    assert result["status"] == "FAILED"
    assert calls == []


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_null_assertion_requires_the_field_to_exist(tmp_path, monkeypatch, exists):
    business = {"success": True, **({"nullable": None} if exists else {})}
    result, calls, _ = execute_project(tmp_path, monkeypatch, child_returns=business,
                                      result_assertions={"nullable": None})
    assert result["status"] == ("SUCCESS" if exists else "FAILED")
    assert calls == (["work", "after"] if exists else ["work"])


def test_side_effect_child_can_explicitly_return_true(tmp_path, monkeypatch):
    result, calls, _ = execute_project(tmp_path, monkeypatch, child_returns=True)
    assert result["status"] == "SUCCESS"
    assert result["user_data"] == {"business": True}
    assert calls == ["work", "after"]
