"""Offline freight modes, task boundaries and execution safety; no game input."""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import trade_planner_pc_actions as planner_action
from plans.resonance_pc.src.actions._freight_contract import normalize_planning_inputs, plan_view
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, routes, run_full
from tests.unit.test_resonance_pc_freight_planner_service import service


ROOT = Path(__file__).resolve().parents[2]


def test_freight_tasks_load_with_nullable_and_strict_integer_schema():
    from packages.aura_core.config.validator import get_task_schema_validator
    from packages.aura_core.packaging.core.task_loader import TaskLoader

    assert get_task_schema_validator() is not None
    loader = TaskLoader("resonance_pc", ROOT / "plans/resonance_pc")
    for name in ("auto_cycle_trade_pc", "preview_trade_plan_pc", "auto_combined_commerce_pc"):
        assert loader.get_task_data(name) is not None
    assert loader.get_task_load_errors() == []


@pytest.mark.parametrize("mode", ["profit", "quick", "fixed", "target"])
@pytest.mark.parametrize("books", [0, 2, None])
def test_task_schema_to_action_preserves_modes_and_nullable_books(mode, books):
    for name, action in (("auto_cycle_trade_pc", trade.resonance_pc_auto_cycle_trade_flow),
                         ("preview_trade_plan_pc", trade.resonance_pc_preview_trade_plan_flow)):
        spec = yaml.safe_load((ROOT / "plans/resonance_pc/tasks" / f"{name}.yaml").read_text("utf-8"))[name]
        provided = {"trade_mode": mode, "book_budget": books,
                    "fixed_route_city_ids": ["15", "11", "15"], "target_profit": 1000000}
        if name == "preview_trade_plan_pc":
            provided["start_city_id"] = "15"
        ok, inputs = InputValidator(None).validate_inputs_against_meta(spec["meta"]["inputs"], provided)
        assert ok, inputs
        rendered = {key: NativeEnvironment().from_string(value).render(inputs=inputs)
                    for key, value in spec["steps"]["run"]["params"].items()}
        inspect.signature(action).bind(**rendered)
        assert rendered["book_budget"] == books
        assert rendered["fatigue_budget"] == 700 and rendered["cargo_capacity"] == 750
        assert rendered["book_profit_threshold"] == 500000
        assert not {"auto_book", "trade_level", "active_events", "fixed_route_repeat_count"} & rendered.keys()
        if name == "auto_cycle_trade_pc":
            assert rendered["auto_cape_island_investment"] is True


def test_nullable_does_not_change_missing_default_or_other_schemas():
    validator = InputValidator(None)
    schema = [{"name": "books", "type": "number", "default": 0, "nullable": True}]
    assert validator.validate_inputs_against_meta(schema, {}) == (True, {"books": 0})
    assert validator.validate_inputs_against_meta(schema, {"books": None}) == (True, {"books": None})
    schema[0].pop("nullable")
    assert validator.validate_inputs_against_meta(schema, {"books": None}) == (True, {"books": 0})
    nested = [{"name": "config", "type": "dict", "properties": {
        "books": {"type": "number", "nullable": True}}}]
    assert validator.validate_inputs_against_meta(nested, {"config": {"books": None}}) == (
        True, {"config": {"books": None}})
    assert validator.validate_inputs_against_meta(nested, {"config": {}}) == (True, {"config": {}})


@pytest.mark.parametrize("value", [True, False, 1.5, "2"])
def test_integer_task_fields_are_not_silently_coerced(value):
    path = ROOT / "plans/resonance_pc/tasks/preview_trade_plan_pc.yaml"
    spec = yaml.safe_load(path.read_text("utf-8"))["preview_trade_plan_pc"]
    ok, _ = InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"], {"start_city_id": "15", "book_budget": value})
    assert not ok


@pytest.mark.parametrize("field,value", [("fatigue_budget", True), ("cargo_capacity", 1.5),
                                        ("book_budget", -1), ("book_profit_threshold", float("nan")),
                                        ("target_profit", 0), ("bargain_success_rates_bps", [False])])
def test_invalid_planning_inputs_are_rejected_before_game(harness, field, value):
    operations, _ = harness
    with pytest.raises(ValueError):
        run_full(**{"trade_mode": "target", "target_profit": 1000000, field: value})
    assert operations == []


@pytest.mark.parametrize("mode", ["profit", "quick", "fixed", "target"])
def test_runtime_forwards_canonical_request_without_old_fields(harness, monkeypatch, mode):
    calls = []
    def planner(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "route": routes(), "book_budget": None, "books_used": 3}
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", planner)
    result = run_full(trade_mode=mode, book_budget=None, target_profit=1000000,
                      fixed_route_city_ids=["15", "11", "15"], auto_sparkling_water=False)
    assert result["success"] is True and result["status"] == "completed"
    assert calls[0]["trade_mode"] == mode and calls[0]["book_budget"] is None
    assert result["remaining_books"] is None and result["book_budget_unlimited"] is True
    assert "auto_book" not in calls[0] and "all_plan" not in calls[0]
    if mode == "quick":
        assert calls[0]["book_policy"] == "fill" and calls[0]["negotiation_policy"] == "required"


def test_planner_action_matches_new_service_signature():
    calls = []
    class Planner:
        def plan_optimal_route(self, **kwargs):
            calls.append(kwargs)
            return {"status": "ok"}
    planner_action.resonance_pc_trade_plan_optimal_route(
        book_budget=None, trade_mode="quick", resonance_pc_trade_planner=Planner())
    assert calls[0]["book_budget"] is None and calls[0]["negotiation_budget"] is None
    assert not {"auto_book", "all_plan", "trade_level", "active_events"} & calls[0].keys()


@pytest.mark.parametrize("mode,status", [("profit", "no_plan"), ("target", "target_unreachable"),
                                       ("fixed", "fixed_route_infeasible")])
def test_unreachable_is_stopped_without_sale_or_recovery(harness, monkeypatch, mode, status):
    operations, _ = harness
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route",
                        lambda **kwargs: {"status": status, "reason": status, "route": []})
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct",
                        lambda **kwargs: {"success": True, "page_state": "city_main"})
    result = run_full(trade_mode=mode, target_profit=1000000,
                      fixed_route_city_ids=["15", "11", "15"], auto_bento=True)
    assert result["status"] == "stopped" and result["success"] is False
    assert result["planning_status"] == status and result["bento_pending"] is False
    assert result["final_sale"] is None and operations == ["open_panel"]


def test_fixed_wrong_start_stops_without_planner_or_departure(harness, monkeypatch):
    operations, _ = harness
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route",
                        lambda **kw: pytest.fail("must not plan from mismatched start"))
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct",
                        lambda **kw: {"success": True, "page_state": "city_main"})
    result = run_full(trade_mode="fixed", fixed_route_city_ids=["11", "15", "11"])
    assert result["reason"] == "fixed_start_mismatch" and result["success"] is False
    assert operations == ["open_panel"]


@pytest.mark.parametrize("blocked", [False, True])
def test_reposition_is_navigation_only_and_precedes_trade(harness, monkeypatch, blocked):
    operations, _ = harness
    leg = routes()[0]
    planned = {"status": "ok", "route": [routes()[1]], "reposition_route": [leg],
               "reposition_expected_fatigue": 100, "expected_fatigue_used": 200}
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kw: deepcopy(planned))
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct",
                        lambda **kw: {"success": True, "page_state": "city_main"})
    def travel(**kwargs):
        operations.append("reposition_travel")
        assert kwargs["auto_pickup"] is False
        return {"success": not blocked, "status": "blocked" if blocked else "ok",
                "page_state": "city_main"}
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    def read_city(**kwargs):
        return {"city_name": "海角城" if "reposition_travel" in operations else "岚心城",
                "city_key": "cape_city" if "reposition_travel" in operations else "lanxin_city"}
    monkeypatch.setattr(trade, "resonance_pc_read_city_name_on_city_panel", read_city)
    result = run_full(trade_mode="fixed", fixed_route_city_ids=["11", "15", "11"],
                      reposition_to_route=True, auto_sparkling_water=False)
    assert result["reposition"]["expected_fatigue"] == 100
    if blocked:
        assert result["status"] == "blocked" and result["final_sale"] is None
        assert operations == ["open_panel", "reposition_travel"]
    else:
        assert result["status"] == "completed"
        assert operations.index("reposition_travel") < operations.index("trade:0")
        assert result["city_visits"][0]["city_id"] == "11"


def test_preview_is_planned_and_matches_execution_request(monkeypatch):
    calls = []
    monkeypatch.setattr(trade, "resonance_pc_market_refresh", lambda **kw: {"snapshot_id": "snapshot"})
    def planner(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "route": routes(), "book_budget": None}
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", planner)
    result = asyncio.run(trade.resonance_pc_preview_trade_plan_flow(
        start_city_id="15", trade_mode="quick", book_budget=None,
        resonance_pc_market_data=object(), resonance_pc_trade_planner=object()))
    assert result["status"] == "planned" and result["planning_status"] == "ok"
    assert result["success"] is True and calls[0]["book_budget"] is None
    assert result["request_kind"] == "preview"


def test_repeated_city_indices_and_terminal_phases_are_explicit():
    request = normalize_planning_inputs({"trade_mode": "fixed", "fixed_route_city_ids": ["15", "11", "15"]})
    result = plan_view({"status": "ok", "route": routes()}, request, kind="run", auto_bento=True)
    visits = result["city_visits"]
    assert [row["city_index"] for row in visits] == [0, 1, 2]
    assert visits[0]["city_id"] == visits[2]["city_id"]
    assert [row["key"] for row in visits[-1]["phases"]][-2:] == ["final_sale", "bento"]
    assert "buy" not in [row["key"] for row in visits[-1]["phases"]]


def test_incomplete_selection_skips_purchase_and_confirms_shop_return(monkeypatch):
    monkeypatch.setattr(trade, "load_product_templates", lambda: object())
    monkeypatch.setattr(trade, "select_buy_products", lambda **kw: {
        "selected_products": [], "selected_product_ids": [], "missing_products": ["missing"],
        "warnings": [{"code": "trade_product_not_selected"}], "scan_trace": [],
        "stop_reason": "list_end"})
    monkeypatch.setattr(trade, "execute_bargain_to_cap", lambda **kw: pytest.fail("must not bargain"))
    monkeypatch.setattr(trade, "_wait_for_text_hit", lambda *args, **kw: pytest.fail("must not buy"))
    monkeypatch.setattr(trade, "resonance_pc_tap_back_once", lambda **kw: {"page_state": "previous"})
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", lambda *args: {"ready": True})
    result = trade.resonance_pc_buy_goods_on_buy_page(["missing"], bargain_to_cap=True,
                        max_scan_rounds=1, app=object(), ocr=object(), vision=object())
    assert result["buy_result"] == "skipped" and result["success"] is True
    assert result["page_state"] == "shop_page" and result["missing_products"] == ["missing"]


def test_unconfirmed_books_stop_before_selecting_products(monkeypatch):
    monkeypatch.setattr(trade, "load_product_templates", lambda: object())
    monkeypatch.setattr(trade, "execute_bargain_to_cap", lambda **kw: {})
    monkeypatch.setattr(trade, "resonance_pc_use_purchase_books", lambda **kw: {"ok": True, "used": 1})
    monkeypatch.setattr(trade, "select_buy_products", lambda **kw: pytest.fail("must not scan"))
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_buy_goods_on_buy_page(["product"], books_used=2,
                                               app=object(), ocr=object(), vision=object())
    assert error.value.code == "purchase_books_not_confirmed"


@pytest.mark.parametrize("mode", ["profit", "quick", "fixed", "target"])
def test_real_service_is_wired_to_preview_and_execution(service, harness, monkeypatch, mode):
    api, _ = service
    monkeypatch.setattr(api.market_data, "get_snapshot", lambda **kw: api.market_data.get_latest(), raising=False)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route",
                        planner_action.resonance_pc_trade_plan_optimal_route)
    monkeypatch.setattr(trade, "resonance_pc_read_city_name_on_city_panel",
                        lambda **kw: {"city_name": "A", "city_key": "A"})
    request = {"trade_mode": mode, "fatigue_budget": 100, "cargo_capacity": 3,
               "book_budget": None, "book_profit_threshold": 0,
               "fixed_route_city_ids": ["A", "B", "A"], "target_profit": 20,
               "resonance_pc_market_data": api.market_data, "resonance_pc_trade_planner": api,
               "bargain_success_rates_bps": [10000], "raise_success_rates_bps": [10000]}
    preview = asyncio.run(trade.resonance_pc_preview_trade_plan_flow(start_city_id="A", **request))
    actual = run_full(auto_sparkling_water=False, **request)
    assert preview["status"] == "planned" and actual["status"] == "completed"
    assert preview["route"] == actual["route"]
    assert preview["expected_fatigue_used_exact"] == actual["expected_fatigue_used_exact"]
    assert preview["book_budget"] is None and actual["resources"]["actual_profit"] is None


@pytest.mark.parametrize("code,status", [("buy_transaction_not_confirmed", "failed"), ("trade_cancelled", "cancelled")])
def test_structured_trade_failure_retains_completed_legs_and_never_clears_endpoint(harness, monkeypatch, code, status):
    operations, _ = harness
    original = trade._execute_trade_leg
    async def leg(**kwargs):
        if kwargs["index"] == 1:
            raise trade.CityTradeFlowError(code, "stopped", {"book_result": {"used": 1}})
        return await original(**kwargs)
    monkeypatch.setattr(trade, "_execute_trade_leg", leg)
    result = run_full(auto_sparkling_water=False, auto_bento=True)
    assert result["success"] is False and result["status"] == status
    assert result["execution"]["completed_leg_count"] == 1
    assert result["execution"]["leg_results"][-1]["error"]["detail"]["book_result"]["used"] == 1
    assert result["final_sale"] is None and "final_sale" not in operations
    assert result["bento_pending"] is False
    assert result["resources"]["confirmed_books_used"] == 1


def test_first_station_sale_does_not_raise_but_incoming_goods_do(harness, monkeypatch):
    seen = []
    original = trade._execute_trade_leg
    planned = routes()
    planned[0]["raise_to_cap"] = True
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route",
                        lambda **kw: {"status": "ok", "route": planned})
    async def leg(**kwargs):
        seen.append(kwargs["sell_raise_to_cap"])
        return await original(**kwargs)
    monkeypatch.setattr(trade, "_execute_trade_leg", leg)
    run_full(trade_mode="quick", auto_sparkling_water=False)
    assert seen == [False, True]
