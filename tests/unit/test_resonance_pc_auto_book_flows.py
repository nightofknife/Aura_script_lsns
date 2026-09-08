from __future__ import annotations

import asyncio
from typing import Any

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade_flow
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions import trade_planner_pc_actions as planner_actions
from plans.resonance_pc.src.services import resonance_pc_trade_planner_service as planner_module


class _FakeMarketData:
    def get_latest(self) -> dict[str, Any]:
        return {"snapshot_id": "snapshot-1", "cities": {}}

    def get_all_travel_fatigue(self) -> dict[str, Any]:
        return {"costs": {"1": {"2": 1}, "2": {"1": 1}}}


def test_public_action_forwards_auto_book_and_default_threshold() -> None:
    captured: dict[str, Any] = {}

    class _Planner:
        def plan_optimal_route(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "ok"}

    result = planner_actions.resonance_pc_trade_plan_optimal_route(
        book_budget=77,
        auto_book=True,
        resonance_pc_trade_planner=_Planner(),
    )

    assert result == {"status": "ok"}
    assert captured["auto_book"] is True
    assert captured["book_budget"] == 77
    assert captured["book_profit_threshold"] == 500000


def test_auto_book_cache_ignores_book_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class _Solver:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def solve(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "status": "ok",
                "auto_book": kwargs["auto_book"],
                "books_budget": None if kwargs["auto_book"] else kwargs["book_budget"],
            }

    monkeypatch.setattr(planner_module, "ResonancePcExactTradeSolver", _Solver)
    service = planner_module.ResonancePcTradePlannerService(_FakeMarketData())
    monkeypatch.setattr(service, "_validate_fatigue_payload", lambda _payload: None)
    monkeypatch.setattr(service, "_resolve_current_city_id", lambda **_kwargs: "1")
    monkeypatch.setattr(
        service,
        "_load_trade_constraints_payload",
        lambda: {
            "default_available_city_ids": ["1", "2"],
            "key_to_city_id": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_load_buy_lot_payload",
        lambda: {"city_product_buy_lot": {}},
    )
    monkeypatch.setattr(service, "_load_product_unlocks_payload", lambda: {"product_ids": []})
    monkeypatch.setattr(service, "_load_trade_rules_payload", lambda: {})

    auto_nonzero = service.plan_optimal_route(
        current_city_id="1",
        available_city_ids=["1", "2"],
        book_budget=77,
        auto_book=True,
    )
    auto_zero = service.plan_optimal_route(
        current_city_id="1",
        available_city_ids=["1", "2"],
        book_budget=0,
        auto_book=True,
    )
    service.plan_optimal_route(
        current_city_id="1",
        available_city_ids=["1", "2"],
        book_budget=0,
        auto_book=False,
    )
    service.plan_optimal_route(
        current_city_id="1",
        available_city_ids=["1", "2"],
        book_budget=77,
        auto_book=False,
    )

    assert auto_zero == auto_nonzero
    assert len(calls) == 3
    assert calls[0]["auto_book"] is True
    assert calls[0]["book_budget"] == 0
    assert calls[1]["auto_book"] is False
    assert calls[1]["book_budget"] == 0
    assert calls[2]["auto_book"] is False
    assert calls[2]["book_budget"] == 77


def test_preview_flow_forwards_auto_book_and_reports_book_profit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    events: list[tuple[str, str, dict[str, Any]]] = []

    class _Reporter:
        async def emit(self, stage: str, state: str, **fields: Any) -> None:
            events.append((stage, state, fields))

        def emit_from_worker(self, _stage: str, _state: str, **_fields: Any) -> None:
            pass

    monkeypatch.setattr(
        trade_flow,
        "resonance_pc_market_refresh",
        lambda **_kwargs: {
            "snapshot_id": "snapshot-1",
            "stale": False,
            "cities": {"1": {"name": "起点"}},
            "fetched_at": "now",
        },
    )

    def fake_plan(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "status": "ok",
            "route": [{"from_city": "起点", "to_city": "终点"}],
            "books_used": 2,
            "book_incremental_profit": 1200000,
            "book_incremental_profit_exact": "1200000",
            "average_book_profit": 600000,
            "average_book_profit_exact": "600000",
        }

    monkeypatch.setattr(trade_flow, "resonance_pc_trade_plan_optimal_route", fake_plan)

    result = asyncio.run(
        trade_flow._preview_trade_plan_from_start_city(
            start_city_id="1",
            book_budget=77,
            auto_book=True,
            resonance_pc_market_data=object(),
            resonance_pc_trade_planner=object(),
            reporter=_Reporter(),
        )
    )

    assert result["average_book_profit_exact"] == "600000"
    assert captured["auto_book"] is True
    assert captured["book_budget"] == 77
    assert captured["book_profit_threshold"] == 500000
    completed = next(fields for stage, state, fields in events if (stage, state) == ("planning", "completed"))
    assert completed["data"]["summary"]["book_incremental_profit"] == 1200000
    assert completed["data"]["summary"]["average_book_profit_exact"] == "600000"


def test_execute_flow_forwards_auto_book(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(trade_flow, "resonance_pc_open_city_panel_from_main", lambda **_kwargs: None)
    monkeypatch.setattr(
        trade_flow,
        "resonance_pc_read_city_name_on_city_panel",
        lambda **_kwargs: {"city_name": "起点", "city_key": "start", "ocr_city_text": "起点"},
    )
    monkeypatch.setattr(
        trade_flow,
        "resonance_pc_market_refresh",
        lambda **_kwargs: {"snapshot_id": "snapshot-1"},
    )
    monkeypatch.setattr(
        trade_flow,
        "resonance_pc_go_city_main_direct",
        lambda **_kwargs: {"page_state": "city_main"},
    )

    def fake_plan(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "status": "no_plan",
            "reason": "no_positive_profit_route",
            "route": [],
            "books_used": 0,
            "book_incremental_profit": 0,
            "book_incremental_profit_exact": "0",
            "average_book_profit": None,
            "average_book_profit_exact": None,
            "warnings": [],
        }

    monkeypatch.setattr(trade_flow, "resonance_pc_trade_plan_optimal_route", fake_plan)
    dependency = object()
    result = asyncio.run(
        trade_flow.resonance_pc_auto_cycle_trade_flow(
            book_budget=77,
            auto_book=True,
            app=dependency,
            ocr=dependency,
            vision=dependency,
            resonance_pc_city_shop_data=dependency,
            resonance_pc_market_data=dependency,
            resonance_pc_trade_planner=dependency,
            state_store=dependency,
            event_bus=None,
            context=None,
            engine=dependency,
        )
    )

    assert result["status"] == "no_plan"
    assert captured["auto_book"] is True
    assert captured["book_budget"] == 77
    assert captured["book_profit_threshold"] == 500000


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
def test_combined_commerce_forwards_auto_book_in_both_orders(
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    trade_calls: list[dict[str, Any]] = []
    preview_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        combined,
        "_build_passenger_route",
        lambda **_kwargs: {"city_a_id": "11", "city_b_id": "15", "trip_fatigue": 20},
    )
    monkeypatch.setattr(combined, "_read_current_city", lambda *_args: {"city_key": "cape_city"})
    monkeypatch.setattr(
        combined,
        "_passenger_forecast",
        lambda **_kwargs: {"expected_fatigue": 20, "end_city": {"city_id": "15"}},
    )

    async def fake_preview(**kwargs: Any) -> dict[str, Any]:
        preview_calls.append(dict(kwargs))
        return {"status": "ok", "route": [{"to_city_id": "3"}]}

    async def fake_trade(inputs: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        trade_calls.append(dict(inputs))
        destination = "11" if inputs.get("required_end_city_ids") == ["11", "15"] else "3"
        return {
            "success": True,
            "status": "completed",
            "route": [{"to_city_id": destination}],
            "selected_end_city_id": destination,
            "execution": {"completed_leg_count": 1},
            "final_sale": {"success": True, "page_state": "city_main"},
            "page_state": "city_main",
            "expected_fatigue_used": 60,
        }

    async def fake_passenger(_inputs: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "status": "completed",
            "requested_trips": 1,
            "completed_trips": 1,
            "requires_manual_completion": False,
            "loaded_destination": None,
            "page_state": "city_main",
            "end_city": {"city_id": "15"},
            "expected_fatigue_used": 20,
        }

    monkeypatch.setattr(combined, "_preview_trade_plan_from_start_city", fake_preview)
    monkeypatch.setattr(combined, "_run_trade", fake_trade)
    monkeypatch.setattr(combined, "_run_passenger", fake_passenger)
    dependency = object()
    result = asyncio.run(
        combined.resonance_pc_auto_combined_commerce_flow(
            order=order,
            total_fatigue_budget=100,
            trade_inputs={
                "available_city_ids": ["3", "11", "15"],
                "required_end_city_ids": ["3"],
                "book_budget": 77,
                "auto_book": True,
                "auto_cape_island_investment": False,
                "auto_rubbish_recycling": False,
            },
            passenger_inputs={
                "passenger_city_a_id": "11",
                "passenger_city_b_id": "15",
                "trip_count": 1,
            },
            app=dependency,
            ocr=dependency,
            vision=dependency,
            resonance_pc_city_shop_data=dependency,
            resonance_pc_market_data=dependency,
            resonance_pc_trade_planner=dependency,
            state_store=dependency,
            event_bus=dependency,
            context=dependency,
            engine=dependency,
        )
    )

    assert result["status"] == "completed"
    assert trade_calls[0]["auto_book"] is True
    assert trade_calls[0]["book_budget"] == 77
    if order == "passenger_first":
        assert preview_calls[0]["auto_book"] is True
        assert preview_calls[0]["book_budget"] == 77
    else:
        assert preview_calls == []
