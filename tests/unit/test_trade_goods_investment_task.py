"""Investment task reuse through the real framework sub-task adapter, without game input."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from packages.aura_core.api import ActionDefinition
from packages.aura_core.engine.action_injector import ActionInjector
from packages.aura_core.scheduler.validation import InputValidator, _MISSING

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, run_full


def business_result(mode=10, **changes):
    return {"success": True, "triggered": True, "status": "invested", "page_state": "shop_page",
            "mode": mode, "target_level": mode, **changes}


@pytest.mark.parametrize("task_name", ["standalone", "freight", "combined"])
def test_task_input_validator_enforces_numeric_target_contract(task_name):
    tasks = Path(__file__).resolve().parents[2] / "plans/resonance_pc/tasks"
    if task_name == "standalone":
        task = yaml.safe_load((tasks / "trade_goods_investment_pc.yaml").read_text(encoding="utf-8"))["trade_goods_investment_pc"]
        schema = next(item for item in task["meta"]["inputs"] if item["name"] == "mode")
    elif task_name == "freight":
        task = yaml.safe_load((tasks / "auto_cycle_trade_pc.yaml").read_text(encoding="utf-8"))["auto_cycle_trade_pc"]
        schema = next(item for item in task["meta"]["inputs"] if item["name"] == "trade_goods_investment_mode")
    else:
        task = yaml.safe_load((tasks / "auto_combined_commerce_pc.yaml").read_text(encoding="utf-8"))["auto_combined_commerce_pc"]
        schema = next(item for item in task["meta"]["inputs"] if item["name"] == "trade_inputs")["properties"]["trade_goods_investment_mode"]
    validator = InputValidator(None)
    assert validator.validate_input_value(schema, _MISSING, "mode") == (True, 10, None)
    for value in range(1, 21):
        assert validator.validate_input_value(schema, value, "mode") == (True, value, None)
    for value in (True, False, 10.0, 9.5, "10", "unlock", "balanced", "full", 0, 21):
        assert validator.validate_input_value(schema, value, "mode")[0] is False


class Renderer:
    def __init__(self, context, state_store):
        self.context = context

    async def get_render_scope(self):
        return {}

    async def render(self, params, *, scope):
        return deepcopy(params)


@pytest.fixture
def task_engine(monkeypatch):
    state = {"result": business_result(), "calls": [], "callback": None}
    context = SimpleNamespace(data={"cid": "parent-freight-cid"})

    async def execute_task(**params):
        state["calls"].append(params)
        callback = investment._INVESTMENT_PROGRESS_CALLBACK.get()
        state["callback"] = callback
        if state.get("emit_progress"):
            callback("running", {"preview_level": 6})
            callback("completed", {"preview_level": 6})
        if state.get("exception"):
            raise state["exception"]
        return state.get("framework_result", {
            "status": "SUCCESS", "framework_data": {"nodes": {"invest": {"output": state["result"]}}},
        })

    orchestrator = SimpleNamespace(plan_name="resonance_pc", loaded_package=None,
                                   resolve_service=lambda key: None, execute_task=execute_task)
    engine = SimpleNamespace(orchestrator=orchestrator, services={}, state_store=object())
    monkeypatch.setattr(trade, "TemplateRenderer", Renderer)
    return context, engine, state


def call_task(task_engine, mode=10):
    context, engine, _ = task_engine
    return asyncio.run(trade._run_trade_goods_investment_task(
        mode=mode, city_name="远星大桥", progress_context={"city_index": 1}, context=context, engine=engine,
    ))


@pytest.mark.parametrize("mode", [1, 9, 10, 11, 14, 20])
def test_real_run_task_adapter_passes_canonical_ref_inputs_and_parent(task_engine, mode):
    _, _, state = task_engine
    state["result"] = business_result(mode)
    assert call_task(task_engine, mode) == state["result"]
    assert state["calls"] == [{
        "task_file_path": "tasks/trade_goods_investment_pc.yaml", "task_key": "trade_goods_investment_pc",
        "inputs": {"mode": mode, "city_name": "远星大桥"}, "parent_cid": "parent-freight-cid",
    }]
    assert callable(state["callback"])
    assert investment._INVESTMENT_PROGRESS_CALLBACK.get() is None


@pytest.mark.parametrize("mode", [1, 9, 10, 11, 14, 20])
def test_numeric_level_normalization_preserves_integer(mode):
    assert investment.normalize_investment_mode(mode) == mode
    assert type(investment.normalize_investment_mode(mode)) is int


@pytest.mark.parametrize("mode", [True, False, 10.0, 9.5, "10", "unlock", "balanced", "full", 0, 21])
def test_invalid_numeric_level_rejected_before_child_or_game_input(task_engine, monkeypatch, mode):
    context, engine, state = task_engine
    with pytest.raises(ValueError):
        investment.normalize_investment_mode(mode)
    with pytest.raises(ValueError):
        call_task(task_engine, mode)
    assert state["calls"] == []
    monkeypatch.setattr(trade, "resonance_pc_click_city_shop_by_name",
                        lambda **params: pytest.fail("Invalid level must not touch the game"))
    with pytest.raises(ValueError):
        asyncio.run(trade._execute_freight_city_trade(
            **trade_inputs(), auto_trade_goods_investment=True, trade_goods_investment_mode=mode,
            context=context, engine=engine,
        ))
    with pytest.raises(ValueError):
        investment.resonance_pc_invest_trade_goods_from_shop(mode=mode, app=object(), vision=object())


@pytest.mark.parametrize("changes", [
    {"success": False}, {"success": 1}, {"status": "failed"}, {"status": "completed"},
    {"page_state": "investment_page"}, {"mode": 20}, {"target_level": 20},
    {"mode": True}, {"mode": 10.0}, {"target_level": True}, {"target_level": 10.0},
])
def test_child_business_failure_cannot_be_framework_success(task_engine, changes):
    task_engine[2]["result"] = business_result(**changes)
    with pytest.raises(trade.CityTradeFlowError) as exc:
        call_task(task_engine)
    assert exc.value.code == "goods_investment_task_incomplete"
    assert investment._INVESTMENT_PROGRESS_CALLBACK.get() is None


@pytest.mark.parametrize("framework", [None, {}, {"nodes": {}}, {"nodes": {"invest": {}}},
                                      {"nodes": {"invest": {"output": []}}}])
def test_missing_child_business_output_is_not_success(task_engine, framework):
    task_engine[2]["framework_result"] = {"status": "SUCCESS", "framework_data": framework}
    with pytest.raises(trade.CityTradeFlowError) as exc:
        call_task(task_engine)
    assert exc.value.code == "goods_investment_task_incomplete"


@pytest.mark.parametrize("status", ["FAILED", "ERROR"])
def test_framework_failure_is_wrapped_and_callback_reset(task_engine, status):
    task_engine[2]["framework_result"] = {"status": status, "error": {"message": "offline failure"}}
    with pytest.raises(trade.CityTradeFlowError) as exc:
        call_task(task_engine)
    assert exc.value.code == "goods_investment_task_failed"
    assert investment._INVESTMENT_PROGRESS_CALLBACK.get() is None


def test_framework_cancellation_propagates_and_restores_previous_callback(task_engine):
    task_engine[2]["framework_result"] = {"status": "CANCELLED"}
    previous = lambda state, data: None
    token = investment._INVESTMENT_PROGRESS_CALLBACK.set(previous)
    try:
        with pytest.raises(asyncio.CancelledError):
            call_task(task_engine)
        assert investment._INVESTMENT_PROGRESS_CALLBACK.get() is previous
    finally:
        investment._INVESTMENT_PROGRESS_CALLBACK.reset(token)


def test_parent_progress_forwards_running_only_with_city_context(task_engine):
    events = []

    class Reporter:
        async def emit(self, stage, state, **fields):
            events.append((stage, state, fields))

        def emit_from_worker(self, stage, state, **fields):
            events.append((stage, state, fields))

    token = trade._ACTIVE_PROGRESS_REPORTER.set(Reporter())
    task_engine[2]["emit_progress"] = True
    try:
        call_task(task_engine)
    finally:
        trade._ACTIVE_PROGRESS_REPORTER.reset(token)
    assert [event[1] for event in events] == ["started", "running", "completed"]
    assert all(stage == "trade_goods_investment" and fields["city_index"] == 1
               for stage, _, fields in events)


def test_standalone_action_uses_same_core_without_parent_callback(monkeypatch):
    captured = {}

    def execute(**params):
        captured.update(params)
        return business_result()

    monkeypatch.setattr(investment, "execute_trade_goods_investment_from_shop", execute)
    assert investment.resonance_pc_invest_trade_goods_from_shop(app=object(), vision=object()) == business_result()
    assert captured["mode"] == 10
    assert captured["city_name"] == ""
    assert captured["progress"] is None


def trade_inputs():
    return {"current_city": "远星大桥", "buy_products": ["扇贝"], "books_used": 2,
            "sell_raise_to_cap": True, "buy_bargain_to_cap": True, "negotiation_max_attempts": 8,
            "app": object(), "ocr": object(), "vision": object(), "city_shop_data": object(),
            "progress_context": {"city_index": 1}}


def test_disabled_wrapper_preserves_old_trade_and_needs_no_engine(monkeypatch):
    received = []
    result = {"success": True, "page_state": "city_main"}
    monkeypatch.setattr(trade, "_execute_city_trade_inside_current_city",
                        lambda **params: received.append(params) or result)
    monkeypatch.setattr(trade, "resonance_pc_click_city_shop_by_name",
                        lambda **params: pytest.fail("Disabled wrapper must not enter twice"))
    params = trade_inputs()
    assert asyncio.run(trade._execute_freight_city_trade(**params)) is result
    assert received == [params]


def install_exchange(monkeypatch, operations):
    entry = {"success": True, "page_state": "shop_page", "shop_name": "交易所"}
    monkeypatch.setattr(trade, "resonance_pc_click_city_shop_by_name",
                        lambda **params: operations.append("exchange") or entry)
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready",
                        lambda *args: operations.append("menu_ready") or {"success": True})
    return entry


@pytest.mark.parametrize("status", ["invested", "skipped"])
def test_enabled_wrapper_enters_once_then_passes_confirmed_entry_to_trade(monkeypatch, task_engine, status):
    context, engine, state = task_engine
    state["result"] = business_result(status=status)
    operations, received = [], []
    entry = install_exchange(monkeypatch, operations)

    def old_trade(**params):
        operations.append("sell_buy_return")
        received.append(params)
        return {"success": True, "page_state": "city_main"}

    monkeypatch.setattr(trade, "_execute_city_trade_inside_current_city", old_trade)
    params = trade_inputs()
    result = asyncio.run(trade._execute_freight_city_trade(
        **params, auto_trade_goods_investment=True, context=context, engine=engine,
    ))
    assert operations == ["exchange", "menu_ready", "sell_buy_return"]
    assert len(state["calls"]) == 1
    assert received == [{**params, "shop_entry": entry}]
    assert result["trade_goods_investment"] == state["result"]


def test_child_failure_prevents_selling_buying_or_returning(monkeypatch, task_engine):
    context, engine, state = task_engine
    state["result"] = business_result(success=False)
    operations = []
    install_exchange(monkeypatch, operations)
    monkeypatch.setattr(trade, "_execute_city_trade_inside_current_city",
                        lambda **params: pytest.fail("Trade must stop after investment failure"))
    with pytest.raises(trade.CityTradeFlowError):
        asyncio.run(trade._execute_freight_city_trade(
            **trade_inputs(), auto_trade_goods_investment=True, context=context, engine=engine,
        ))
    assert operations == ["exchange", "menu_ready"]


def test_enabled_wrapper_missing_engine_rejects_before_game_input(monkeypatch):
    monkeypatch.setattr(trade, "resonance_pc_click_city_shop_by_name",
                        lambda **params: pytest.fail("Missing task engine must not touch the game"))
    with pytest.raises(RuntimeError, match="execution context and engine"):
        asyncio.run(trade._execute_freight_city_trade(**trade_inputs(), auto_trade_goods_investment=True))


def test_registered_child_then_real_city_trade_does_not_reenter_exchange(monkeypatch, task_engine):
    context, engine, _ = task_engine
    operations = []
    entry = install_exchange(monkeypatch, operations)
    original = engine.orchestrator.execute_task

    async def record_task(**params):
        operations.append("investment")
        return await original(**params)

    engine.orchestrator.execute_task = record_task
    monkeypatch.setattr(trade, "resonance_pc_click_shop_menu_node",
                        lambda **params: operations.append(f"node:{params['node_index']}") or {"success": True})
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page",
                        lambda **params: operations.append("sell") or
                        {"success": True, "sold_confirmed": True, "page_state": "shop_page"})
    monkeypatch.setattr(trade, "resonance_pc_buy_goods_on_buy_page",
                        lambda **params: operations.append("buy") or
                        {"success": True, "buy_result": "complete", "page_state": "shop_page",
                         "settlement": {"closed": True}})
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct",
                        lambda **params: operations.append("main") or {"success": True, "page_state": "city_main"})
    result = asyncio.run(trade._execute_freight_city_trade(
        **trade_inputs(), auto_trade_goods_investment=True, context=context, engine=engine,
    ))
    assert operations == ["exchange", "menu_ready", "investment", "menu_ready", "node:2", "sell", "node:1", "buy", "main"]
    assert result["enter_shop"] is entry
    assert result["trade_goods_investment"]["status"] == "invested"


def test_route_child_failure_disables_departure(monkeypatch):
    async def failed_trade(**params):
        raise trade.CityTradeFlowError("goods_investment_task_failed", "offline failure", {})

    monkeypatch.setattr(trade, "_execute_freight_city_trade", failed_trade)
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait",
                        lambda **params: pytest.fail("Failed investment must not depart"))
    with pytest.raises(trade.CityTradeFlowError) as exc:
        asyncio.run(trade._execute_trade_leg(
            index=1, leg={"from_city": "远星大桥", "to_city": "海角城", "buy_products": []},
            sell_raise_to_cap=False, page_state="city_panel", use_fatigue_medicine=False,
            allowed_fatigue_medicines=[], fatigue_medicine_max_uses=1, negotiation_max_attempts=8,
            app=object(), ocr=object(), vision=object(), city_shop_data=object(),
            auto_cape_island_investment=False, auto_rubbish_recycling=False,
            auto_trade_goods_investment=True, context=object(), engine=object(),
        ))
    assert exc.value.code == "goods_investment_task_failed"


@pytest.mark.parametrize("index,city", [(0, "岚心城"), (1, "海角城"), (2, "岚心城")])
def test_route_first_station_excluded_and_repeat_visits_included(monkeypatch, index, city):
    received, operations = [], []

    async def execute_trade(**params):
        received.append(params)
        operations.append("trade")
        return {"success": True, "page_state": "city_main"}

    monkeypatch.setattr(trade, "_execute_freight_city_trade", execute_trade)
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait",
                        lambda **params: operations.append("travel") or {"success": True, "status": "ok"})
    context, engine = object(), object()
    result = asyncio.run(trade._execute_trade_leg(
        index=index, leg={"from_city": city, "to_city": "海角城", "buy_products": []},
        sell_raise_to_cap=False, page_state="city_panel", use_fatigue_medicine=False,
        allowed_fatigue_medicines=[], fatigue_medicine_max_uses=1, negotiation_max_attempts=8,
        app=object(), ocr=object(), vision=object(), city_shop_data=object(),
        auto_cape_island_investment=False, auto_rubbish_recycling=False,
        auto_trade_goods_investment=True, trade_goods_investment_mode=14, context=context, engine=engine,
    ))
    assert result["travel"]["success"]
    assert operations == ["trade", "travel"]
    assert received[0]["auto_trade_goods_investment"] is (index > 0)
    assert received[0]["current_city"] == city
    assert received[0]["trade_goods_investment_mode"] == 14
    assert received[0]["context"] is context and received[0]["engine"] is engine


def test_endpoint_runs_child_after_water_before_final_sale(harness, monkeypatch, task_engine):
    operations, _ = harness
    context, engine, state = task_engine
    install_exchange(monkeypatch, operations)
    original = engine.orchestrator.execute_task

    async def record_task(**params):
        operations.append("goods_investment")
        return await original(**params)

    engine.orchestrator.execute_task = record_task
    result = run_full(auto_trade_goods_investment=True, context=context, engine=engine)
    assert operations.index("water:岚心城:4") < operations.index("goods_investment") < operations.index("final_sale")
    assert operations.count("exchange") == 1
    assert state["calls"][0]["inputs"] == {"mode": 10, "city_name": "岚心城"}
    assert result["final_sale"]["trade_goods_investment"]["status"] == "invested"


def test_endpoint_child_failure_prevents_final_sale(harness, monkeypatch, task_engine):
    operations, _ = harness
    context, engine, state = task_engine
    install_exchange(monkeypatch, operations)
    state["result"] = business_result(page_state="investment_page")
    result = run_full(auto_trade_goods_investment=True, context=context, engine=engine)
    assert "water:岚心城:4" in operations
    assert "final_sale" not in operations
    assert result["success"] is False and result["status"] == "failed"
    assert result["final_sale"]["reason"] == "goods_investment_task_incomplete"


def test_child_sync_action_inherits_scoped_callback_through_real_injector(task_engine, monkeypatch):
    context, engine, state = task_engine
    received = []

    def execute_core(**params):
        received.append(params)
        assert callable(params["progress"])
        params["progress"]("running", {"preview_level": 6})
        return business_result()

    async def execute_task(**params):
        definition = ActionDefinition(
            func=investment.resonance_pc_invest_trade_goods_from_shop,
            name="resonance_pc.invest_trade_goods_from_shop", public=True, read_only=False,
            service_deps={"app": "mock/app", "vision": "mock/vision"},
            plugin=SimpleNamespace(package=SimpleNamespace(canonical_id="@plans/resonance_pc")),
        )
        child_context = SimpleNamespace(data={"cid": "child-investment-cid"})
        adapter = ActionInjector(child_context, engine, Renderer(child_context, engine.state_store),
                                 {"mock/app": object(), "mock/vision": object()})
        result = await adapter._invoke_action(definition, child_context, params["inputs"])
        return {"status": "SUCCESS", "framework_data": {"nodes": {"invest": {"output": result}}}}

    monkeypatch.setattr(investment, "execute_trade_goods_investment_from_shop", execute_core)
    engine.orchestrator.execute_task = execute_task
    assert call_task(task_engine) == business_result()
    assert len(received) == 1 and received[0]["city_name"] == "远星大桥"
    assert investment._INVESTMENT_PROGRESS_CALLBACK.get() is None


def test_task_contract_is_standalone_and_freight_exposes_shared_result():
    tasks = Path(__file__).resolve().parents[2] / "plans/resonance_pc/tasks"
    standalone = yaml.safe_load((tasks / "trade_goods_investment_pc.yaml").read_text(encoding="utf-8"))["trade_goods_investment_pc"]
    assert standalone["meta"]["entry_point"] is True
    assert standalone["steps"]["invest"]["action"] == "resonance_pc.invest_trade_goods_from_shop"
    mode = next(item for item in standalone["meta"]["inputs"] if item["name"] == "mode")
    assert mode["type"] == "number" and mode["strict_integer"] is True
    assert mode["min"] == 1 and mode["max"] == 20 and mode["default"] == 10
    assert "enum" not in mode
    for key in ("success", "status", "mode", "target_level", "transactions", "page_state", "triggered", "city_name"):
        assert standalone["returns"][key] == "{{ nodes.invest.output." + key + " }}"
    freight = yaml.safe_load((tasks / "auto_cycle_trade_pc.yaml").read_text(encoding="utf-8"))["auto_cycle_trade_pc"]
    assert "trade_goods_investment" in freight["returns"]
    for key in ("auto_trade_goods_investment", "trade_goods_investment_mode"):
        assert key in freight["steps"]["run"]["params"]
    combined = yaml.safe_load((tasks / "auto_combined_commerce_pc.yaml").read_text(encoding="utf-8"))["auto_combined_commerce_pc"]
    freight_mode = next(item for item in freight["meta"]["inputs"] if item["name"] == "trade_goods_investment_mode")
    combined_inputs = next(item for item in combined["meta"]["inputs"] if item["name"] == "trade_inputs")
    for mode in (freight_mode, combined_inputs["properties"]["trade_goods_investment_mode"]):
        assert mode["type"] == "number" and mode["strict_integer"] is True
        assert mode["min"] == 1 and mode["max"] == 20 and mode["default"] == 10
        assert "enum" not in mode
