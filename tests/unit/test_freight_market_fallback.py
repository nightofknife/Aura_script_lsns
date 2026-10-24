"""Freight cache policy is explicit; offline fakes never touch the game."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jinja2 import StrictUndefined, Undefined
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions import passenger_flow_pc_actions as passenger
from plans.resonance_pc.src.services.resonance_pc_freight_parameters import FREIGHT_PLANNING_KEYS
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, routes, run_full


ROOT = Path(__file__).resolve().parents[2]
CACHE = {"snapshot_id": "old-market", "stale": True, "stale_reason": "network timeout",
         "fetched_at": "2026-10-17T08:00:00Z"}


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(deepcopy(event.payload))


def install_market(monkeypatch, stale=True):
    snapshot = {**CACHE, "stale": stale}
    if not stale:
        snapshot.update(snapshot_id="new-market", stale_reason=None)
    monkeypatch.setattr(trade, "resonance_pc_market_refresh", lambda **kw: deepcopy(snapshot))


@pytest.mark.parametrize("mode", ["free", "fixed"])
@pytest.mark.parametrize("explicit", [False, True])
def test_cache_default_or_false_blocks_before_planning_and_trade(harness, monkeypatch, mode, explicit):
    operations, _ = harness
    install_market(monkeypatch)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kw: pytest.fail("no plan"))
    bus = Bus()
    result = run_full(trade_mode=mode, fixed_route_city_ids=["15", "11"],
                      event_bus=bus, context=ExecutionContext(cid="blocked-market"),
                      **({"allow_stale_market": False} if explicit else {}))
    assert operations == ["open_panel"]
    assert result["success"] is False and result["status"] == "blocked"
    assert result["reason"] == "stale_market_disallowed"
    assert result["market_fallback_blocked"] is True
    assert result["market_source"] == "fallback_cache" and result["market_refreshed"] is False
    assert result["market_fetched_at"] == CACHE["fetched_at"]
    assert result["market_stale_reason"] == CACHE["stale_reason"]
    assert result["route"] == [] and result["final_sale"] is None
    assert result["execution"]["completed_leg_count"] == 0
    assert result["input_snapshot"]["allow_stale_market"] is False
    assert result["warnings"] and result["error"]["code"] == result["reason"]
    blocked = next(e for e in bus.events if e["stage"] == "market" and e["state"] == "blocked")
    assert blocked["data"]["source"] == "fallback_cache"
    assert blocked["data"]["fetched_at"] == CACHE["fetched_at"]
    assert blocked["data"]["stale_reason"] == CACHE["stale_reason"]
    assert not any(e["stage"] == "planning" for e in bus.events)
    assert bus.events[-1]["stage"] == "task" and bus.events[-1]["state"] == "blocked"
    final = asyncio.run(trade.resonance_pc_finish_auto_cycle_trade(result))
    assert final["success"] is False and final["bento_consumption"]["triggered"] is False
    spec = yaml.safe_load((ROOT / "plans/resonance_pc/tasks/auto_cycle_trade_pc.yaml").read_text("utf-8"))["auto_cycle_trade_pc"]
    env = NativeEnvironment(undefined=StrictUndefined)
    for field, template in spec["returns"].items():
        value = env.from_string(template).render(nodes={"finish": {"output": final}})
        assert not isinstance(value, Undefined), field


@pytest.mark.parametrize("stale,allowed", [(True, True), (False, False), (False, True)])
@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_allowed_cache_and_fresh_continue_with_truthful_provenance(harness, monkeypatch, stale, allowed, mode):
    operations, _ = harness
    install_market(monkeypatch, stale)
    planner_calls = []
    def plan(**kwargs):
        planner_calls.append(kwargs)
        return {"status": "ok", "route": routes(), "warnings": ["existing warning"]}
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", plan)
    bus = Bus()
    result = run_full(trade_mode=mode, fixed_route_city_ids=["15", "11", "15"],
                      allow_stale_market=allowed, auto_sparkling_water=False,
                      context=ExecutionContext(cid="market"), event_bus=bus)
    assert result["success"] is True and "final_sale" in operations
    assert result["market_source"] == ("fallback_cache" if stale else "refresh")
    assert result["market_refreshed"] is not stale and result["market_fallback_blocked"] is False
    assert result["input_snapshot"]["allow_stale_market"] is allowed
    assert "allow_stale_market" not in planner_calls[0]
    assert "existing warning" in result["warnings"]
    assert any("历史行情" in warning for warning in result["warnings"]) is stale
    market = next(e for e in bus.events if e["stage"] == "market" and e["state"] == "completed")
    assert market["data"]["source"] == result["market_source"]
    assert market["data"]["fetched_at"] == CACHE["fetched_at"]
    planned = next(e for e in bus.events if e["stage"] == "planning" and e["state"] == "completed")
    assert planned["data"]["summary"]["market_source"] == result["market_source"]
    assert planned["data"]["summary"]["market_fetched_at"] == CACHE["fetched_at"]
    assert planned["data"]["summary"]["market_fallback_blocked"] is False


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_policy_requires_actual_bool_before_game_input(harness, value):
    operations, _ = harness
    with pytest.raises(ValueError, match="allow_stale_market must be a boolean"):
        run_full(allow_stale_market=value)
    assert operations == []


@pytest.mark.parametrize("allowed", [False, True])
def test_missing_cache_preserves_refresh_error(harness, monkeypatch, allowed):
    operations, _ = harness
    error = RuntimeError("refresh failed and no cache available")
    def refresh(**kwargs):
        raise error
    monkeypatch.setattr(trade, "resonance_pc_market_refresh", refresh)
    with pytest.raises(RuntimeError) as caught:
        run_full(allow_stale_market=allowed)
    assert caught.value is error and operations == ["open_panel"]


def test_event_delivery_failure_does_not_bypass_block(harness, monkeypatch):
    install_market(monkeypatch)
    class BrokenBus:
        async def publish(self, event):
            raise RuntimeError("offline event delivery")
    result = run_full(event_bus=BrokenBus(), context=ExecutionContext(cid="market"))
    assert result["success"] is False and result["market_fallback_blocked"] is True
    assert harness[0] == ["open_panel"]


def test_preview_still_allows_cache_and_passenger_still_skips_buy(monkeypatch):
    install_market(monkeypatch)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kw: {"status": "ok", "route": routes()})
    bus = Bus()
    result = asyncio.run(trade.resonance_pc_preview_trade_plan_flow(
        start_city_id="15", resonance_pc_market_data=object(), resonance_pc_trade_planner=object(),
        event_bus=bus, context=ExecutionContext(cid="preview-market")))
    assert result["success"] is True and result["market_source"] == "fallback_cache"
    market = next(e for e in bus.events if e["stage"] == "market" and e["state"] == "completed")
    assert market["data"]["fetched_at"] == CACHE["fetched_at"]
    monkeypatch.setattr(passenger, "resonance_pc_market_refresh", lambda **kw: deepcopy(CACHE))
    monkeypatch.setattr(passenger, "resonance_pc_trade_plan_optimal_route", lambda **kw: pytest.fail("no cached passenger plan"))
    result = passenger._prepare_passenger_trade_plan(
        source_city_id="15", destination_city_id="11", route_by_id={"15": {}, "11": {}},
        market_data=object(), trade_planner=object())
    assert result["reason"] == "stale_market_rejected" and result["buy_products"] == []


def test_flag_schema_default_forwarding_and_execution_only_contract():
    assert "allow_stale_market" in combined._TRADE_INPUT_KEYS
    assert "allow_stale_market" not in combined._PREVIEW_INPUT_KEYS
    assert "allow_stale_market" not in combined._PASSENGER_INPUT_KEYS
    assert "allow_stale_market" not in FREIGHT_PLANNING_KEYS
    spec = yaml.safe_load((ROOT / "plans/resonance_pc/tasks/auto_cycle_trade_pc.yaml").read_text("utf-8"))["auto_cycle_trade_pc"]
    validator = InputValidator(None)
    env = NativeEnvironment(undefined=StrictUndefined)
    for provided, expected in [({}, False), ({"allow_stale_market": True}, True)]:
        ok, inputs = validator.validate_inputs_against_meta(spec["meta"]["inputs"], provided)
        assert ok, inputs
        assert env.from_string(spec["steps"]["run"]["params"]["allow_stale_market"]).render(inputs=inputs) is expected
    combined_spec = yaml.safe_load((ROOT / "plans/resonance_pc/tasks/auto_combined_commerce_pc.yaml").read_text("utf-8"))["auto_combined_commerce_pc"]
    trade_schema = next(item for item in combined_spec["meta"]["inputs"] if item["name"] == "trade_inputs")
    assert trade_schema["properties"]["allow_stale_market"]["default"] is False


def _combined_args():
    return dict(total_fatigue_budget=100, app=object(), ocr=object(), vision=object(),
                resonance_pc_city_shop_data=object(), resonance_pc_market_data=object(),
                resonance_pc_trade_planner=object(), state_store=object(), event_bus=object(),
                context=object(), engine=object())


@pytest.mark.parametrize("value", ["false", "true", 1, None])
def test_combined_rejects_non_bool_before_starting_tasks(monkeypatch, value):
    monkeypatch.setattr(combined, "_build_passenger_route", lambda **kw: pytest.fail("preflight first"))
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        trade_inputs={"allow_stale_market": value}, **_combined_args()))
    assert result["success"] is False and result["reason"] == "invalid_trade_inputs"


def test_combined_forwards_flag_and_does_not_run_passenger_after_cache_block(monkeypatch):
    monkeypatch.setattr(combined, "_build_passenger_route", lambda **kw: {
        "city_a_id": "11", "city_b_id": "15", "trip_fatigue": 20})
    async def freight(inputs, **kwargs):
        assert inputs["allow_stale_market"] is False
        return {"success": False, "status": "blocked", "reason": "stale_market_disallowed",
                "market_fallback_blocked": True, "route": []}
    async def passenger_forbidden(*args, **kwargs):
        pytest.fail("must stop sequence after freight cache rejection")
    monkeypatch.setattr(combined, "_run_trade", freight)
    monkeypatch.setattr(combined, "_run_passenger", passenger_forbidden)
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        order="trade_first", trade_inputs={"allow_stale_market": False,
                                           "available_city_ids": ["11", "15"]}, **_combined_args()))
    assert result["success"] is False and result["reason"] == "trade_failed"
    assert result["trade"]["market_fallback_blocked"] is True
