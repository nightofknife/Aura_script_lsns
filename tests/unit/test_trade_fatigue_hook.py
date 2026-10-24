"""Real city-trade sequencing samples fatigue after settlement and before exit."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(
        calls=[], fatigue=100, settlement_closed=False, page="city_main", reader_error=None,
        observation=None, main={"success": True, "page_state": "city_main"},
        sell={"success": True, "sold_confirmed": True, "page_state": "shop_page",
              "sell_result": "complete", "settlement": {"closed": True},
              "raise": {"actual_fatigue_used": 8}},
        buy={"success": True, "page_state": "shop_page", "buy_result": "complete",
             "selected_products": ["goods"], "settlement": {"closed": True},
             "bargain": {"actual_fatigue_used": 16}},
    )

    def enter(**kwargs):
        state.calls.append("enter")
        state.page = "shop_page"
        return {"success": True, "page_state": "shop_page", "menu_ready": {"ready": True}}

    def ready(*args, **kwargs):
        state.calls.append("ready")
        return {"ready": True}

    def node(*, node_index, **kwargs):
        state.calls.append("sell_node" if node_index == 2 else "buy_node")
        state.page = "sell_page" if node_index == 2 else "buy_page"
        state.settlement_closed = False
        return {"clicked": True}

    def sell(**kwargs):
        state.calls.append("sell_negotiation")
        assert kwargs["raise_to_cap"] is True
        state.fatigue += 8
        state.calls.append("sell_settlement_closed")
        state.settlement_closed = True
        state.page = state.sell["page_state"]
        return deepcopy(state.sell)

    def buy(**kwargs):
        state.calls.append("buy_negotiation")
        assert kwargs["bargain_to_cap"] is True
        state.fatigue += 16
        state.calls.append("buy_settlement_closed")
        state.settlement_closed = True
        state.page = state.buy["page_state"]
        return deepcopy(state.buy)

    def read(**kwargs):
        state.calls.append("fatigue")
        assert state.page == "shop_page" and state.settlement_closed
        assert kwargs["check_cancelled"] is trade._check_trade_cancelled
        if state.reader_error is not None:
            raise state.reader_error
        return deepcopy(state.observation if state.observation is not None else {
            "success": True, "current_fatigue": state.fatigue, "max_fatigue": 872,
            "source": "trade_page", "reason": "stable", "attempts": 2})

    def main(**kwargs):
        state.calls.append("main")
        state.page = state.main["page_state"]
        return deepcopy(state.main)

    monkeypatch.setattr(trade, "_enter_exchange", enter)
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", ready)
    monkeypatch.setattr(trade, "resonance_pc_click_shop_menu_node", node)
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page", sell)
    monkeypatch.setattr(trade, "resonance_pc_buy_goods_on_buy_page", buy)
    monkeypatch.setattr(trade, "read_trade_fatigue", read)
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", main)

    def run(*, scoped=False, purchase=True, capture=True, reuse_entry=False):
        action = (trade._execute_city_trade_inside_current_city_scoped if scoped
                  else trade._execute_city_trade_inside_current_city)
        state.page = "shop_page" if reuse_entry else "city_main"
        return action(
            current_city="city-a", buy_products=["goods"] if purchase else [], books_used=0,
            sell_raise_to_cap=True, buy_bargain_to_cap=purchase,
            app=object(), ocr=object(), vision=object(), city_shop_data=object(),
            capture_fatigue=capture,
            shop_entry=({"success": True, "page_state": "shop_page"} if reuse_entry else None),
        )

    state.run = run
    return state


@pytest.mark.parametrize("scoped", [False, True])
def test_sell_then_buy_reads_once_after_latest_negotiation_and_settlement(rig, scoped):
    result = rig.run(scoped=scoped)
    assert rig.calls == ["enter", "sell_node", "sell_negotiation", "sell_settlement_closed",
                         "buy_node", "buy_negotiation", "buy_settlement_closed", "fatigue", "main"]
    assert result["fatigue_observation"]["current_fatigue"] == 124
    assert result["success"] is True and result["page_state"] == "city_main"
    assert result["sell"]["sold_confirmed"] is True and result["buy"]["settlement"]["closed"]


@pytest.mark.parametrize("scoped", [False, True])
def test_sale_only_reads_once_before_exit(rig, scoped):
    result = rig.run(scoped=scoped, purchase=False)
    assert rig.calls == ["enter", "sell_node", "sell_negotiation", "sell_settlement_closed", "fatigue", "main"]
    assert result["buy"] is None
    assert result["fatigue_observation"]["current_fatigue"] == 108


@pytest.mark.parametrize("purchase", [False, True])
def test_disabled_hook_adds_no_observation(rig, purchase):
    result = rig.run(purchase=purchase, capture=False)
    assert "fatigue" not in rig.calls and rig.calls[-1] == "main"
    assert result["fatigue_observation"] is None


def test_failed_observation_keeps_confirmed_trade_and_restores_main_for_fallback(rig):
    rig.observation = {"success": False, "current_fatigue": None, "max_fatigue": None,
                       "source": "trade_page", "reason": "unstable_fatigue", "attempts": 4}
    result = rig.run()
    assert result["success"] is True and result["page_state"] == "city_main"
    assert result["fatigue_observation"] == rig.observation
    assert rig.calls[-2:] == ["fatigue", "main"]
    assert result["sell"]["sold_confirmed"] and result["buy"]["settlement"]["closed"]


def test_cooperative_reader_cancellation_keeps_code_and_confirmed_transactions(rig):
    error = trade.CityTradeFlowError("trade_cancelled", "cancelled", {"reader": "cancelled"})
    rig.reader_error = error
    with pytest.raises(trade.CityTradeFlowError) as caught:
        rig.run()
    assert caught.value is error and caught.value.code == "trade_cancelled"
    assert caught.value.detail["reader"] == "cancelled"
    confirmed = caught.value.detail["city_trade"]
    assert confirmed["success"] is True and confirmed["page_state"] == "shop_page"
    assert confirmed["sell"]["sold_confirmed"] is True
    assert confirmed["buy"]["settlement"]["closed"] is True
    assert "main" not in rig.calls


def test_async_reader_cancellation_propagates_without_exiting_main(rig):
    error = asyncio.CancelledError()
    rig.reader_error = error
    with pytest.raises(asyncio.CancelledError) as caught:
        rig.run()
    assert caught.value is error and "main" not in rig.calls
    confirmed = caught.value.detail["city_trade"]
    assert confirmed["success"] is True and confirmed["page_state"] == "shop_page"
    assert confirmed["current_city"] == "city-a"
    assert confirmed["sell"]["sold_confirmed"] is True
    assert confirmed["buy"]["settlement"]["closed"] is True


@pytest.mark.parametrize("main", [{"success": False, "page_state": "unknown"},
                                   {"success": True, "page_state": "shop_page"}])
def test_failed_main_navigation_is_not_reported_as_completed_trade(rig, main):
    rig.main = main
    with pytest.raises(trade.CityTradeFlowError) as caught:
        rig.run()
    assert caught.value.code == "trade_main_not_restored"
    assert rig.calls[-2:] == ["fatigue", "main"]
    confirmed = caught.value.detail["city_trade"]
    assert confirmed["success"] is True and confirmed["page_state"] == "shop_page"
    assert confirmed["sell"]["sold_confirmed"] is True
    assert confirmed["buy"]["settlement"]["closed"] is True
    assert confirmed["fatigue_observation"]["current_fatigue"] == 124
    assert confirmed["fatigue_observation"]["source"] == "trade_page"


@pytest.mark.parametrize("stage", ["sell", "buy"])
def test_unconfirmed_transaction_never_reads_fatigue_or_exits_main(rig, stage):
    getattr(rig, stage)["success"] = False
    with pytest.raises(trade.CityTradeFlowError) as caught:
        rig.run()
    assert caught.value.code == f"{stage}_transaction_not_confirmed"
    assert "fatigue" not in rig.calls and "main" not in rig.calls


def test_investment_shop_entry_reuse_still_reads_only_after_trade(rig):
    result = rig.run(reuse_entry=True)
    assert "enter" not in rig.calls and rig.calls[0] == "ready"
    assert rig.calls[-3:] == ["buy_settlement_closed", "fatigue", "main"]
    assert result["fatigue_observation"]["current_fatigue"] == 124
