"""Freight cache policy is explicit; offline fakes never touch the game."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import ssl
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
import yaml
from jinja2 import StrictUndefined, Undefined
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.services import resonance_pc_market_data_service as market
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


def test_preview_still_allows_cache(monkeypatch):
    install_market(monkeypatch)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kw: {"status": "ok", "route": routes()})
    bus = Bus()
    result = asyncio.run(trade.resonance_pc_preview_trade_plan_flow(
        start_city_id="15", resonance_pc_market_data=object(), resonance_pc_trade_planner=object(),
        event_bus=bus, context=ExecutionContext(cid="preview-market")))
    assert result["success"] is True and result["market_source"] == "fallback_cache"
    market = next(e for e in bus.events if e["stage"] == "market" and e["state"] == "completed")
    assert market["data"]["fetched_at"] == CACHE["fetched_at"]


@pytest.mark.parametrize("preview", [False, True])
def test_manual_market_proxy_is_forwarded_to_refresh_only(harness, monkeypatch, preview):
    refresh_calls = []
    planner_calls = []

    def refresh(**kwargs):
        refresh_calls.append(kwargs)
        return {"snapshot_id": "new-market"}

    def plan(**kwargs):
        planner_calls.append(kwargs)
        return {"status": "ok", "route": routes()}

    monkeypatch.setattr(trade, "resonance_pc_market_refresh", refresh)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", plan)
    settings = {"market_proxy_mode": "manual", "market_proxy_port": 7890}
    if preview:
        result = asyncio.run(trade.resonance_pc_preview_trade_plan_flow(
            start_city_id="15", resonance_pc_market_data=object(), resonance_pc_trade_planner=object(),
            **settings,
        ))
    else:
        result = run_full(auto_sparkling_water=False, **settings)

    assert result["success"] is True
    assert len(refresh_calls) == len(planner_calls) == 1
    assert {key: refresh_calls[0][key] for key in settings} == settings
    assert not (settings.keys() & planner_calls[0].keys())


def test_manual_market_proxy_overrides_windows_bypass_without_automatic_retry(tmp_path, monkeypatch):
    service = market.ResonancePcMarketDataService(plan_root=tmp_path)
    monkeypatch.setattr(market, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(market.urllib.request, "proxy_bypass", lambda _host: True)
    monkeypatch.setattr(market, "get_market_url", lambda *a, **k: pytest.fail("manual mode auto-retried"))
    proxy_hosts = []

    def build_opener(proxy_handler, _redirect_handler):
        def open_request(request, *, timeout):
            proxy_handler.https_open(request)
            proxy_hosts.append(request.host)
            raise URLError(TimeoutError("offline test"))
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(market.urllib.request, "build_opener", build_opener)
    with pytest.raises(market.ResonancePcMarketDataError, match="127.0.0.1:7890") as caught:
        service.fetch_raw(proxy_port=7890)
    assert caught.value.code == "market_fetch_failed"
    assert proxy_hosts == ["127.0.0.1:7890"]


@pytest.mark.parametrize("failure,expected_error", [
    (URLError(TimeoutError("offline test")), None),
    (URLError(ssl.SSLCertVerificationError("offline test")), "market_fetch_failed"),
    (HTTPError("https://example.invalid/market", 503, "Unavailable", {}, None), "market_fetch_http_failed"),
])
def test_windows_market_proxy_retries_only_transient_transport_failure(tmp_path, monkeypatch, failure, expected_error):
    service = market.ResonancePcMarketDataService(plan_root=tmp_path, timeout_seconds=20)
    monkeypatch.setattr(market, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(market.urllib.request, "getproxies", lambda: {})
    fallback_calls = []

    def open_request(request, *, timeout):
        assert request.full_url == service.api_url and timeout == 10
        raise failure

    def windows_proxy(url, *, user_agent, timeout_seconds):
        fallback_calls.append(url)
        assert 0 < timeout_seconds <= service.timeout_seconds
        return b'{"data": {}}'

    monkeypatch.setattr(market.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=open_request))
    monkeypatch.setattr(market, "get_market_url", windows_proxy)
    if expected_error is None:
        assert service.fetch_raw() == {"data": {}}
        assert fallback_calls == [service.api_url]
    else:
        with pytest.raises(market.ResonancePcMarketDataError) as caught:
            service.fetch_raw()
        assert caught.value.code == expected_error
        assert fallback_calls == []


def test_flag_schema_default_forwarding_and_execution_only_contract():
    assert "allow_stale_market" not in FREIGHT_PLANNING_KEYS
    spec = yaml.safe_load((ROOT / "plans/resonance_pc/tasks/auto_cycle_trade_pc.yaml").read_text("utf-8"))["auto_cycle_trade_pc"]
    validator = InputValidator(None)
    env = NativeEnvironment(undefined=StrictUndefined)
    for provided, expected in [({}, False), ({"allow_stale_market": True}, True)]:
        ok, inputs = validator.validate_inputs_against_meta(spec["meta"]["inputs"], provided)
        assert ok, inputs
        assert env.from_string(spec["steps"]["run"]["params"]["allow_stale_market"]).render(inputs=inputs) is expected
