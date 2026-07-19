from __future__ import annotations

import asyncio

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as actions


class _MemoryStateStore:
    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def set(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


async def _no_sleep(_seconds: float) -> None:
    return None


def _route() -> list[dict]:
    return [
        {
            "from_city": "A",
            "to_city": "B",
            "buy_products": ["A1", "A2"],
            "books_used": 2,
            "bargain_to_cap": True,
            "raise_to_cap": True,
        },
        {
            "from_city": "B",
            "to_city": "C",
            "buy_products": [],
            "books_used": 0,
            "bargain_to_cap": False,
            "raise_to_cap": True,
        },
    ]


def _patch_route_ui(monkeypatch, events: list, *, blocked_destination: str | None = None) -> None:
    def open_city_panel(**_kwargs):
        events.append(("open_city_panel",))
        return {"success": True, "page_state": "city_panel"}

    def city_trade(
        *,
        current_city,
        buy_products,
        books_used,
        sell_raise_to_cap=False,
        buy_bargain_to_cap=False,
        **_kwargs,
    ):
        events.append(
            (
                "city_trade",
                current_city,
                list(buy_products),
                books_used,
                bool(sell_raise_to_cap),
                bool(buy_bargain_to_cap),
            )
        )
        return {
            "success": current_city != "A",
            "page_state": "city_main",
            "current_city": current_city,
            "soft_result": "unconfirmed" if current_city == "A" else "confirmed",
        }

    def travel(*, to_city_name, **_kwargs):
        events.append(("travel", to_city_name))
        if to_city_name == blocked_destination:
            return {
                "success": False,
                "status": "blocked",
                "reason": "fatigue_recovery_required",
                "blocked_at": "departure",
                "fatigue_medicine_used": [],
                "fatigue_medicine_use_count": 0,
            }
        return {
            "success": True,
            "status": "ok",
            "arrival_status": "arrived",
            "fatigue_medicine_used": [],
            "fatigue_medicine_use_count": 0,
        }

    monkeypatch.setattr(actions, "resonance_pc_open_city_panel_from_main", open_city_panel)
    monkeypatch.setattr(actions, "_execute_city_trade_inside_current_city", city_trade)
    monkeypatch.setattr(actions, "resonance_pc_intercity_depart_and_wait", travel)
    monkeypatch.setattr(actions.asyncio, "sleep", _no_sleep)


def _patch_planning(monkeypatch, events: list, plan: dict) -> None:
    def open_city_panel(**_kwargs):
        events.append(("open_city_panel",))
        return {"success": True, "page_state": "city_panel"}

    def read_city(**_kwargs):
        events.append(("read_city",))
        return {"city_name": "A", "city_key": "a", "ocr_city_text": "A"}

    def refresh(**_kwargs):
        events.append(("refresh",))
        return {"snapshot_id": "snapshot-1"}

    def planner(**_kwargs):
        events.append(("plan",))
        return dict(plan)

    monkeypatch.setattr(actions, "resonance_pc_open_city_panel_from_main", open_city_panel)
    monkeypatch.setattr(actions, "resonance_pc_read_city_name_on_city_panel", read_city)
    monkeypatch.setattr(actions, "resonance_pc_market_refresh", refresh)
    monkeypatch.setattr(actions, "resonance_pc_trade_plan_optimal_route", planner)


def _run_flow(state_store: _MemoryStateStore):
    service = object()
    return asyncio.run(
        actions.resonance_pc_auto_cycle_trade_flow(
            fatigue_budget=600,
            cargo_capacity=650,
            book_budget=4,
            app=service,
            ocr=service,
            vision=service,
            controller=service,
            resonance_pc_city_shop_data=service,
            resonance_pc_market_data=service,
            resonance_pc_trade_planner=service,
            state_store=state_store,
        )
    )


def test_full_multi_leg_flow_trades_travels_and_sells_at_endpoint(monkeypatch):
    events: list[tuple] = []
    route = _route()
    plan = {
        "status": "ok",
        "reason": None,
        "snapshot_id": "snapshot-1",
        "route": route,
    }
    _patch_planning(monkeypatch, events, plan)
    _patch_route_ui(monkeypatch, events)

    result = _run_flow(_MemoryStateStore())

    assert events == [
        ("open_city_panel",),
        ("read_city",),
        ("refresh",),
        ("plan",),
        ("city_trade", "A", ["A1", "A2"], 2, False, True),
        ("travel", "B"),
        ("open_city_panel",),
        ("city_trade", "B", [], 0, True, False),
        ("travel", "C"),
        ("open_city_panel",),
        ("city_trade", "C", [], 0, True, False),
    ]
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["page_state"] == "city_main"
    assert result["execution"]["completed_leg_count"] == 2
    assert len(result["execution"]["leg_results"]) == 2
    assert result["execution"]["leg_results"][0]["city_trade"]["success"] is False
    assert result["execution"]["leg_results"][0]["status"] == "completed"
    assert result["final_sale"]["current_city"] == "C"


def test_route_block_records_leg_result_and_stops_before_later_city(monkeypatch):
    events: list[tuple] = []
    _patch_route_ui(monkeypatch, events, blocked_destination="B")

    result = asyncio.run(
        actions._execute_route(
            route=_route(),
            start_page_state="city_panel",
            use_fatigue_medicine=False,
            allowed_fatigue_medicines=[],
            fatigue_medicine_max_uses=4,
            app=object(),
            ocr=object(),
            vision=object(),
            controller=object(),
            city_shop_data=object(),
            state_store=_MemoryStateStore(),
        )
    )

    assert events == [
        ("city_trade", "A", ["A1", "A2"], 2, False, True),
        ("travel", "B"),
    ]
    assert result["status"] == "blocked"
    assert result["completed_leg_count"] == 0
    assert result["blocked_leg"]["to_city"] == "B"
    assert result["leg_results"][0]["status"] == "blocked"


def test_route_propagates_trade_exception_and_cleans_execution_state(monkeypatch):
    store = _MemoryStateStore()

    def fail_trade(**_kwargs):
        raise RuntimeError("trade failed")

    def unexpected_travel(**_kwargs):
        pytest.fail("travel must not start after a hard trade failure")

    monkeypatch.setattr(actions, "_execute_city_trade_inside_current_city", fail_trade)
    monkeypatch.setattr(actions, "resonance_pc_intercity_depart_and_wait", unexpected_travel)

    with pytest.raises(RuntimeError, match="trade failed"):
        asyncio.run(
            actions._execute_route(
                route=_route(),
                start_page_state="city_panel",
                use_fatigue_medicine=False,
                allowed_fatigue_medicines=[],
                fatigue_medicine_max_uses=4,
                app=object(),
                ocr=object(),
                vision=object(),
                controller=object(),
                city_shop_data=object(),
                state_store=store,
            )
        )

    assert store.data == {}


def test_empty_buy_products_skips_buy_page(monkeypatch):
    menu_nodes: list[int] = []

    monkeypatch.setattr(
        actions,
        "resonance_pc_click_city_shop_by_name",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        actions,
        "resonance_pc_click_shop_menu_node",
        lambda node_index, **_kwargs: menu_nodes.append(node_index) or {"success": True},
    )
    monkeypatch.setattr(
        actions,
        "resonance_pc_sell_goods_on_sell_page",
        lambda **_kwargs: {"success": True, "sold_confirmed": False},
    )
    monkeypatch.setattr(
        actions,
        "resonance_pc_buy_goods_on_buy_page",
        lambda **_kwargs: pytest.fail("empty migration leg must not enter the buy page"),
    )
    monkeypatch.setattr(
        actions,
        "resonance_pc_go_city_main_direct",
        lambda **_kwargs: {"success": True, "page_state": "city_main"},
    )

    result = actions._execute_city_trade_inside_current_city(
        current_city="B",
        buy_products=[],
        books_used=0,
        app=object(),
        ocr=object(),
        vision=object(),
        controller=object(),
        city_shop_data=object(),
    )

    assert menu_nodes == [2]
    assert result["success"] is True
    assert result["buy_node"] is None
    assert result["buy"] is None
    assert result["page_state"] == "city_main"


def test_bargain_request_without_buy_products_fails_before_shop_input(monkeypatch):
    monkeypatch.setattr(
        actions,
        "resonance_pc_click_city_shop_by_name",
        lambda **_kwargs: pytest.fail("invalid negotiation leg must fail before shop input"),
    )

    with pytest.raises(actions.CityTradeFlowError) as exc_info:
        actions._execute_city_trade_inside_current_city(
            current_city="B",
            buy_products=[],
            books_used=0,
            buy_bargain_to_cap=True,
            app=object(),
            ocr=object(),
            vision=object(),
            controller=object(),
            city_shop_data=object(),
        )

    assert exc_info.value.code == "negotiation_without_selected_goods"


def test_no_plan_returns_from_city_panel_without_trade_or_travel(monkeypatch):
    events: list[tuple] = []
    plan = {
        "status": "no_plan",
        "reason": "no_positive_profit_route",
        "snapshot_id": "snapshot-1",
        "route": [],
    }
    _patch_planning(monkeypatch, events, plan)

    def cleanup(**_kwargs):
        events.append(("go_city_main",))
        return {"success": True, "page_state": "city_main"}

    monkeypatch.setattr(actions, "resonance_pc_go_city_main_direct", cleanup)
    monkeypatch.setattr(
        actions,
        "_execute_city_trade_inside_current_city",
        lambda **_kwargs: pytest.fail("no-plan flow must not trade"),
    )
    monkeypatch.setattr(
        actions,
        "resonance_pc_intercity_depart_and_wait",
        lambda **_kwargs: pytest.fail("no-plan flow must not travel"),
    )

    result = _run_flow(_MemoryStateStore())

    assert events == [
        ("open_city_panel",),
        ("read_city",),
        ("refresh",),
        ("plan",),
        ("go_city_main",),
    ]
    assert result["success"] is True
    assert result["status"] == "no_plan"
    assert result["page_state"] == "city_main"
    assert result["execution"]["leg_results"] == []
    assert result["final_sale"] is None


def test_buy_negotiates_after_selection_and_before_buy(monkeypatch):
    events: list[tuple] = []

    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        actions,
        "_capture_text_items",
        lambda *_args, **_kwargs: [{"text": "A1", "norm_text": "a1", "kind": "product"}],
    )

    def click_hit(_app, hit):
        events.append(("click", hit["kind"]))
        return {"clicked": True}

    monkeypatch.setattr(actions, "_click_hit", click_hit)
    monkeypatch.setattr(
        actions,
        "execute_bargain_to_cap",
        lambda **kwargs: events.append(("bargain", kwargs["requested_to_cap"]))
        or {"requested_to_cap": True, "completed_to_cap": True},
    )
    monkeypatch.setattr(
        actions,
        "_wait_for_text_hit",
        lambda *_args, **_kwargs: {"kind": "buy"},
    )
    monkeypatch.setattr(
        actions,
        "_close_settlement",
        lambda *_args, **_kwargs: {"closed": True},
    )

    result = actions.resonance_pc_buy_goods_on_buy_page(
        product_list=["A1"],
        bargain_to_cap=True,
        app=object(),
        ocr=object(),
        vision=object(),
        controller=object(),
    )

    assert events == [
        ("bargain", False),
        ("click", "product"),
        ("bargain", True),
        ("click", "buy"),
    ]
    assert result["negotiation"]["completed_to_cap"] is True


def test_buy_does_not_confirm_after_negotiation_failure(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        actions,
        "_capture_text_items",
        lambda *_args, **_kwargs: [{"text": "A1", "norm_text": "a1"}],
    )
    monkeypatch.setattr(actions, "_click_hit", lambda *_args, **_kwargs: {"clicked": True})

    def bargain(**kwargs):
        if kwargs["requested_to_cap"]:
            raise actions.NegotiationExecutionError("negotiation_page_lost", "lost")
        return {"requested_to_cap": False, "completed_to_cap": False}

    monkeypatch.setattr(actions, "execute_bargain_to_cap", bargain)
    monkeypatch.setattr(
        actions,
        "_wait_for_text_hit",
        lambda *_args, **_kwargs: pytest.fail("buy confirmation must not be searched after negotiation failure"),
    )

    with pytest.raises(actions.CityTradeFlowError) as exc_info:
        actions.resonance_pc_buy_goods_on_buy_page(
            product_list=["A1"],
            bargain_to_cap=True,
            app=object(),
            ocr=object(),
            vision=object(),
            controller=object(),
        )

    assert exc_info.value.code == "negotiation_page_lost"


def test_sell_negotiates_after_sell_all_and_before_sell(monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    def click_text(_app, _ocr, texts, _region, **_kwargs):
        event = "sell_all" if "全部卖出" in texts else "sell"
        events.append(("click", event))
        return {"clicked": True}

    monkeypatch.setattr(actions, "_wait_and_click_text", click_text)
    monkeypatch.setattr(
        actions,
        "execute_raise_to_cap",
        lambda **kwargs: events.append(("raise", kwargs["requested_to_cap"]))
        or {"requested_to_cap": True, "completed_to_cap": True},
    )
    monkeypatch.setattr(
        actions,
        "_close_settlement",
        lambda *_args, **_kwargs: {"closed": True},
    )

    result = actions.resonance_pc_sell_goods_on_sell_page(
        raise_to_cap=True,
        app=object(),
        ocr=object(),
        vision=object(),
    )

    assert events == [
        ("click", "sell_all"),
        ("raise", False),
        ("raise", True),
        ("click", "sell"),
    ]
    assert result["negotiation"]["completed_to_cap"] is True


def test_sell_does_not_confirm_after_negotiation_failure(monkeypatch):
    calls = iter([{"clicked": True}])
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        actions,
        "_wait_and_click_text",
        lambda *_args, **_kwargs: next(calls),
    )

    def raise_price(**kwargs):
        if kwargs["requested_to_cap"]:
            raise actions.NegotiationExecutionError("negotiation_button_not_found", "missing")
        return {"requested_to_cap": False, "completed_to_cap": False}

    monkeypatch.setattr(actions, "execute_raise_to_cap", raise_price)

    with pytest.raises(actions.CityTradeFlowError) as exc_info:
        actions.resonance_pc_sell_goods_on_sell_page(
            raise_to_cap=True,
            app=object(),
            ocr=object(),
            vision=object(),
        )

    assert exc_info.value.code == "negotiation_button_not_found"
